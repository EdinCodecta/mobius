"""Retired chat-attached contribution routes stay compatible with old callers.

Chat Changes no longer prepares, sends or settles work. A released Contribute
still runs ``settle_chat_changes.py`` (which exits 1 on any HTTP error) and an
already-loaded shell may still call the old Changes actions, so the platform
keeps an authenticated, app-scoped facade that changes nothing and says so.
"""
import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from app import auth, models
from app.database import SessionLocal
from tests.test_apps import create_local_app

FIXTURE = Path(__file__).parent / "fixtures" / "contribute_settle_chat_changes_19beea8.py"
CHAT_ID = "chat-a"


@pytest.fixture
def contribute(client, owner_token):
  owner_headers = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(
    client, owner_headers, name="contribute-test", description="t",
  )["id"]
  with SessionLocal() as db:
    db.get(models.App, app_id).github_access = True
    db.commit()
    owner = db.query(models.Owner).one()
    agent_token = auth.create_agent_token(
      CHAT_ID, owner.username, owner.token_epoch,
    )
  app_token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=owner_headers,
  ).json()["token"]
  return app_id, agent_token, app_token


def _load_shipped_settle_script(client, monkeypatch):
  spec = importlib.util.spec_from_file_location("shipped_settle", FIXTURE)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)

  class _Response(io.BytesIO):
    def __enter__(self):
      return self

    def __exit__(self, *_exc):
      self.close()

  def urlopen(request, timeout):
    response = client.request(
      request.get_method(),
      request.full_url.removeprefix("http://mobius.test"),
      headers=dict(request.header_items()),
      content=request.data,
    )
    if response.status_code >= 400:
      raise HTTPError(
        request.full_url, response.status_code, "error", {},
        io.BytesIO(response.content),
      )
    return _Response(response.content)

  monkeypatch.setattr(module, "urlopen", urlopen)
  return module


def test_shipped_contribute_settle_step_succeeds_without_recording(
  client, contribute, monkeypatch, capsys,
):
  app_id, agent_token, _app_token = contribute
  script = _load_shipped_settle_script(client, monkeypatch)
  monkeypatch.setenv("API_BASE_URL", "http://mobius.test")
  monkeypatch.setenv("AGENT_TOKEN", agent_token)
  monkeypatch.setenv("CHAT_ID", CHAT_ID)

  exit_code = script.main([
    "/data/platform/frontend/src/example.js",
    "--app-id", str(app_id),
    "--through", "1787800000000",
    "--disposition", "experimental",
    "--summary", "Kept as a local experiment.",
  ])

  captured = capsys.readouterr()
  assert exit_code == 0, captured.err
  assert json.loads(captured.out)["settled"] == 0


def test_settle_facade_is_a_truthful_scoped_no_op(client, contribute):
  app_id, agent_token, app_token = contribute
  url = f"/api/github/contributions/{app_id}/for-chat/{CHAT_ID}/settle"
  body = {"coverage_at": 1, "items": [{"path": "/data/platform/a.js"}]}

  ok = client.post(url, json=body, headers={"Authorization": f"Bearer {agent_token}"})
  assert ok.status_code == 200, ok.text
  payload = ok.json()
  assert payload["retired"] is True and payload["settlements"] == []
  assert "nothing was recorded" in payload["notice"]

  # Same authority as the removed route: owner bearer only, own app, valid id.
  assert client.post(url, json=body).status_code == 401
  assert client.post(
    url, json=body, headers={"Authorization": f"Bearer {app_token}"},
  ).status_code == 403
  assert client.post(
    f"/api/github/contributions/{app_id + 999}/for-chat/{CHAT_ID}/settle",
    json=body, headers={"Authorization": f"Bearer {agent_token}"},
  ).status_code == 404
  assert client.post(
    f"/api/github/contributions/{app_id}/for-chat/-bad/settle",
    json=body, headers={"Authorization": f"Bearer {agent_token}"},
  ).status_code == 422


@pytest.mark.parametrize(("method", "suffix"), [
  ("GET", ""),
  ("GET", "/work/history"),
  ("POST", "/work"),
  ("POST", "/work/stop"),
  ("POST", "/coverage"),
])
def test_old_shell_changes_actions_are_refused_without_starting_work(
  client, contribute, owner_token, method, suffix,
):
  app_id, _agent_token, app_token = contribute
  url = f"/api/github/contributions/{app_id}/for-chat/{CHAT_ID}{suffix}"
  with SessionLocal() as db:
    before = db.query(models.Delegation).count()

  response = client.request(
    method, url, json={"intent": "prepare"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 410, response.text
  detail = response.json()["detail"]
  assert detail["retired"] is True and detail["started"] is False
  assert "Nothing was started" in detail["notice"]
  with SessionLocal() as db:
    assert db.query(models.Delegation).count() == before
  assert client.request(
    method, url, headers={"Authorization": f"Bearer {app_token}"},
  ).status_code == 403
