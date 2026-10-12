"""Reviewed update batches: server-owned, durable, and the single-update path."""

import asyncio
import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
import pytest

from app import app_update_batches as batches
from app import fs_locks, install, models

COMMIT = "a" * 40


def request(*ids, key=None, commit=COMMIT):
  return batches.ReviewedUpdateBatch(id=key or uuid4(), updates=[{
    "app_id": app_id,
    "manifest_url": f"https://example.test/app-{app_id}/mobius.json",
    "upstream_commit": commit,
    "source_digest": "b" * 64,
    "capability_digest": "c" * 64,
  } for app_id in ids])


@pytest.fixture(autouse=True)
def lifecycle_lock(monkeypatch):
  """A lock bound to this test's loop; the module singleton outlives loops."""
  monkeypatch.setattr(fs_locks, "_lifecycle_lock", asyncio.Lock())


LANDED = "e" * 40  # an accepted source commit that contains COMMIT


@pytest.fixture
def git_state(monkeypatch):
  """Durable Git facts per source dir: which pending receipts exist."""
  pending = set()
  monkeypatch.setattr(batches.app_git, "ref_is_ancestor",
    lambda path, reviewed, accepted: reviewed == COMMIT and accepted == LANDED)
  monkeypatch.setattr(install, "pending_conflict_update_receipt_present",
    lambda path: str(path) in pending)
  monkeypatch.setattr(install, "read_pending_conflict_update_receipt",
    lambda path, *, app_id, upstream_commit:
      {"upstream_commit": upstream_commit}
      if str(path) in pending and upstream_commit == COMMIT else None)
  return pending


@pytest.fixture
def app_rows(monkeypatch):
  monkeypatch.setattr(batches, "live_app_or_404", lambda db, app_id:
    SimpleNamespace(id=app_id, name=f"App {app_id}", version="1.0"))


def add_app(db, tmp_path, name, **fields):
  source = tmp_path / name
  source.mkdir()
  app = models.App(name=name, slug=name, jsx_source="export default 1",
    source_dir=str(source), **fields)
  db.add(app)
  db.commit()
  return app.id


async def drain(owner):
  """Let accepted work finish on its own; stop() would cancel it."""
  while owner.tasks:
    await asyncio.gather(*list(owner.tasks.values()), return_exceptions=True)


@pytest.mark.asyncio
async def test_entries_run_two_at_a_time_and_keep_mixed_outcomes(tmp_path, app_rows, monkeypatch):
  entered = set()
  all_entered = asyncio.Event()
  release = asyncio.Event()

  async def execute(update):
    app_id = update["app_id"]
    entered.add(app_id)
    if len(entered) == batches._RUNNING_ENTRIES:
      all_entered.set()
    await release.wait()
    if app_id == 3:
      raise HTTPException(409, detail={"code": "update_changed", "message": "Review the changed candidate."})
    if app_id == 4:
      raise RuntimeError("test failure")
    if app_id == 5:
      raise HTTPException(409, detail={
        "code": "git_transfer_timeout", "message": "Timed out.", "retryable": True,
      })
    return {"status": "updated" if app_id == 1 else "conflict", "version": "2.0"}

  published = []
  monkeypatch.setattr(batches, "get_system_broadcast",
    lambda: SimpleNamespace(publish=published.append))
  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  body = request(1, 2, 3, 4, 5)
  first = owner.submit(body, None)
  await asyncio.wait_for(all_entered.wait(), timeout=2)
  assert first["phase"] == "applying"
  # Only the transfer slots run; the rest are really queued.
  await asyncio.sleep(0.01)
  assert [e["status"] for e in owner.snapshot(first["id"])["entries"]] == [
    "applying", "applying", "queued", "queued", "queued",
  ]
  assert entered == {1, 2}
  assert len(owner.tasks) == 1
  # The same id is the same approval: a retry attaches, never starts twice.
  assert owner.submit(body, None)["id"] == first["id"]
  release.set()
  await drain(owner)
  final = owner.snapshot(first["id"])
  assert [e["status"] for e in final["entries"]] == ["updated", "conflict", "failed", "failed", "failed"]
  assert [(e["error"], e["error_code"], e["retryable"]) for e in final["entries"][2:]] == [
    ("Review the changed candidate.", "update_changed", False),
    ("This update could not finish. Check it again before retrying.", None, False),
    ("Timed out.", "git_transfer_timeout", True),
  ]
  assert final["entries"][0]["error_code"] is None
  # Like the single route: only durable changes refresh app lists.
  assert sorted(event["appId"] for event in published) == ["1", "2"]
  assert final["phase"] == "complete"
  assert "updates" not in final
  assert json.loads((owner.root / f"{first['id']}.json").read_text())["phase"] == "complete"
  assert (owner.root / f"{first['id']}.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_entries_share_the_single_update_path_and_lifecycle_lock(tmp_path, monkeypatch, db):
  """Transfers overlap outside every lock; local installs take the lock in turn."""
  ids = []
  for name in ("first", "second"):
    app_id = add_app(db, tmp_path, name, version="1.0")
    subprocess.run(["git", "init", "-q", str(tmp_path / name)], check=True)
    ids.append(app_id)
  fetching, installing = set(), []
  both_fetching, release_fetch = asyncio.Event(), asyncio.Event()
  overlap = []

  async def fetch_app_commit(source_dir, ref, **_):
    assert not fs_locks.install_uninstall_lock().locked()
    fetching.add(source_dir)
    if len(fetching) == 2:
      both_fetching.set()
    await release_fetch.wait()
    return ref

  async def install_from_manifest(session, **kwargs):
    assert fs_locks.install_uninstall_lock().locked()
    installing.append(kwargs["reviewed_app_id"])
    overlap.append(len(installing))
    await asyncio.sleep(0.01)
    installing.remove(kwargs["reviewed_app_id"])
    assert kwargs["reviewed_upstream_commit"] == COMMIT
    assert kwargs["reviewed_source_digest"] == "b" * 64
    assert kwargs["reviewed_capability_digest"] == "c" * 64
    assert kwargs["staged_clone"] is None
    return SimpleNamespace(mode="update", app=SimpleNamespace(version="2.0"), warnings=["w"])

  monkeypatch.setattr(install, "fetch_app_commit", fetch_app_commit)
  monkeypatch.setattr(install, "install_from_manifest", install_from_manifest)
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  accepted = owner.submit(request(*ids), db)
  await asyncio.wait_for(both_fetching.wait(), timeout=2)
  release_fetch.set()
  await drain(owner)
  final = owner.snapshot(accepted["id"])
  assert [(e["status"], e["version"], e["warnings"]) for e in final["entries"]] == [
    ("updated", "2.0", ["w"]), ("updated", "2.0", ["w"]),
  ]
  assert max(overlap) == 1


@pytest.mark.asyncio
async def test_unrelated_batch_never_hides_actionable_results(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  keys = [str(uuid4()) for _ in range(4)]
  for index, (app_id, status) in enumerate([(1, "conflict"), (2, "failed"), (3, "updated")]):
    owner._save({"id": keys[index], "created_at": f"2026-01-0{index + 1}",
      "updates": [], "entries": [{"app_id": app_id, "status": status}]})
  assert [r["id"] for r in owner.recent()] == keys[2::-1]
  owner._save({"id": keys[3], "created_at": "2026-01-04", "updates": [],
    "entries": [{"app_id": 1, "status": "updated"}]})
  assert [r["id"] for r in owner.recent()] == [keys[3], keys[1]]
  # Older receipts remain addressable for exact-id retries and audit.
  assert owner.snapshot(keys[0])["entries"][0]["status"] == "conflict"


@pytest.mark.asyncio
async def test_closing_progress_does_not_cancel_approved_work(tmp_path, app_rows):
  entered, release = asyncio.Event(), asyncio.Event()

  async def execute(update):
    entered.set()
    await release.wait()
    return {"status": "updated"}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  accepted = owner.submit(request(1), None)
  stream = owner.snapshots(accepted["id"])
  assert (await anext(stream))["id"] == accepted["id"]
  await entered.wait()
  await stream.aclose()
  assert not owner.listeners
  assert owner.recent()[0]["phase"] == "applying"
  release.set()
  await drain(owner)
  assert owner.snapshot(accepted["id"])["entries"][0]["status"] == "updated"


@pytest.mark.asyncio
async def test_shutdown_settles_running_installs_and_records_what_landed(tmp_path, db, git_state, monkeypatch):
  """Cancellation waits for the install pass, then reads the durable row."""
  from app.runtime_supervisors import RuntimeSupervisors

  landed = add_app(db, tmp_path, "landed", version="1.0", upstream_commit="0" * 40)
  rolled_back = add_app(db, tmp_path, "rolled-back", version="1.0", upstream_commit="0" * 40)
  queued = add_app(db, tmp_path, "queued", version="1.0", upstream_commit="0" * 40)
  monkeypatch.setattr(batches, "_RUNNING_ENTRIES", 2)
  entered, release = asyncio.Event(), asyncio.Event()
  started = set()
  order = []

  async def pass_in_thread(app_id):
    # Stands in for a locked install pass whose threads outlive a cancel.
    await release.wait()
    if app_id == landed:
      with batches.SessionLocal() as session:
        row = session.get(models.App, app_id)
        row.upstream_commit, row.source_commit, row.version = COMMIT, LANDED, "2.0"
        session.commit()
    order.append(f"settled {app_id}")
    return {"status": "updated"}

  async def execute(update):
    started.add(update["app_id"])
    if len(started) == 2:
      entered.set()
    return await install._settle_before_cancel(pass_in_thread(update["app_id"]))

  async def resident():
    try:
      await asyncio.Event().wait()
    finally:
      order.append("other owner stopped")

  runtime = RuntimeSupervisors(settings=SimpleNamespace(data_dir=str(tmp_path)),
    logger=logging.getLogger(__name__), restart_authorization=None,
    restart_fallback_chats=[])
  runtime.app_updates.execute = execute
  await runtime.app_updates.start()
  runtime._spawn("test-resident", resident())
  accepted = runtime.app_updates.submit(request(landed, rolled_back, queued), db)
  await entered.wait()
  stopping = asyncio.create_task(runtime.stop())
  await asyncio.sleep(0.05)
  assert not stopping.done()
  assert not runtime.app_updates.accepting
  assert order == []
  release.set()
  await stopping
  assert order[-1] == "other owner stopped"
  entries = runtime.app_updates.snapshot(accepted["id"])["entries"]
  assert [(e["status"], e["version"]) for e in entries] == [
    ("updated", "2.0"), ("failed", "1.0"), ("failed", "1.0"),
  ]
  assert entries[1]["error"] == entries[2]["error"] == batches._NOT_APPLIED
  assert started == {landed, rolled_back}  # the queued entry never ran
  saved = json.loads((runtime.app_updates.root / f"{accepted['id']}.json").read_text())
  assert saved["phase"] == "complete"
  with pytest.raises(HTTPException) as closed:
    runtime.app_updates.submit(request(landed), db)
  assert closed.value.status_code == 503


@pytest.mark.asyncio
async def test_batch_identity_and_running_app_cannot_be_repurposed(tmp_path, app_rows):
  release = asyncio.Event()

  async def execute(update):
    await release.wait()
    return {"status": "updated"}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  body = request(1)
  owner.submit(body, None)
  try:
    with pytest.raises(HTTPException, match="different reviewed"):
      owner.submit(request(2, key=body.id), None)
    with pytest.raises(HTTPException) as conflict:
      owner.submit(request(1), None)
    assert conflict.value.detail["code"] == "app_update_running"
    assert conflict.value.detail["batch_id"] == str(body.id)
  finally:
    release.set()
    await drain(owner)


@pytest.mark.asyncio
async def test_restart_settles_unfinished_entries_from_durable_rows(tmp_path, db, git_state):
  """Boot never replays an approval; the served source says what happened."""
  landed = add_app(db, tmp_path, "landed", version="2.0", upstream_commit=COMMIT,
    source_commit=LANDED)
  conflicted = add_app(db, tmp_path, "conflicted", version="1.0", upstream_commit=COMMIT,
    source_commit="1" * 40)
  git_state.add(str(tmp_path / "conflicted"))
  # A conflicted retry records the reviewed upstream; clearing its receipt
  # never serves it. Upstream alone must not read as updated.
  cleared = add_app(db, tmp_path, "cleared", version="1.0", upstream_commit=COMMIT,
    source_commit="1" * 40)
  removed = add_app(db, tmp_path, "removed", version="1.0", upstream_commit=COMMIT,
    deleted_at=datetime.now(UTC).replace(tzinfo=None))
  finished = add_app(db, tmp_path, "finished", version="2.0", upstream_commit=COMMIT)
  body = request(finished, landed, conflicted, cleared, removed)
  key = str(body.id)
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  owner._save({"id": key, "created_at": _now(),
    "updates": [u.model_dump() for u in body.updates],
    "entries": [{"app_id": finished, "status": "updated", "version": "2.0", "error": ""}] + [
      {"app_id": app_id, "status": status, "version": "1.0", "error": ""}
      for app_id, status in [(landed, "applying"), (conflicted, "applying"),
                             (cleared, "applying"), (removed, "queued")]
    ]})

  async def forbidden(update):
    pytest.fail("Boot must not replay an uncertain operation.")

  reboot = batches.AppUpdateBatches(str(tmp_path), execute=forbidden)
  await reboot.start()
  assert reboot.accepting
  result = reboot.submit(body, db)
  assert [(e["status"], e["version"]) for e in result["entries"]] == [
    ("updated", "2.0"), ("updated", "2.0"), ("conflict", "1.0"),
    ("failed", "1.0"), ("failed", "1.0"),
  ]
  assert result["entries"][3]["error"] == batches._NOT_APPLIED
  assert result["entries"][3]["error_code"] == "update_not_applied"
  assert result["entries"][4]["error"] == "This app is no longer installed."
  assert result["phase"] == "complete"
  assert reboot.tasks == {}
  assert json.loads((reboot.root / f"{key}.json").read_text())["phase"] == "complete"


def _now():
  return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_restart_prunes_only_old_finished_receipts(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
  expired, recent_key = str(uuid4()), str(uuid4())
  for key in (expired, recent_key):
    owner._save({"id": key, "created_at": old, "updates": [],
      "entries": [{"app_id": 1, "status": "updated"}]})
  record = json.loads((owner.root / f"{expired}.json").read_text())
  record["updated_at"] = old
  (owner.root / f"{expired}.json").write_text(json.dumps(record))
  reboot = batches.AppUpdateBatches(str(tmp_path))
  await reboot.start()
  assert set(reboot.records) == {recent_key}
  assert not (reboot.root / f"{expired}.json").exists()


@pytest.mark.asyncio
async def test_receipt_failure_never_starts_unrecorded_work(tmp_path, app_rows, monkeypatch):
  calls = []

  async def execute(update):
    calls.append(update)
    return {"status": "updated"}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  def no_space(*args, **kwargs):
    raise OSError("disk full")
  monkeypatch.setattr(batches, "atomic_write", no_space)
  with pytest.raises(HTTPException) as unavailable:
    owner.submit(request(1), None)
  assert unavailable.value.status_code == 503
  assert "could not be saved" in unavailable.value.detail
  assert calls == []
  assert owner.tasks == {}
  assert not owner.accepting


@pytest.mark.asyncio
async def test_damaged_progress_disables_only_updates_without_discarding_evidence(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.root.mkdir()
  path = owner.root / f"{uuid4()}.json"
  path.write_text("interrupted bytes")
  await owner.start()
  assert not owner.accepting
  assert path.read_text() == "interrupted bytes"
  with pytest.raises(HTTPException, match="could not be read"):
    owner.submit(request(1), None)


def test_duplicate_apps_and_incomplete_review_are_rejected():
  with pytest.raises(ValueError, match="only once"):
    request(1, 1)
  body = request(1).model_dump()
  del body["updates"][0]["source_digest"]
  with pytest.raises(ValueError, match="source_digest"):
    batches.ReviewedUpdateBatch.model_validate(body)


@pytest.mark.asyncio
async def test_result_write_failure_lets_started_siblings_finish_and_closes_admission(tmp_path, app_rows, monkeypatch):
  entered, release = asyncio.Event(), asyncio.Event()
  started = set()
  finished = set()

  async def execute(update):
    app_id = update["app_id"]
    started.add(app_id)
    if len(started) == 2:
      entered.set()
    await release.wait()
    finished.add(app_id)
    return {"status": "updated"}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  accepted = owner.submit(request(1, 2), None)
  await entered.wait()

  def no_space(*args, **kwargs):
    raise OSError("disk full")

  monkeypatch.setattr(batches, "atomic_write", no_space)
  release.set()
  await drain(owner)
  assert finished == {1, 2}
  assert not owner.accepting
  with pytest.raises(HTTPException, match="could not be saved"):
    owner.snapshot(accepted["id"])
  with pytest.raises(HTTPException):
    owner.submit(request(3), None)


def test_batch_routes_require_manage_apps_and_owner_control(client, owner_token, monkeypatch):
  owner = SimpleNamespace(recent=lambda: [])
  async def batches_owner(request):
    return owner
  monkeypatch.setattr("app.routes.app_update_batches.batches", batches_owner)
  assert client.get("/api/apps/update-batches").status_code == 401
  response = client.get("/api/apps/update-batches", headers={"Authorization": f"Bearer {owner_token}"})
  assert response.status_code == 200
  assert response.json() == {"batches": []}


def test_batch_routes_report_unavailable_owner(client, owner_token):
  headers = {"Authorization": f"Bearer {owner_token}"}
  response = client.get("/api/apps/update-batches", headers=headers)
  assert response.status_code == 503


def test_unprivileged_app_and_delegated_agent_cannot_admit_batches(client, owner_token, db, tmp_path):
  from app import auth

  owner = db.query(models.Owner).one()
  app = models.App(name="Ordinary app", slug="ordinary-batch-test", jsx_source="export default 1", source_dir=str(tmp_path))
  db.add(app)
  db.commit()
  token = auth.create_app_token(app.id, owner.username, owner.token_epoch, app.token_nonce)
  app_headers = {"Authorization": f"Bearer {token}"}
  body = request(app.id).model_dump(mode="json")
  assert client.get("/api/apps/update-batches", headers=app_headers).status_code == 403
  assert client.post("/api/apps/update-batches", headers=app_headers, json=body).status_code == 403

  delegated = auth.create_agent_token(
    "batch-child", owner.username, owner.token_epoch,
    delegation_id="batch-delegation", delegation_chat="batch-child",
  )
  assert client.post("/api/apps/update-batches", headers={
    "Authorization": f"Bearer {delegated}",
  }, json=body).status_code == 403


def test_store_permission_admits_exact_review_but_never_incomplete_consent(client, owner_token, db, tmp_path, monkeypatch):
  from app import auth

  owner = db.query(models.Owner).one()
  store = models.App(name="Store", slug="store-batch-test", manage_apps=True,
    jsx_source="export default 1", source_dir=str(tmp_path))
  db.add(store)
  db.commit()
  token = auth.create_app_token(store.id, owner.username, owner.token_epoch, store.token_nonce)
  headers = {"Authorization": f"Bearer {token}"}
  admitted = []

  def submit(body, session):
    admitted.append(body.model_dump(mode="json"))
    return {"id": str(body.id), "phase": "applying"}

  async def batches_owner(request):
    return SimpleNamespace(submit=submit)
  monkeypatch.setattr("app.routes.app_update_batches.batches", batches_owner)
  body = request(store.id).model_dump(mode="json")
  response = client.post("/api/apps/update-batches", headers=headers, json=body)
  assert response.status_code == 200, response.text
  assert admitted == [body]
  del body["updates"][0]["upstream_commit"]
  assert client.post("/api/apps/update-batches", headers=headers, json=body).status_code == 422
  assert len(admitted) == 1


@pytest.mark.asyncio
async def test_progress_stream_coalesces_to_latest_result(tmp_path, app_rows):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  key = str(uuid4())
  record = {"id": key, "created_at": "2026-01-01", "updates": [],
            "entries": [{"app_id": 1, "status": "queued"}]}
  owner._save(record)
  stream = owner.snapshots(key)
  assert (await anext(stream))["entries"][0]["status"] == "queued"
  record["entries"][0]["status"] = "applying"
  owner._save(record)
  record["entries"][0]["status"] = "updated"
  owner._save(record)
  assert (await anext(stream))["entries"][0]["status"] == "updated"
  with pytest.raises(StopAsyncIteration):
    await anext(stream)
  assert not owner.listeners


@pytest.mark.asyncio
async def test_conflicted_entry_follows_its_resolver_until_the_answer_is_applied(tmp_path, db, git_state):
  app_id = add_app(db, tmp_path, "resolved", version="1.0", source_commit="d" * 40,
    upstream_commit=COMMIT)
  git_state.add(str(tmp_path / "resolved"))

  async def execute(update):
    return {"status": "conflict", "version": "1.0", "warnings": []}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  accepted = owner.submit(request(app_id), db)
  await drain(owner)
  key = accepted["id"]
  assert owner.snapshot(key)["entries"][0]["status"] == "conflict"
  assert owner.snapshot(key)["entries"][0]["resolver_chat_id"] is None

  # The owner opens a resolver for this exact upstream commit.
  row = db.get(models.App, app_id)
  row.conflict_resolver_chat_id = "resolver-chat"
  row.conflict_resolver_upstream_commit = COMMIT
  db.commit()
  await owner.refresh_results(key, db)
  entry = owner.snapshot(key)["entries"][0]
  assert (entry["status"], entry["resolver_chat_id"]) == ("conflict", "resolver-chat")

  # Only the applied answer, with its pending receipt gone, finishes the entry.
  row = db.get(models.App, app_id)
  row.version, row.source_commit = "2.0", LANDED
  db.commit()
  git_state.clear()
  await owner.refresh_results(key, db)
  entry = owner.snapshot(key)["entries"][0]
  assert (entry["status"], entry["version"], entry["resolver_chat_id"]) == ("updated", "2.0", None)


@pytest.mark.asyncio
async def test_conflict_cleared_without_its_answer_is_no_longer_actionable(tmp_path, db, git_state):
  """resolve-update can drop a stale receipt; the update then did not land."""
  app_id = add_app(db, tmp_path, "dropped", version="1.0", source_commit="d" * 40,
    upstream_commit=COMMIT, conflict_resolver_chat_id="old-resolver",
    conflict_resolver_upstream_commit=COMMIT)
  git_state.add(str(tmp_path / "dropped"))

  async def execute(update):
    return {"status": "conflict", "version": "1.0", "warnings": []}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  key = owner.submit(request(app_id), db)["id"]
  await drain(owner)
  await owner.refresh_results(key, db)
  assert owner.snapshot(key)["entries"][0]["resolver_chat_id"] == "old-resolver"

  git_state.clear()  # cleared, but the served source never took the commit
  await owner.refresh_results(key, db)
  entry = owner.snapshot(key)["entries"][0]
  assert (entry["status"], entry["error_code"], entry["resolver_chat_id"]) == (
    "failed", "update_not_applied", None,
  )


@pytest.mark.asyncio
async def test_unreadable_progress_is_retried_on_the_next_request(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.root.mkdir()
  path = owner.root / f"{uuid4()}.json"
  path.write_text("interrupted bytes")
  await owner.start()
  assert not owner.accepting
  await owner.retry_start()
  assert not owner.accepting and "retried on the next request" in owner.failure
  path.unlink()  # the owner repaired or removed the evidence
  await owner.retry_start()
  assert owner.accepting and owner.failure is None


@pytest.mark.asyncio
async def test_single_update_transfers_before_the_lock_and_cancels_safely(tmp_path, db, monkeypatch):
  """The shared path: no Git transfer under the lock; a cancel waits for the pass."""
  app_id = add_app(db, tmp_path, "single", version="1.0")
  subprocess.run(["git", "init", "-q", str(tmp_path / "single")], check=True)
  events = []
  in_pass, release = asyncio.Event(), asyncio.Event()

  async def fetch_app_commit(source_dir, ref, **_):
    events.append(("fetch", fs_locks.install_uninstall_lock().locked()))
    return ref

  async def locked_pass():
    in_pass.set()
    await release.wait()
    events.append(("pass settled", fs_locks.install_uninstall_lock().locked()))

  async def install_from_manifest(session, **kwargs):
    return await install._settle_before_cancel(locked_pass())

  monkeypatch.setattr(install, "fetch_app_commit", fetch_app_commit)
  monkeypatch.setattr(install, "install_from_manifest", install_from_manifest)
  update = request(app_id).updates[0].model_dump()
  task = asyncio.create_task(batches.install_reviewed_update(update))
  await in_pass.wait()
  task.cancel()
  await asyncio.sleep(0.05)
  assert not task.done()
  assert fs_locks.install_uninstall_lock().locked()
  release.set()
  with pytest.raises(asyncio.CancelledError):
    await task
  assert events == [("fetch", False), ("pass settled", True)]
  assert not fs_locks.install_uninstall_lock().locked()


@pytest.mark.asyncio
async def test_database_error_at_boot_disables_only_batches(tmp_path, monkeypatch):
  """Settling old batches can fail; readiness for everything else must hold."""
  from sqlalchemy.exc import OperationalError

  from app import runtime_supervisors
  from app.runtime_supervisors import RuntimeSupervisors

  runtime = RuntimeSupervisors(settings=SimpleNamespace(data_dir=str(tmp_path)),
    logger=logging.getLogger(__name__), restart_authorization=None,
    restart_fallback_chats=[])
  body = request(1)
  key = str(body.id)
  runtime.app_updates.root.mkdir(parents=True)
  (runtime.app_updates.root / f"{key}.json").write_text(json.dumps({
    "id": key, "created_at": _now(), "updated_at": _now(), "phase": "applying",
    "updates": [u.model_dump() for u in body.updates],
    "entries": [{"app_id": 1, "status": "applying"}],
  }))

  def database_down(reviewed):
    raise OperationalError("SELECT", {}, Exception("database is locked"))

  async def required_owners():
    for name in runtime_supervisors.REQUIRED_DATABASE_SUPERVISORS:
      runtime._spawn(name, asyncio.Event().wait())

  monkeypatch.setattr(batches, "_settled_outcomes", database_down)
  monkeypatch.setattr(runtime, "_start_chat_supervisors", required_owners)
  try:
    await runtime.start_database_services()
    assert runtime.database_service_readiness()[0] is True
    assert not runtime.app_updates.accepting
    with pytest.raises(HTTPException) as unavailable:
      runtime.app_updates.submit(request(2), None)
    assert unavailable.value.status_code == 503
    assert "could not be read" in unavailable.value.detail
    # The receipt is evidence, never rewritten by a failed settlement.
    assert json.loads((runtime.app_updates.root / f"{key}.json").read_text())["phase"] == "applying"
  finally:
    await runtime.stop()


def test_progress_stream_route_sends_snapshots_and_ends_when_complete(
  client, owner_token, monkeypatch, tmp_path,
):
  from app.main import app as application

  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.accepting = True
  owner.root.mkdir(parents=True)
  key = str(uuid4())
  owner._save({"id": key, "created_at": _now(), "updates": [],
    "entries": [{"app_id": 1, "status": "updated", "name": "One"}]})
  monkeypatch.setattr(application.state, "runtime_supervisors",
    SimpleNamespace(app_updates=owner), raising=False)
  headers = {"Authorization": f"Bearer {owner_token}"}
  with client.stream("GET", f"/api/apps/update-batches/{key}/events", headers=headers) as response:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in response.iter_lines() if line]
  assert len(frames) == 1 and frames[0].startswith("data: ")
  snapshot = json.loads(frames[0][len("data: "):])
  assert (snapshot["id"], snapshot["phase"], snapshot["entries"][0]["status"]) == (
    key, "complete", "updated",
  )
  assert not owner.listeners
  assert client.get(f"/api/apps/update-batches/{uuid4()}/events", headers=headers).status_code == 404
  listed = client.get("/api/apps/update-batches", headers=headers)
  assert [b["id"] for b in listed.json()["batches"]] == [key]


def test_landed_rule_against_a_real_repository(tmp_path):
  repo = tmp_path / "repo"
  subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

  def commit(message):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
      "commit", "-q", "--allow-empty", "-m", message], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
      capture_output=True, text=True, check=True).stdout.strip()

  base = commit("base")
  reviewed = commit("reviewed upstream")
  resolved = commit("resolver answer")
  subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "other", base], check=True)
  unrelated = commit("served without the update")

  assert batches._reviewed_commit_landed(str(repo), reviewed, resolved) is True
  assert batches._reviewed_commit_landed(str(repo), reviewed, reviewed) is True
  assert batches._reviewed_commit_landed(str(repo), reviewed, unrelated) is False
  assert batches._reviewed_commit_landed(str(repo), reviewed, base) is False
  # A pending-update receipt means the answer is not installed yet.
  receipt = install.pending_update_receipt_file(repo)
  receipt.parent.mkdir(parents=True)
  receipt.write_text("{}")
  assert batches._reviewed_commit_landed(str(repo), reviewed, resolved) is False
