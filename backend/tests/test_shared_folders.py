"""Folders the owner shares from a browser: relay, safety, and access contracts."""

import asyncio
import threading
import time

import pytest
from pydantic import ValidationError

from app import auth as auth_module
from app import shared_folders
from app.broadcast import get_system_broadcast
from app.routes.shared_folders import FolderCommandBody, normalized_path
from app.shared_folders import DeviceReport, SharedFolder, SharedFolderRegistry
from tests.test_browser_access_routes import https, invite  # noqa: F401 - fixture and helper


def _folder(folder_id="f1", *, name="Downloads", mode="edit", access="remembered", state="ready"):
  return SharedFolder(id=folder_id, name=name, mode=mode, access=access, state=state)


def _report(subscription_id="s1", *, device="laptop", sequence=1, folders=None, at=None):
  return DeviceReport(
    subscription_id=subscription_id,
    device_id=device,
    device_label=f"Chrome on {device}",
    sequence=sequence,
    folders=tuple(folders if folders is not None else [_folder()]),
    reported_at=at if at is not None else time.time(),
  )


class _FakeStreams:
  """Live system-stream subscriptions, recording what each one receives."""

  def __init__(self, *live):
    self.live = set(live)
    self.sent: list[tuple[str, dict]] = []

  def is_live(self, subscription_id):
    return subscription_id in self.live

  def deliver(self, subscription_id, event):
    if subscription_id not in self.live:
      return False
    self.sent.append((subscription_id, event))
    return True

  def registry(self):
    return SharedFolderRegistry(self.is_live, self.deliver)


async def _next_event(streams):
  for _ in range(100):
    if streams.sent:
      return streams.sent.pop(0)
    await asyncio.sleep(0.01)
  raise AssertionError("no command was delivered")


@pytest.mark.asyncio
async def test_command_reaches_only_the_reporting_stream_and_returns_its_answer():
  streams = _FakeStreams("s1", "s2")
  registry = streams.registry()
  assert registry.report(_report("s1", folders=[_folder("f1")]))
  assert registry.report(_report("s2", device="phone", folders=[_folder("f2")]))

  waiting = asyncio.create_task(registry.issue("f1", {"op": "list", "path": ""}))
  subscription_id, event = await _next_event(streams)
  assert subscription_id == "s1"
  assert event["type"] == "shared_folder_command" and event["folderId"] == "f1"
  # Another tab or device cannot answer a command it was never sent.
  assert not registry.answer("s2", event["commandId"], {"ok": True, "result": "forged"})
  assert registry.answer("s1", event["commandId"], {"ok": True, "result": {"entries": []}})
  assert await waiting == {"ok": True, "result": {"entries": []}}
  assert not registry.answer("s1", event["commandId"], {"ok": True, "result": "late"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,access", [("read", "remembered"), ("read", "visit"), ("edit", "visit")])
async def test_read_only_or_visit_folder_refuses_every_change_before_reaching_the_browser(mode, access):
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report(folders=[_folder(mode=mode, access=access)]))
  for operation in shared_folders.EDIT_OPERATIONS:
    outcome = await registry.issue("f1", {"op": operation, "path": "a.txt"})
    assert outcome["code"] == "read_only"
  assert streams.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["needs_permission", "needs_pick", "paused"])
async def test_unready_folder_returns_an_owner_actionable_reason(state):
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report(folders=[_folder(state=state)]))
  outcome = await registry.issue("f1", {"op": "list", "path": ""})
  assert outcome["ok"] is False and outcome["code"] == state
  assert "Settings" in outcome["error"]


@pytest.mark.asyncio
async def test_closed_browser_is_offline_and_unknown_folder_is_not_found():
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report("s1"))
  streams.live.clear()
  assert (await registry.issue("f1", {"op": "list", "path": ""}))["code"] == "offline"
  assert (await registry.issue("nope", {"op": "list", "path": ""}))["code"] == "not_found"
  listed = registry.folders()
  assert listed[0]["online"] is False and listed[0]["state"] == "offline"


@pytest.mark.asyncio
async def test_tab_closing_mid_command_fails_fast_instead_of_waiting_for_timeout(monkeypatch):
  monkeypatch.setattr(shared_folders, "LIVENESS_POLL_SECONDS", 0.02)
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report())
  waiting = asyncio.create_task(registry.issue("f1", {"op": "read", "path": "a.txt"}))
  await _next_event(streams)
  streams.live.clear()
  started = time.monotonic()
  assert (await waiting)["code"] == "offline"
  assert time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_unanswered_command_times_out_and_is_forgotten(monkeypatch):
  monkeypatch.setattr(shared_folders, "COMMAND_TIMEOUT_SECONDS", 0.05)
  monkeypatch.setattr(shared_folders, "LIVENESS_POLL_SECONDS", 0.02)
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report())
  assert (await registry.issue("f1", {"op": "info", "path": ""}))["code"] == "timeout"
  assert registry._pending == {}


@pytest.mark.asyncio
async def test_busy_browser_refuses_more_than_the_pending_limit(monkeypatch):
  monkeypatch.setattr(shared_folders, "MAX_PENDING_PER_STREAM", 1)
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report())
  first = asyncio.create_task(registry.issue("f1", {"op": "info", "path": ""}))
  _, event = await _next_event(streams)
  assert (await registry.issue("f1", {"op": "info", "path": ""}))["code"] == "busy"
  registry.answer("s1", event["commandId"], {"ok": True, "result": {}})
  assert (await first)["ok"] is True


@pytest.mark.asyncio
async def test_folder_picked_in_one_tab_is_served_by_that_tab_not_a_newer_one():
  # Safari and Firefox keep a picked folder only in the tab where it was picked.
  streams = _FakeStreams("picker-tab", "other-tab")
  registry = streams.registry()
  registry.report(_report("picker-tab", at=1, folders=[_folder(access="visit", mode="read")]))
  registry.report(_report("other-tab", at=2, folders=[_folder(access="visit", mode="read", state="needs_pick")]))
  assert [folder["state"] for folder in registry.folders()] == ["ready"]
  waiting = asyncio.create_task(registry.issue("f1", {"op": "list", "path": ""}))
  subscription_id, event = await _next_event(streams)
  assert subscription_id == "picker-tab"
  registry.answer("picker-tab", event["commandId"], {"ok": True, "result": {}})
  assert (await waiting)["ok"] is True


def test_delayed_older_report_cannot_overwrite_a_newer_one():
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report(sequence=2, folders=[_folder(name="New")]))
  registry.report(_report(sequence=1, folders=[_folder(name="Old")]))
  assert [folder["name"] for folder in registry.folders()] == ["New"]


def test_report_from_a_disconnected_stream_is_refused():
  registry = _FakeStreams().registry()
  assert registry.report(_report("gone")) is False
  assert registry.folders() == []


def test_one_device_keeps_its_live_tab_and_forgets_superseded_closed_tabs():
  streams = _FakeStreams("new")
  registry = streams.registry()
  streams.live.add("old")
  registry.report(_report("old", at=1, folders=[_folder(name="Old tab")]))
  registry.report(_report("new", at=2, folders=[_folder(name="New tab")]))
  streams.live.discard("old")
  registry.report(_report("new", sequence=2, at=3, folders=[_folder(name="New tab")]))
  assert set(registry._reports) == {"new"}
  assert [folder["name"] for folder in registry.folders()] == ["New tab"]


def test_remembered_offline_devices_are_capped(monkeypatch):
  monkeypatch.setattr(shared_folders, "MAX_REMEMBERED_DEVICES", 3)
  streams = _FakeStreams()
  registry = streams.registry()
  for index in range(6):
    streams.live.add(f"s{index}")
    registry.report(_report(f"s{index}", device=f"d{index}", at=index))
    streams.live.discard(f"s{index}")
  assert len(registry._reports) <= 3


@pytest.mark.parametrize("path", ["..", "a/../b", "./a", "a//b", "a\\b", "a\x00b", "a/\nb"])
def test_path_cannot_leave_the_shared_folder(path):
  with pytest.raises(ValueError):
    normalized_path(path, allow_root=False)


def test_path_is_plain_segments_and_root_only_where_allowed():
  assert normalized_path("/Photos/2026/a.jpg", allow_root=False) == "Photos/2026/a.jpg"
  assert normalized_path("", allow_root=True) == ""
  with pytest.raises(ValueError):
    normalized_path("  ", allow_root=False)


@pytest.mark.parametrize("body", [
  {"op": "list", "path": "", "content": "x"},
  {"op": "read", "path": "a.txt", "to": "b.txt"},
  {"op": "move", "path": "a.txt"},
  {"op": "write", "path": "a.txt"},
  {"op": "read", "path": ""},
  {"op": "read", "path": "a.txt", "length": 4 * 1024 * 1024 + 1},
  {"op": "exec", "path": "a.txt"},
  {"op": "move", "path": "a.txt", "to": "../b.txt"},
  {"op": "image", "path": ""},
  {"op": "image", "path": "a.jpg", "maxEdge": 8000},
  {"op": "list", "path": "", "maxEdge": 2576},
])
def test_command_accepts_only_its_own_fields_and_safe_paths(body):
  with pytest.raises(ValidationError):
    FolderCommandBody(**body)


def test_image_command_is_a_read_that_carries_only_its_size_limit():
  body = FolderCommandBody(op="image", path="Photos/IMG_1.HEIC", maxEdge=2576)
  assert body.wire() == {"op": "image", "path": "Photos/IMG_1.HEIC", "maxEdge": 2576}
  assert "image" in shared_folders.READ_OPERATIONS


@pytest.mark.asyncio
async def test_read_only_folder_still_shows_images():
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report(folders=[_folder(mode="read")]))
  waiting = asyncio.create_task(registry.issue("f1", {"op": "image", "path": "a.jpg", "maxEdge": 2576}))
  _, event = await _next_event(streams)
  registry.answer("s1", event["commandId"], {"ok": True, "result": {"shrunk": True}})
  assert (await waiting)["ok"] is True


def test_move_command_normalizes_both_paths():
  body = FolderCommandBody(op="move", path="/a.txt", to="Done/a.txt")
  assert body.wire() == {"op": "move", "path": "a.txt", "to": "Done/a.txt"}


# ---------------------------------------------------------------------------
# Routes: who may report, answer, list and command.


def _agent(chat_id="chat-1", **kwargs):
  return {"Authorization": "Bearer " + auth_module.create_agent_token(chat_id, "test", 0, **kwargs)}


_REPORT = {
  "sequence": 1,
  "deviceId": "laptop",
  "deviceLabel": "Chrome on Mac",
  "folders": [{"id": "f1", "name": "Downloads", "mode": "edit", "access": "remembered", "state": "ready"}],
}


@pytest.fixture
def live_stream():
  broadcast = get_system_broadcast()
  subscription = broadcast.subscribe()
  yield subscription
  broadcast.unsubscribe(subscription)
  shared_folders.registry._reports.clear()
  shared_folders.registry._pending.clear()


def test_only_the_owner_browser_can_report_folders(client, auth, live_stream):
  path = f"/api/shared-folders/devices/{live_stream.id}"
  assert client.put(path, headers=_agent(), json=_REPORT).status_code == 403
  assert client.put(path, headers=auth, json=_REPORT).status_code == 204
  assert client.put("/api/shared-folders/devices/gone", headers=auth, json=_REPORT).status_code == 404


def test_owner_browser_lists_but_cannot_command_folders(client, auth, live_stream):
  client.put(f"/api/shared-folders/devices/{live_stream.id}", headers=auth, json=_REPORT)
  listed = client.get("/api/shared-folders", headers=auth)
  assert listed.status_code == 200
  assert listed.json()["folders"][0]["online"] is True
  command = client.post("/api/shared-folders/f1/commands", headers=auth, json={"op": "list"})
  assert command.status_code == 403


def test_guest_and_guest_started_agent_never_reach_owner_folders(https, auth):  # noqa: F811
  grant_id, token, _ = invite(https)
  guest = {"Authorization": "Bearer " + token}
  assert https.get("/api/shared-folders", headers=guest).status_code == 403
  assert https.put("/api/shared-folders/devices/x", headers=guest, json=_REPORT).status_code == 403
  from app.browser_access import BrowserLineage
  guest_agent = _agent(browser=BrowserLineage(grant_id))
  assert https.get("/api/shared-folders", headers=guest_agent).status_code == 403
  assert https.post(
    "/api/shared-folders/f1/commands", headers=guest_agent, json={"op": "list"},
  ).status_code == 403


def test_mini_app_tokens_never_reach_shared_folders(client, auth, db):
  from test_app_fixtures import create_local_app
  from app import models
  app_row = create_local_app(client, auth, name="Folder Snoop")
  owner = db.query(models.Owner).one()
  token = auth_module.create_app_token(app_row["id"], owner.username, owner.token_epoch)
  app_headers = {"Authorization": f"Bearer {token}"}
  assert client.get("/api/shared-folders", headers=app_headers).status_code == 403
  assert client.post(
    "/api/shared-folders/f1/commands", headers=app_headers, json={"op": "list"},
  ).status_code == 403


def test_agent_command_round_trips_through_the_owner_browser(client, auth, live_stream):
  client.put(f"/api/shared-folders/devices/{live_stream.id}", headers=auth, json=_REPORT)
  outcome = {}

  def agent_runs_command():
    outcome["response"] = client.post(
      "/api/shared-folders/f1/commands", headers=_agent(),
      json={"op": "read", "path": "notes.txt", "length": 10},
    )

  thread = threading.Thread(target=agent_runs_command)
  thread.start()
  event = _await_stream_event(live_stream)
  assert event == {
    "type": "shared_folder_command", "commandId": event["commandId"], "folderId": "f1",
    "op": "read", "path": "notes.txt", "length": 10,
  }
  answered = client.post("/api/shared-folders/responses", headers=auth, json={
    "subscriptionId": live_stream.id, "commandId": event["commandId"],
    "ok": True, "result": {"text": "hello", "size": 5},
  })
  assert answered.status_code == 200, answered.text
  thread.join(timeout=10)
  assert outcome["response"].json() == {"ok": True, "result": {"text": "hello", "size": 5}}


def test_agent_sees_refusals_as_ordinary_results(client, auth, live_stream):
  report = {**_REPORT, "folders": [{**_REPORT["folders"][0], "mode": "read"}]}
  client.put(f"/api/shared-folders/devices/{live_stream.id}", headers=auth, json=report)
  response = client.post(
    "/api/shared-folders/f1/commands", headers=_agent(), json={"op": "bin", "path": "a.txt"},
  )
  assert response.status_code == 200
  assert response.json()["code"] == "read_only"


def test_agent_cannot_answer_for_the_browser(client, auth, live_stream):
  response = client.post("/api/shared-folders/responses", headers=_agent(), json={
    "subscriptionId": live_stream.id, "commandId": "x", "ok": True,
  })
  assert response.status_code == 403


def _await_stream_event(subscription, timeout=5.0):
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    try:
      return subscription.get_nowait()
    except asyncio.QueueEmpty:
      time.sleep(0.01)
  raise AssertionError("the browser stream received no command")
