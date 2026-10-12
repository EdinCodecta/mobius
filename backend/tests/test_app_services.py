"""Accepted app services own policy; the platform owns their hard boundary."""

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import app_services, auth as auth_tokens, models
from app.applied_app_runtime import runtime_parent
from app.config import get_settings


def test_capabilities_document_cross_lane_concurrency():
  contract = (Path(__file__).parents[2] / "CAPABILITIES.md").read_text()
  assert "Private and public requests use separate serialized lanes" in contract
  assert "the app must\nprovide its own file or database locking" in contract


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_at", ["app", "global"])
async def test_queued_service_pins_runtime_until_cancelled(monkeypatch, blocked_at):
  class Gate(asyncio.Semaphore):
    def __init__(self):
      super().__init__(0)
      self.waiting = asyncio.Event()

    async def acquire(self):
      self.waiting.set()
      return await super().acquire()

  gate = Gate()
  events = []
  monkeypatch.setattr(app_services, "_app_slots", {
    (1, "private"): gate if blocked_at == "app" else asyncio.Semaphore(1),
  })
  monkeypatch.setattr(app_services, "_global_slots", {
    "private": gate if blocked_at == "global" else asyncio.Semaphore(1),
    "public": asyncio.Semaphore(1),
  })
  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})

  def pin(_app_id):
    events.append("pinned")
    return SimpleNamespace(close=lambda: events.append("released"))

  monkeypatch.setattr(app_services, "hold_runtime", pin)
  task = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1), None, {}))
  try:
    await asyncio.wait_for(gate.waiting.wait(), timeout=1)
    assert events == ["pinned"], "queued old-runtime requests must prevent a false drain verdict"
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert events == ["pinned", "released"]


@pytest.mark.asyncio
async def test_one_apps_backlog_does_not_take_other_apps_execution_slots(monkeypatch):
  class BusyApp(asyncio.Semaphore):
    def __init__(self):
      super().__init__(0)
      self.waiting = asyncio.Event()

    async def acquire(self):
      self.waiting.set()
      return await super().acquire()

  busy = BusyApp()
  monkeypatch.setattr(app_services, "_app_slots", {(1, "private"): busy})
  monkeypatch.setattr(app_services, "_global_slots", {
    "private": asyncio.Semaphore(1),
    "public": asyncio.Semaphore(1),
  })
  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(close=lambda: None))

  def entered(app, _contract):
    assert app.id == 2
    raise HTTPException(418, "second app admitted")

  monkeypatch.setattr(app_services, "service_entry", entered)
  first = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1), None, {}))
  try:
    await asyncio.wait_for(busy.waiting.wait(), timeout=1)
    with pytest.raises(HTTPException, match="second app admitted"):
      await asyncio.wait_for(
        app_services.invoke_service(SimpleNamespace(id=2), None, {}), timeout=1,
      )
  finally:
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_at", ["app", "global"])
async def test_public_callback_does_not_queue_behind_private_request(monkeypatch, blocked_at):
  class BusyPrivateLane(asyncio.Semaphore):
    def __init__(self):
      super().__init__(0)
      self.waiting = asyncio.Event()

    async def acquire(self):
      self.waiting.set()
      return await super().acquire()

  busy = BusyPrivateLane()
  monkeypatch.setattr(app_services, "_app_slots", {
    (1, "private"): busy if blocked_at == "app" else asyncio.Semaphore(1),
  })
  monkeypatch.setattr(app_services, "_global_slots", {
    "private": busy if blocked_at == "global" else asyncio.Semaphore(1),
    "public": asyncio.Semaphore(1),
  })
  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(close=lambda: None))

  def entered(app, _contract):
    assert app.id == 1
    raise HTTPException(418, "public callback admitted")

  monkeypatch.setattr(app_services, "service_entry", entered)
  private = asyncio.create_task(
    app_services.invoke_service(SimpleNamespace(id=1), None, {"public": False}),
  )
  try:
    await asyncio.wait_for(busy.waiting.wait(), timeout=1)
    with pytest.raises(HTTPException, match="public callback admitted"):
      await asyncio.wait_for(
        app_services.invoke_service(SimpleNamespace(id=1), None, {"public": True}),
        timeout=1,
      )
  finally:
    private.cancel()
    await asyncio.gather(private, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_during_spawn_reaps_process_before_releasing_runtime(monkeypatch, tmp_path):
  entry = tmp_path / "service.py"
  entry.write_text("import time; time.sleep(60)")
  created = asyncio.Event()
  finish_spawn = asyncio.Event()
  processes = []
  released = []
  real_spawn = asyncio.create_subprocess_exec

  async def delayed_spawn(*args, **kwargs):
    process = await real_spawn(*args, **kwargs)
    processes.append(process)
    created.set()
    await finish_spawn.wait()
    return process

  monkeypatch.setattr(app_services, "service_contract", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "service_entry", lambda *a: entry)
  monkeypatch.setattr(app_services, "service_environment", lambda *a, **k: {})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(
    close=lambda: released.append(processes[0].returncode),
  ))
  monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
  task = asyncio.create_task(app_services.invoke_service(SimpleNamespace(id=1), None, {}))
  try:
    await asyncio.wait_for(created.wait(), timeout=3)
    task.cancel()
    await asyncio.sleep(0)  # Deliver cancellation precisely inside admission.
    task.cancel()  # A shutdown or timeout can cancel cleanup again.
    await asyncio.sleep(0)
    assert not task.done()
    assert not released
    finish_spawn.set()
    with pytest.raises(asyncio.CancelledError):
      await asyncio.wait_for(task, timeout=3)
    assert released and released[0] is not None, "runtime released before child was reaped"
    assert processes[0].returncode is not None
  finally:
    finish_spawn.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for process in processes:
      if process.returncode is None:
        os.killpg(process.pid, signal.SIGKILL)
      await process.wait()


SERVICE = b'''import json, sys
request = json.load(sys.stdin)
print(json.dumps({
  "status": 201,
  "body": {
    "method": request["method"],
    "path": request["path"],
    "query": request["query"],
    "body": request["body"],
    "scope": request["actor"]["scope"],
    "app_slug": request["actor"].get("app_slug"),
  },
  "headers": {"Content-Language": "en"},
}))
'''


def _service_app(
  db, *, access="self", slug="service-test", service_id=None, aliases=(),
  service_bytes=SERVICE, max_bytes=8 * 1024 * 1024, route_limits=None,
):
  source = Path(get_settings().data_dir) / "apps" / slug
  source.mkdir(parents=True)
  service = {
    "entry": "service.py",
    "access": access,
    "protocol": "json-v1",
    "max_request_bytes": max_bytes,
    "max_response_bytes": max_bytes,
  }
  if route_limits is not None:
    service["route_limits"] = route_limits
  app = models.App(
    name="Service test",
    slug=slug,
    description="",
    source_dir=str(source),
    jsx_source="export default () => null",
    capability_contract={"schema": 6, "service": service},
    service_id=service_id,
  )
  db.add(app)
  db.commit()
  for alias in aliases:
    db.add(models.AppServiceAlias(service_id=alias, app_id=app.id))
  db.commit()
  revision = "a" * 64
  accepted = runtime_parent(app.id) / revision
  accepted.mkdir(parents=True)
  (accepted / "service.py").write_bytes(service_bytes)
  app.runtime_revision = revision
  db.commit()
  return app


def test_service_request_body_uses_accepted_limit_before_buffering(client, auth, db, monkeypatch):
  from app.routes import app_services as routes
  app = _service_app(db, max_bytes=1024)
  seen = []
  original = routes.read_capped_body

  async def tracked(request, limit, **kwargs):
    seen.append(limit)
    return await original(request, limit, **kwargs)

  monkeypatch.setattr(routes, "read_capped_body", tracked)
  denied = client.post(
    f"/api/apps/{app.id}/service/echo", headers=auth,
    content=b'"' + b'x' * 1024 + b'"',
  )
  assert denied.status_code == 413
  assert seen == [1024]
  allowed = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={})
  assert allowed.status_code == 201


def test_service_default_eight_mib_still_rejects_oversize_body(client, auth, db):
  app = _service_app(db)
  denied = client.post(
    f"/api/apps/{app.id}/service/echo", headers=auth,
    content=b'"' + b'x' * (8 * 1024 * 1024) + b'"',
  )
  assert denied.status_code == 413


def test_reviewed_service_accepts_request_above_default_eight_mib(client, auth, db):
  service = b'''import json, sys
request = json.load(sys.stdin)
print(json.dumps({"body": {"length": len(request["body"]["payload"])}}))
'''
  app = _service_app(db, service_bytes=service, max_bytes=60 * 1024 * 1024)
  response = client.post(
    f"/api/apps/{app.id}/service/echo", headers=auth,
    json={"payload": "x" * (8 * 1024 * 1024)},
  )
  assert response.status_code == 200
  assert response.json() == {"length": 8 * 1024 * 1024}


def test_binary_response_budget_includes_base64_envelope(client, auth, db):
  service = b'''import base64, json, sys
json.load(sys.stdin)
print(json.dumps({"body_base64": base64.b64encode(b"x" * 1536).decode(), "media_type": "image/gif"}))
'''
  small = _service_app(db, slug="binary-small", service_bytes=service, max_bytes=2048)
  large = _service_app(db, slug="binary-large", service_bytes=service, max_bytes=4096)
  denied = client.get(f"/api/apps/{small.id}/service/echo", headers=auth)
  allowed = client.get(f"/api/apps/{large.id}/service/echo", headers=auth)
  assert denied.status_code == 503
  assert allowed.status_code == 200
  assert allowed.content == b"x" * 1536


@pytest.mark.asyncio
async def test_preload_and_tool_lane_receive_the_reviewed_output_budget(db, auth, monkeypatch):
  from app import service_preload
  app = _service_app(db, max_bytes=12 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  seen = []
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: object())

  async def run(_host, _environment, _request, **kwargs):
    seen.append(kwargs["max_stdout"])
    return b'{"body": {"ok": true}}', b"", 0

  monkeypatch.setattr(service_preload, "run", run)
  status, body, _headers, _media = await app_services.invoke_service(
    app, owner, {"actor": {}, "public": False}, lane="tools",
  )
  assert (status, body, seen) == (200, {"ok": True}, [12 * 1024 * 1024])


@pytest.mark.asyncio
async def test_dense_tool_request_serialization_uses_bounded_buffer(db, auth, monkeypatch):
  import tracemalloc
  from app import service_preload

  app = _service_app(db, max_bytes=60 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  envelope = {"actor": {}, "body": [0] * 200_000, "caption": "café"}
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: object())

  async def run(_host, _environment, request, **kwargs):
    # Input allocation is excluded; serialization must not retain one object
    # per JSON token. Leave headroom for encoding and normal invocation setup.
    _current, peak = tracemalloc.get_traced_memory()
    assert peak < len(request) * 8
    assert json.loads(request) == envelope
    return b'{"body": {"ok": true}}', b"", 0

  monkeypatch.setattr(service_preload, "run", run)
  tracemalloc.start()
  try:
    status, body, _headers, _media = await app_services.invoke_service(
      app, owner, envelope, lane="tools",
    )
  finally:
    tracemalloc.stop()
  assert (status, body) == (200, {"ok": True})


@pytest.mark.asyncio
async def test_tool_lane_rejects_oversize_request_before_execution(db, auth, monkeypatch):
  app = _service_app(db, max_bytes=1024)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(app_services, "service_entry", lambda *_: pytest.fail("service executed"))
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(
      app, owner, {"actor": {}, "body": {"arguments": {"payload": "x" * 1024}}},
      lane="tools",
    )
  assert caught.value.status_code == 413


@pytest.mark.asyncio
async def test_oversize_nested_tool_string_rejects_without_scalar_copy_or_execution(
  db, auth, monkeypatch,
):
  import tracemalloc

  app = _service_app(db, max_bytes=1024)
  owner = db.query(models.Owner).first()
  envelope = {"actor": {}, "body": {"arguments": {"nested": ["\"" * (8 * 1024 * 1024)]}}}
  monkeypatch.setattr(app_services, "service_entry", lambda *_: pytest.fail("service executed"))
  tracemalloc.start()
  try:
    with pytest.raises(HTTPException) as caught:
      await app_services.invoke_service(app, owner, envelope, lane="tools")
    _current, peak = tracemalloc.get_traced_memory()
  finally:
    tracemalloc.stop()
  assert caught.value.status_code == 413
  assert peak < 2 * 1024 * 1024, "oversize scalar was escaped and UTF-8 encoded before admission"


@pytest.mark.asyncio
@pytest.mark.parametrize("scalar", ["\x00" * 512, "\"" * 512, "é" * 512])
async def test_tool_string_escape_or_utf8_expansion_counts_toward_limit(
  db, auth, monkeypatch, scalar,
):
  app = _service_app(db, max_bytes=1024)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(app_services, "service_entry", lambda *_: pytest.fail("service executed"))
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(
      app, owner, {"actor": {}, "body": {scalar: "ok"}}, lane="tools",
    )
  assert caught.value.status_code == 413


@pytest.mark.asyncio
async def test_valid_nested_tool_json_bytes_and_invalid_data_errors_are_unchanged(
  db, auth, monkeypatch,
):
  from app import service_preload

  app = _service_app(db, max_bytes=1024)
  owner = db.query(models.Owner).first()
  envelope = {"actor": {}, "body": {"café\"": ["\x00" * 20, "雪" * 20]}}
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: object())

  async def run(_host, _environment, request, **kwargs):
    assert request == json.dumps(
      envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return b'{"body": {"ok": true}}', b"", 0

  monkeypatch.setattr(service_preload, "run", run)
  status, body, _headers, _media = await app_services.invoke_service(
    app, owner, envelope, lane="tools",
  )
  assert (status, body) == (200, {"ok": True})
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(
      app, owner, {"actor": {}, "body": {"bad": object()}}, lane="tools",
    )
  assert caught.value.status_code == 400


def test_authenticated_service_receives_one_bounded_json_envelope(
  client, auth, db,
):
  app = _service_app(db)

  response = client.post(
    f"/api/apps/{app.id}/service/review/decision?state=ready&state=fresh",
    headers=auth,
    json={"record": "abc"},
  )

  assert response.status_code == 201
  assert response.headers["content-language"] == "en"
  assert response.json() == {
    "method": "POST",
    "path": "review/decision",
    "query": {"state": ["ready", "fresh"]},
    "body": {"record": "abc"},
    "scope": "owner",
    "app_slug": None,
  }


SERVICE_ENV = b'''import json, os, sys
json.load(sys.stdin)
print(json.dumps({
  "status": 200,
  "body": {
    "provider_env": sorted(
      key for key in ("DATA_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME")
      if key in os.environ
    ),
  },
}))
'''


def test_only_authenticated_services_receive_provider_credentials(
  client, auth, db, monkeypatch,
):
  monkeypatch.setenv("DATA_DIR", "/data")
  monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/data/cli-auth/claude")
  monkeypatch.setenv("CODEX_HOME", "/data/cli-auth/codex")
  private = _service_app(
    db, slug="recall-navigator", service_bytes=SERVICE_ENV,
  )
  public = _service_app(
    db, access="public", slug="public-echo", service_bytes=SERVICE_ENV,
  )
  everything = ["CLAUDE_CONFIG_DIR", "CODEX_HOME", "DATA_DIR"]

  # A private service the owner reaches may run a provider CLI, so it receives
  # the same credential locations as its scheduled job.
  private_call = client.post(
    f"/api/apps/{private.id}/service/echo", headers=auth, json={},
  )
  assert private_call.json()["provider_env"] == everything

  # An anonymous caller of a public service gets none of them.
  anonymous_call = client.post("/api/app-services/public-echo/echo", json={})
  assert anonymous_call.json()["provider_env"] == []

  # Nor does the owner's own authenticated call to a publicly reachable service:
  # the credentials follow the reviewed access, not the current caller.
  owner_call = client.post(
    f"/api/apps/{public.id}/service/echo", headers=auth, json={},
  )
  assert owner_call.json()["provider_env"] == []


def test_public_service_requires_an_explicit_reviewed_grant(client, auth, db):
  private = _service_app(db, slug="private-service")
  public = _service_app(db, access="public", slug="public-service")

  denied = client.get("/api/app-services/private-service/status")
  allowed = client.get("/api/app-services/public-service/status")

  assert denied.status_code == 404
  assert allowed.status_code == 201
  assert allowed.json()["scope"] == "public"
  assert private.id != public.id


def test_app_services_have_no_app_specific_route_aliases(client, auth, db):
  _service_app(db, access="public", slug="common")

  canonical = client.get("/api/app-services/common/status")
  legacy = client.get("/api/common/status")

  assert canonical.status_code == 201, canonical.text
  assert legacy.status_code == 404


def test_public_service_uses_stable_identity_without_leaving_slug_alias(
  client, auth, db,
):
  _service_app(
    db, access="public", slug="renamed-product", service_id="stable-social",
  )

  stable = client.get("/api/app-services/stable-social/status")
  mutable_slug = client.get("/api/app-services/renamed-product/status")

  assert stable.status_code == 201, stable.text
  assert mutable_slug.status_code == 404


def test_public_service_routes_only_explicit_transition_aliases(
  client, auth, db,
):
  app = _service_app(
    db, access="public", slug="renamed-product", service_id="social",
    aliases=("common",),
  )

  canonical = client.get("/api/app-services/social/status")
  transition = client.get("/api/app-services/common/status")
  product_slug = client.get("/api/app-services/renamed-product/status")

  assert canonical.status_code == 201, canonical.text
  assert transition.status_code == 201, transition.text
  assert product_slug.status_code == 404
  db.query(models.AppServiceAlias).filter_by(app_id=app.id).delete()
  db.commit()
  assert client.get("/api/app-services/common/status").status_code == 404


def test_shared_service_requires_an_explicit_cross_app_grant(
  client, owner_token, db,
):
  owner = db.query(models.Owner).one()
  caller = models.App(
    name="Caller", slug="caller", description="", source_dir="/tmp/caller",
    jsx_source="export default () => null", token_nonce="caller-nonce",
  )
  db.add(caller)
  db.commit()
  private = _service_app(db, slug="private-shared")
  shared = _service_app(db, access="apps", slug="shared-service")
  token = auth_tokens.create_app_token(
    caller.id, owner.username, owner.token_epoch, app_nonce=caller.token_nonce,
  )
  headers = {"Authorization": f"Bearer {token}"}

  denied = client.get("/api/services/private-shared/status", headers=headers)
  allowed = client.get("/api/services/shared-service/status", headers=headers)

  assert denied.status_code == 403
  assert allowed.status_code == 201
  assert allowed.json()["scope"] == "app"
  assert allowed.json()["app_slug"] == "caller"
  assert private.id != shared.id


def test_service_contract_is_bound_to_the_accepted_runtime(client, auth, db):
  app = _service_app(db, slug="missing-entry")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").unlink()

  response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 503
  assert response.json()["detail"] == "Accepted app service entry is unavailable."


def test_service_runtime_stays_pinned_through_process_exit(
  client, auth, db, monkeypatch,
):
  app = _service_app(db, slug="pinned-service")
  state = {"closed": False}

  class Pin:
    def close(self):
      state["closed"] = True

  monkeypatch.setattr(app_services, "hold_runtime", lambda _app_id: Pin())
  response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 201
  assert state["closed"] is True


def test_service_headers_outside_the_allowlist_never_reach_the_response(client, auth, db):
  # A service answers on the shell origin, so a header such as Set-Cookie,
  # Clear-Site-Data, or NEL/Report-To would act on the owner's whole session
  # rather than on this one response. Only the allowlisted names pass.
  app = _service_app(db, slug="header-service")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").write_text(
    "import json\nprint(json.dumps({\"headers\":{"
    "\"Set-Cookie\":\"x=y\",\"NEL\":\"{}\",\"Report-To\":\"{}\","
    "\"Service-Worker-Allowed\":\"/\",\"X-Anything\":\"1\","
    "\"ETag\":\"\\\"v1\\\"\",\"Content-Disposition\":\"inline\"}}))\n"
  )

  response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 200
  for name in ("set-cookie", "nel", "report-to", "service-worker-allowed", "x-anything"):
    assert name not in response.headers
  assert response.headers["etag"] == '"v1"'
  assert response.headers["content-disposition"] == "inline"


@pytest.mark.parametrize("name", [
  "Set-Cookie", "Clear-Site-Data", "Refresh", "Service-Worker-Allowed",
  "NEL", "Report-To", "Content-Type", "Content-Length", "Transfer-Encoding",
])
def test_service_cannot_set_session_transport_or_origin_wide_headers(name):
  assert app_services._response_headers({name: "*"}, public=True) == {}


@pytest.mark.parametrize("value", [
  "public, max-age=60", "max-age=60, s-maxage=600", "max-age=60, must-revalidate",
])
def test_authenticated_service_response_is_never_shared_cacheable(value):
  # A shared cache may store a response to an authorized request only when
  # the response opts in (RFC 9111 section 3.5). An owner-authenticated service
  # response must not.
  assert app_services._response_headers({"Cache-Control": value}, public=False) == {}
  assert app_services._response_headers({"Cache-Control": value}, public=True) == {
    "Cache-Control": value,
  }


@pytest.mark.parametrize("value", ["no-store", "private, max-age=60", "no-cache"])
def test_authenticated_service_keeps_private_cache_policy(value):
  assert app_services._response_headers({"Cache-Control": value}, public=False) == {
    "Cache-Control": value,
  }


def test_service_rejects_a_malformed_allowed_header():
  with pytest.raises(ValueError):
    app_services._response_headers({"ETag": "a\r\nSet-Cookie: x=y"}, public=True)


def test_service_rejects_nonstandard_json_constants(client, auth, db):
  app = _service_app(db, slug="constant-service")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").write_text('print("{\\\"body\\\":NaN}")\n')

  response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 502
  assert response.json()["detail"] == "App service returned invalid JSON."


def test_service_rejects_nonstandard_request_json(client, auth, db):
  app = _service_app(db, slug="request-constant-service")

  response = client.post(
    f"/api/apps/{app.id}/service/status", headers={
      **auth, "Content-Type": "application/json",
    }, content='{"value":NaN}',
  )

  assert response.status_code == 400
  assert response.json()["detail"] == "App service requests must contain JSON."


def test_public_service_failure_does_not_expose_app_diagnostics(client, auth, db):
  app = _service_app(db, access="public", slug="failing-service")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").write_text(
    "import sys\nprint('private app detail', file=sys.stderr)\nraise SystemExit(1)\n"
  )

  response = client.get("/api/app-services/failing-service/status")

  assert response.status_code == 502, response.text
  assert response.json()["detail"] == "App service failed."
  assert "private app detail" not in response.text


def test_handled_service_failure_retains_bounded_diagnostics(
  client, auth, db, caplog,
):
  app = _service_app(db, slug="diagnostic-service")
  accepted = runtime_parent(app.id) / ("a" * 64)
  (accepted / "service.py").write_text(
    "import json,sys\n"
    "print('private traceback detail', file=sys.stderr)\n"
    "print(json.dumps({\"status\":500,\"body\":{\"detail\":\"safe\"}}))\n"
  )

  with caplog.at_level(logging.WARNING, logger="app.app_services"):
    response = client.get(f"/api/apps/{app.id}/service/status", headers=auth)

  assert response.status_code == 500
  assert response.json() == {"detail": "safe"}
  assert "private traceback detail" not in response.text
  assert "private traceback detail" in caplog.text


@pytest.mark.parametrize('route', ['numeric', 'named'])
@pytest.mark.parametrize('caller_kind', ['app', 'owner', 'delegated-owner'])
def test_authenticated_routes_preserve_the_same_complete_actor(
  client, auth, db, route, caller_kind,
):
  from app.deps import Principal, get_principal
  from app.main import app as server

  target = _service_app(db, access='public', slug='stable-actor')
  accepted = runtime_parent(target.id) / target.runtime_revision
  (accepted / 'service.py').write_text(
    'import json,sys\nr=json.load(sys.stdin)\n'
    'print(json.dumps({"status":200,"body":r["actor"]}))\n'
  )
  owner = db.query(models.Owner).one()
  headers = auth
  expected = {'scope':'owner', 'app_id':None, 'app_slug':None, 'delegated':False,
              'access':'write'}
  if caller_kind == 'app':
    token = auth_tokens.create_app_token(
      target.id, owner.username, owner.token_epoch, app_nonce=target.token_nonce,
    )
    headers = {'Authorization':f'Bearer {token}'}
    expected.update(scope='app', app_id=target.id, app_slug=target.slug)
  elif caller_kind == 'delegated-owner':
    from fastapi import Depends
    from app.database import get_db

    def delegated_principal(session=Depends(get_db)):
      return Principal(owner=session.query(models.Owner).one(), app_id=None,
                       delegation_id='fixture-delegation')

    server.dependency_overrides[get_principal] = delegated_principal
    # No delegation row stands behind this bearer, so it may only read.
    expected.update(delegated=True, access='read')
  path = (f'/api/apps/{target.id}/service/actor' if route == 'numeric'
          else '/api/services/stable-actor/actor')
  try:
    response = client.post(path, headers=headers, json={'actor':{'scope':'owner'}})
    assert response.status_code == 200, response.text
    assert response.json() == expected
  finally:
    if caller_kind == 'delegated-owner':
      server.dependency_overrides.pop(get_principal, None)


SERVICE_TOKEN = b'''import json, os, sys
json.load(sys.stdin)
print(json.dumps({"status": 200, "body": {"token": os.environ["APP_TOKEN"]}}))
'''


def test_public_invocation_cannot_spend_on_the_owners_providers(client, auth, db):
  app = _service_app(
    db, access="public", slug="public-token", service_bytes=SERVICE_TOKEN,
  )
  anonymous = client.post("/api/app-services/public-token/echo", json={})
  public = {"Authorization": f"Bearer {anonymous.json()['token']}"}

  # The anonymous visitor's request may not start or drive the owner's agents,
  # nor read what only the app's own authenticated backend may.
  assert client.post("/api/app-chats", headers=public, json={}).status_code == 403
  assert client.post(
    "/api/app-chats/start", headers=public, json={"scope": "x", "prompt": "hi"},
  ).status_code == 403
  assert client.get("/api/app-chats", headers=public).status_code == 403
  assert client.get(
    f"/api/apps/{app.id}/secrets/key", headers=public,
  ).status_code == 403
  # It keeps the few reads and notices a public service is reviewed to use.
  assert client.get("/api/apps/", headers=public).status_code == 200
  assert client.post(
    "/api/notifications/send", headers=public, json={"title": "t", "body": "b"},
  ).status_code == 200

  # The owner's own call to the same service carries the app's full authority.
  owner_call = client.post(
    f"/api/apps/{app.id}/service/echo", headers=auth, json={},
  )
  private = {"Authorization": f"Bearer {owner_call.json()['token']}"}
  assert client.get("/api/app-chats", headers=private).status_code == 200


@pytest.mark.parametrize(
  "path", ["tools/log", "/tools/log", "//tools/log", "./tools/log", "x/../tools/log"],
)
def test_http_callers_cannot_reach_the_platforms_tool_lane(client, auth, db, path):
  app = _service_app(db, slug="tool-forge")
  response = client.post(
    f"/api/apps/{app.id}/service/{path}", headers=auth,
    json={"arguments": {}, "call": {"chat_id": "forged"}},
  )
  assert response.status_code == 404


def test_http_dense_container_decode_peak_includes_input_allocation(
  client, auth, db, monkeypatch,
):
  import tracemalloc

  app = _service_app(db, max_bytes=60 * 1024 * 1024)
  real_decode = app_services.ServiceExchangeAdmission.decode
  measurements = []

  async def decoded_only(_app, _owner, envelope, **_kwargs):
    # Measure the actual HTTP materialization boundary, not the encoder.
    measurements.append(tracemalloc.get_traced_memory()[1])
    return 200, {"count": len(envelope["body"])}, {}, None

  monkeypatch.setattr(app_services, "invoke_service", decoded_only)
  # Reproduce the predecessor's decode at the same HTTP boundary, with only
  # 2.1 MB of input (never a dense 60 MiB/OOM trial).
  async def predecessor_decode(_self, raw):
    return json.loads(raw)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode", predecessor_decode)
  tracemalloc.start()
  try:
    raw = b"[" + b"[]," * 699_999 + b"[]]"
    before = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
    baseline = tracemalloc.get_traced_memory()[1]
  finally:
    tracemalloc.stop()
  assert before.status_code == 200
  assert before.json() == {"count": 700_000}
  assert baseline > 35 * 1024 * 1024
  del raw
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode", real_decode)
  tracemalloc.start()
  try:
    raw = b"[" + b"[]," * 699_999 + b"[]]"
    after = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
    repaired = tracemalloc.get_traced_memory()[1]
  finally:
    tracemalloc.stop()
  assert after.status_code == 413
  assert len(measurements) == 1, "dense JSON reached invocation after the resource scan"
  assert repaired < 12 * 1024 * 1024, "decoded containers were allocated before resource rejection"
  assert app_services._admitted_exchange_cost == 0
  print(f"HTTP decode peak including input: before={baseline}, after={repaired} bytes")


class _BodyBuffer(bytearray):
  """Record the shared body reader's buffer copies and the cost admitted before each."""

  events: list = []

  def extend(self, chunk):
    _BodyBuffer.events.append(("extend", len(self) + len(chunk), app_services._admitted_exchange_cost))
    super().extend(chunk)

  def __bytes__(self):
    _BodyBuffer.events.append(("copy", len(self), app_services._admitted_exchange_cost))
    return bytes(memoryview(self))


@pytest.fixture
def body_buffer(monkeypatch):
  from app import storage_io

  _BodyBuffer.events = []
  monkeypatch.setattr(storage_io, "bytearray", _BodyBuffer, raising=False)
  return _BodyBuffer.events


@pytest.mark.parametrize("route", ["self", "shared", "public"])
def test_http_resource_admission_precedes_each_body_buffer_copy_for_every_route(
  client, auth, db, body_buffer, monkeypatch, route,
):
  app = _service_app(db, access="public")
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 1)
  path = {
    "self": f"/api/apps/{app.id}/service/echo",
    "shared": f"/api/services/{app.slug}/echo",
    "public": f"/api/app-services/{app.slug}/echo",
  }[route]
  response = client.post(path, headers=auth, content=b"{}")
  assert response.status_code == 413
  assert response.json()["detail"] == "App service request admission budget is exhausted."
  assert body_buffer == [], "request body was buffered before admission"
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.parametrize("raw", [
  # Each exceeds its cap by at least one mark or level, nothing more.
  b"[" + b"{}," * (app_services.MAX_JSON_STRUCTURE // 2) + b"{}]",
  b"{" + b'"x":0,' * (app_services.MAX_JSON_STRUCTURE // 3) + b'"x":0}',
  b"[" * (app_services.MAX_JSON_DEPTH + 1) + b"0" + b"]" * (app_services.MAX_JSON_DEPTH + 1),
], ids=["dense-objects", "duplicate-members", "nesting"])
def test_http_structural_limits_reject_before_stdlib_materialization(
  client, auth, db, monkeypatch, raw,
):
  app = _service_app(db)
  real_loads = json.loads

  def guarded_loads(value, *args, **kwargs):
    if isinstance(value, bytes) and value == raw:
      pytest.fail("HTTP input tree materialized")
    return real_loads(value, *args, **kwargs)

  monkeypatch.setattr(app_services.json, "loads", guarded_loads)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
  assert response.status_code == 413
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32", "ascii"])
def test_http_resource_scan_preserves_stdlib_escaped_unicode_and_chunk_boundaries(
  client, auth, db, encoding,
):
  app = _service_app(db)
  # A backslash/escaped quote crosses the incremental scanner's chunk boundary.
  value = {"payload": "x" * 65_522 + '\\"{},[]:雪😀' * 30_000, "nested": [{"é": "\x00"}]}
  raw = json.dumps(value, ensure_ascii=encoding == "ascii").encode(encoding)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
  assert response.status_code == 201
  assert response.json()["body"] == value


@pytest.mark.parametrize("raw", [b'{"x":', b'"\\uZZZZ"', b'"\xff"', b'NaN', b'Infinity', b'{]'])
def test_http_malformed_json_stays_400_and_releases_admission(client, auth, db, raw):
  app = _service_app(db)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
  assert response.status_code == 400
  assert app_services._admitted_exchange_cost == 0


def test_http_decode_cost_rejects_before_materialization(client, auth, db, monkeypatch):
  app = _service_app(db)
  monkeypatch.setattr(app_services, "MAX_JSON_DECODE_COST", 1024)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=b'"' + b'x' * 2048 + b'"')
  assert response.status_code == 413
  assert app_services._admitted_exchange_cost == 0


def _nested(depth, leaf=0):
  for _ in range(depth):
    leaf = [leaf]
  return leaf


def _compact(value, encoding="utf-8", ensure_ascii=False):
  return json.dumps(value, ensure_ascii=ensure_ascii, separators=(",", ":")).encode(encoding)


# Payloads an 8 MiB service accepted before structural admission existed. Each
# exceeds the predecessor's 262,144 marks or depth 64 and fits the budgets.
_LEGACY_DENSE_AND_DEEP = {
  "dense-ints": list(range(300_000)),
  "deep": _nested(70),
  "dense-floats": [i + 0.5 for i in range(300_000)],
  "dense-literals": [True, False, None] * 100_000,
  "dense-empties": [[], {}] * 110_000,
  "wide-object": {f"k{i}": i for i in range(100_000)},
  "dense-unicode": ["é雪😀\n"] * 140_000,
}


@pytest.mark.parametrize("shape", sorted(_LEGACY_DENSE_AND_DEEP))
def test_http_legacy_dense_and_deep_payloads_round_trip_within_unchanged_budgets(
  client, auth, db, shape,
):
  value = _LEGACY_DENSE_AND_DEEP[shape]
  service = b'''import json, sys
print(json.dumps({"body": json.load(sys.stdin)["body"]}))
'''
  app = _service_app(db, access="public", service_bytes=service)
  raw = _compact(value)
  assert len(raw) <= 8 * MIB
  response = client.post(f"/api/app-services/{app.slug}/echo", content=raw)
  assert response.status_code == 200
  assert response.json() == value
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["tool", "policy"])
@pytest.mark.parametrize("shape", ["dense-ints", "deep", "wide-object"])
async def test_materialized_legacy_payloads_reach_the_service_unchanged(
  db, auth, monkeypatch, caller, shape,
):
  from app import service_preload

  value = _LEGACY_DENSE_AND_DEEP[shape]
  app = _service_app(db)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)

  async def reply(_python, _entry, _environment, request, _timeout, _max_stdout):
    received = json.loads(request)["body"]
    assert (received["arguments"] if caller == "tool" else received) == value
    return b'{"body":{"ok":true}}', b"", 0

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  if caller == "tool":
    status, result, _headers, _media = await app_services.invoke_service(app, owner, {
      "schema": 1, "method": "POST", "path": "/tools/echo", "query": {}, "headers": {},
      "body": {"arguments": value, "call": {}}, "public": False, "actor": {},
    }, lane="tools")
    assert status == 200
  else:
    result = await app_services.invoke_policy(app, owner, "echo", value)
  assert result == {"ok": True}
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["tool", "policy"])
@pytest.mark.parametrize("excess", ["marks", "depth"])
async def test_materialized_envelope_past_the_structural_bounds_rejects_before_serialization(
  db, auth, monkeypatch, caller, excess,
):
  app = _service_app(db, max_bytes=60 * MIB)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(
    app_services, "_serialize_request", lambda *_: pytest.fail("request serialized"),
  )
  monkeypatch.setattr(app_services, "service_entry", lambda *_: pytest.fail("service executed"))
  # The envelope is depth 0, so its body nests exactly as an HTTP body does.
  body = (
    [0] * app_services.MAX_JSON_STRUCTURE if excess == "marks"
    else {"value": _nested(app_services.MAX_JSON_DEPTH)}
  )
  with pytest.raises(HTTPException) as caught:
    if caller == "tool":
      await app_services.invoke_service(app, owner, {
        "schema": 1, "method": "POST", "path": "/tools/echo", "query": {},
        "headers": {}, "body": body, "public": False, "actor": {},
      }, lane="tools")
    else:
      await app_services.invoke_policy(app, owner, "echo", body)
  assert caught.value.status_code == 413
  assert caught.value.detail == "App service request structural resource limit exceeded."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_retained_depth_matches_the_lexical_scan_at_its_exact_bound():
  deepest = _nested(app_services.MAX_JSON_DEPTH)
  with app_services.ServiceExchangeAdmission(MIB) as admission:
    body = await admission.decode(_compact(deepest))
    await admission.retain({"body": body})
    assert admission.request_cost > 0
  with pytest.raises(HTTPException):
    with app_services.ServiceExchangeAdmission(MIB) as admission:
      await admission.retain({"body": [deepest]})
  assert app_services._admitted_exchange_cost == 0


def _resource_corpus():
  n = 40_000
  return {
    "small-ints": b"[" + b"0," * n + b"0]",
    "large-ints": _compact([2 ** 62 + i for i in range(n)]),
    "long-int": _compact(int("9" * 4000)),
    "floats": _compact([i + 0.5 for i in range(n)]),
    "float-render-expansion": b"[" + b"1e15," * n + b"1e15]",
    "literals": _compact([True, False, None] * (n // 3)),
    "empty-lists": b"[" + b"[]," * n + b"[]]",
    "empty-objects": b"[" + b"{}," * n + b"{}]",
    "one-member-objects": _compact([{"k": i} for i in range(n)]),
    "wide-object-distinct-keys": _compact({f"key{i}": i for i in range(n)}),
    "duplicate-keys": b"{" + b'"x":0,' * n + b'"x":0}',
    "deep-at-bound": _compact(_nested(app_services.MAX_JSON_DEPTH)),
    "nested-lists": _compact([[i] for i in range(n)]),
    "ascii-strings": _compact([f"s{i}" for i in range(n)]),
    "escapes": _compact(['a"b\\c\n\t'] * n),
    "unicode-escapes": _compact(["é雪😀"] * n, ensure_ascii=True),
    "mixed-unicode": _compact(["é雪😀"] * n),
    "astral-then-escape": _compact("😀" * 100_000 + "\n"),
    "ascii-then-astral-escape": _compact("x" * 400_000 + "\n😀"),
    "bmp-then-astral": _compact("x" * 400_000 + "雪😀"),
    "astral-only": _compact("x" * 400_000 + "😀"),
    "latin1": _compact("é" * 200_000),
    "utf-16": _compact(["雪😀", "x"] * n, "utf-16"),
    "utf-32": _compact(["é", 1.5] * n, "utf-32"),
    "base64-media": _compact({"items": ["QUJD" * 250_000] * 2, "caption": "😀"}),
    "pretty-printed": json.dumps({"rows": [{"id": i, "v": [i]} for i in range(n // 4)]}, indent=4).encode(),
  }


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_resource_corpus()))
async def test_decode_estimates_cover_measured_stdlib_allocation_peaks(shape):
  """Estimates are upper bounds of what stdlib JSON actually allocates.

  The raw bytes are excluded (transfer owns them). A request's decode must
  cover json.loads' peak and retain() its surviving tree. A response's must
  also cover that tree while it renders; three stdout copies (transfer) cover
  the rendered text and bytes.
  """
  import gc
  import tracemalloc
  from starlette.responses import JSONResponse

  raw = _resource_corpus()[shape]
  with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
    await admission.decode_response(raw)
    response_estimate = admission.response_cost
  with app_services.ServiceExchangeAdmission(60 * MIB) as admission:
    value = await admission.decode(raw)
    request_estimate = admission.request_cost
    await admission.retain(value)
    retained_estimate = admission.request_cost
  del value
  gc.collect()
  tracemalloc.start()
  try:
    value = json.loads(raw)
    tree, decode_peak = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    rendered = JSONResponse(value).body
    _current, render_peak = tracemalloc.get_traced_memory()
  finally:
    tracemalloc.stop()
  assert decode_peak <= request_estimate <= response_estimate
  assert tree <= retained_estimate
  assert render_peak <= response_estimate + 3 * len(raw)
  assert json.loads(rendered) == value
  print(
    f"{shape}: {len(raw)} bytes, decode peak {decode_peak} <= {request_estimate}, "
    f"tree {tree} <= retained {retained_estimate}, "
    f"render peak {render_peak} <= {response_estimate + 3 * len(raw)}"
  )
  assert app_services._admitted_exchange_cost == 0


def test_http_unicode_decode_widening_beyond_budget_rejects_before_materialization(
  client, auth, db, monkeypatch,
):
  # A BMP character then an astral one makes stdlib decoding hold a UCS2 and a
  # UCS4 buffer sized by the byte count at once: six times this ~59 MB body,
  # beyond the unchanged 320 MiB decode budget, before any tree is built.
  app = _service_app(db, max_bytes=60 * MIB)
  encoded = "QUJD" * (7 * MIB)
  raw = _compact({"items": [encoded, encoded], "caption": "雪😀"})
  assert 5 * len(raw) <= app_services.MAX_JSON_DECODE_COST < 6 * len(raw)
  real_loads = json.loads

  def guarded_loads(value, *args, **kwargs):
    if isinstance(value, bytes) and len(value) == len(raw):
      pytest.fail("over-budget body reached the stdlib decoder")
    return real_loads(value, *args, **kwargs)

  monkeypatch.setattr(app_services.json, "loads", guarded_loads)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, content=raw)
  assert response.status_code == 413
  assert response.json()["detail"] == "App service request decoded resource limit exceeded."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_actual_public_http_dense_payload_at_the_mark_bound_does_not_starve_heartbeat(db, auth):
  import httpx
  import time
  from app.main import app as application

  service = b'''import json, sys
print(json.dumps({"body": json.load(sys.stdin)["body"]}, separators=(",", ":")))
'''
  app = _service_app(db, access="public", service_bytes=service)
  # Each "5," is one mark plus two characters outside strings (1/12 mark);
  # the envelope adds a few dozen marks and the response wrapper three.
  value = [5] * ((app_services.MAX_JSON_STRUCTURE - 64) * 12 // 13)
  raw = _compact(value)
  beats = []
  running = True

  async def heartbeat():
    while running:
      beats.append(time.monotonic())
      await asyncio.sleep(0.01)

  beat = asyncio.create_task(heartbeat())
  try:
    await asyncio.sleep(0.02)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
      response = await http.post(f"/api/app-services/{app.slug}/echo", content=raw)
    await asyncio.sleep(0.02)
  finally:
    running = False
    await beat
  assert response.status_code == 200
  assert response.json() == value
  gap = max(b - a for a, b in zip(beats, beats[1:]))
  print(f"Actual public {len(raw)}-byte {len(value)}-element round trip maximum 10ms heartbeat gap: {gap:.4f}s")
  assert gap < 0.2, "a dense exchange monopolized the event loop"
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.parametrize("shape", ["scalar", "original", "gallery"])
@pytest.mark.parametrize("public", [False, True], ids=["private", "public"])
def test_http_reviewed_media_allowance_preserves_twenty_mib_gif_and_gallery(
  client, auth, db, shape, public,
):
  import base64

  service = b'''import base64, hashlib, json, sys
body = json.load(sys.stdin)["body"]
items = [body] if isinstance(body, str) else body["items"]
print(json.dumps({"body": [{"size": len(base64.b64decode(item)), "sha256": hashlib.sha256(base64.b64decode(item)).hexdigest()} for item in items]}))
'''
  import hashlib
  app = _service_app(db, access="public", service_bytes=service, max_bytes=60 * 1024 * 1024)
  gif = b"GIF89a" + b"x" * (20 * 1024 * 1024 - 6)
  encoded = base64.b64encode(gif).decode("ascii")
  items = [encoded] * (2 if shape == "gallery" else 1)
  body = encoded if shape == "scalar" else {"items": items, "caption": "😀 snow"}
  # ensure_ascii=False exercises the full Unicode text copy alongside base64:
  # stdlib decoding widens a byte-sized buffer from ASCII to UCS4 (5x bytes).
  # A BMP character before the astral one costs a sixth copy, which a gallery
  # this size cannot fit (test_http_unicode_decode_widening_beyond_budget...).
  raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
  path = f"/api/app-services/{app.slug}/media" if public else f"/api/apps/{app.id}/service/media"
  response = client.post(path, headers=auth, content=raw)
  assert response.status_code == 200
  assert response.json() == [{"size": len(gif), "sha256": hashlib.sha256(gif).hexdigest()}] * len(items)
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_queued_http_payloads_keep_their_envelope_admission_and_reject_new_growth(
  monkeypatch, body_buffer,
):
  from starlette.requests import Request
  from app.routes.app_services import _envelope

  class Gate(asyncio.Semaphore):
    def __init__(self):
      super().__init__(0)
      self.waiting = asyncio.Event()

    async def acquire(self):
      self.waiting.set()
      return await super().acquire()

  gate = Gate()
  reads = []
  monkeypatch.setattr(app_services, "_app_slots", {(1, "private"): gate})
  monkeypatch.setattr(app_services, "service_contract", lambda *_a, **_k: {"max_request_bytes": 1024, "max_response_bytes": 1024})
  monkeypatch.setattr(app_services, "hold_runtime", lambda *_: SimpleNamespace(close=lambda: None))

  body = b'{"payload":"' + b'x' * 256 + b'"}'
  admissions = []

  async def http_call():
    async def receive():
      reads.append(True)
      return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""}, receive)
    with app_services.ServiceExchangeAdmission(1024) as admission:
      admissions.append(admission)
      envelope = await _envelope(request, "echo", public=False, actor={}, max_bytes=1024, admission=admission)
      return await app_services.invoke_service(SimpleNamespace(id=1), None, envelope, admission=admission)

  first = asyncio.create_task(http_call())
  try:
    await asyncio.wait_for(gate.waiting.wait(), 1)
    reserved = app_services._admitted_exchange_cost
    assert reserved > len(body), "queued decoded input was no longer counted"
    # Only the retained envelope is held while queued, not a transfer allowance.
    assert reserved == admissions[0].request_cost
    assert admissions[0].transfer_cost == 0
    monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", reserved + 3 * len(body) - 1)
    buffered = list(body_buffer)
    assert buffered[-1][:2] == ("copy", len(body))
    with pytest.raises(HTTPException) as caught:
      await http_call()
    assert caught.value.status_code == 413
    assert reads == [True, True]
    assert body_buffer == buffered, "rejected backlog request buffered its body"
    assert app_services._admitted_exchange_cost == reserved
  finally:
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_cancellation_while_reading_releases_admission():
  started = asyncio.Event()

  async def read():
    with app_services.ServiceExchangeAdmission(1024) as admission:
      admission.admit_received(1024)
      started.set()
      await asyncio.Event().wait()

  task = asyncio.create_task(read())
  await started.wait()
  assert app_services._admitted_exchange_cost == 3072
  task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await task
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["spawn", "preload"])
async def test_service_execution_timeout_releases_request_admission(
  db, auth, monkeypatch, backend,
):
  from app import service_preload

  app = _service_app(db, service_bytes=b'import time; time.sleep(60)')
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: object() if backend == "preload" else None)

  async def timeout(*_args, **_kwargs):
    raise TimeoutError()

  monkeypatch.setattr(service_preload, "run", timeout)
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, {}, timeout_seconds=0.05)
  assert caught.value.status_code == 503
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_concurrent_http_backlog_is_bounded_before_execution_and_cleans_up(
  auth, db, monkeypatch,
):
  import httpx
  from app.main import app as application

  app = _service_app(db, max_bytes=4096)
  waiting = asyncio.Event()

  class BusyApp(asyncio.Semaphore):
    async def acquire(self):
      waiting.set()
      return await super().acquire()

  monkeypatch.setattr(app_services, "_app_slots", {(app.id, "private"): BusyApp(0)})
  requests = []
  peaks = []
  real_reserve = app_services.ServiceExchangeAdmission._reserve

  def measured_reserve(self, cost, **kwargs):
    real_reserve(self, cost, **kwargs)
    peaks.append(app_services._admitted_exchange_cost)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "_reserve", measured_reserve)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    async def post():
      return await http.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={"payload": "x" * 256})

    requests.append(asyncio.create_task(post()))
    try:
      await asyncio.wait_for(waiting.wait(), 3)
      budget = 3 * app_services._admitted_exchange_cost
      monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", budget)
      requests.extend(asyncio.create_task(post()) for _ in range(7))
      # Let the real HTTP routes finish admission. Queued requests remain
      # asleep at the per-app execution semaphore, not at a new memory queue.
      for _ in range(100):
        if sum(task.done() for task in requests) >= 5:
          break
        await asyncio.sleep(0.01)
      done = [task for task in requests if task.done()]
      assert len(done) >= 5
      assert all(task.result().status_code == 413 for task in done)
      assert 1 <= sum(not task.done() for task in requests) <= 3
      assert max(peaks) <= budget
      assert app_services._admitted_exchange_cost <= budget
    finally:
      for task in requests:
        task.cancel()
      await asyncio.gather(*requests, return_exceptions=True)
  assert app_services._admitted_exchange_cost == 0


DENSE_RESPONSE_SERVICE = b'''import json, sys
MOBIUS_PRELOAD = True
if __name__ == "__main__":
  json.load(sys.stdin)
  sys.stdout.write('{"body":[' + '[],' * 349_999 + '[]]}')
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["spawn", "preload"])
@pytest.mark.parametrize("lane", ["private", "public", "tools"])
async def test_dense_service_response_is_rejected_before_json_materialization(
  db, auth, monkeypatch, backend, lane,
):
  import sys
  import tracemalloc
  from app import service_preload

  app = _service_app(db, access="public", service_bytes=DENSE_RESPONSE_SERVICE, max_bytes=60 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  service = app_services.service_contract(app, access="public")
  entry = app_services.service_entry(app, service)
  envelope = {"actor": {}, "public": lane == "public"}
  if backend == "preload":
    host = await service_preload.start(
      (app.id, app.runtime_revision), app.slug, sys.executable, entry,
      app_services.service_environment(app, owner, service, public=lane == "public"),
    )
    assert host is not None
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: host)
  else:
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  real_loads = json.loads

  def guarded_loads(raw, *args, **kwargs):
    if isinstance(raw, bytes) and raw.startswith(b'{"body":[[],[],[]'):
      pytest.fail("dense stdout materialized before the response resource check")
    return real_loads(raw, *args, **kwargs)

  monkeypatch.setattr(app_services.json, "loads", guarded_loads)
  tracemalloc.start()
  try:
    with pytest.raises(HTTPException) as caught:
      await app_services.invoke_service(app, owner, envelope, lane=lane)
    peak = tracemalloc.get_traced_memory()[1]
  finally:
    tracemalloc.stop()
    await service_preload.shutdown()
  assert caught.value.status_code == 502
  assert "structural resource limit" in caught.value.detail
  assert peak < 8 * 1024 * 1024
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_spawned_response_decode_peak_includes_stdout_allocation(db, auth, monkeypatch):
  import tracemalloc
  from app import service_preload

  app = _service_app(db, service_bytes=DENSE_RESPONSE_SERVICE, max_bytes=60 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  real_decode = app_services.ServiceExchangeAdmission.decode_response
  async def predecessor_decode(_self, raw):
    return json.loads(raw)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode_response", predecessor_decode)
  tracemalloc.start()
  try:
    result = await app_services.invoke_service(app, owner, {"actor": {}}, lane="tools")
    before = tracemalloc.get_traced_memory()[1]
    assert len(result[1]) == 350_000
  finally:
    tracemalloc.stop()
  del result
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode_response", real_decode)
  tracemalloc.start()
  try:
    with pytest.raises(HTTPException) as caught:
      await app_services.invoke_service(app, owner, {"actor": {}}, lane="tools")
    after = tracemalloc.get_traced_memory()[1]
  finally:
    tracemalloc.stop()
  assert caught.value.status_code == 502
  assert before > 20 * 1024 * 1024
  assert after < 8 * 1024 * 1024
  assert app_services._admitted_exchange_cost == 0
  print(f"Spawned response decode peak including stdout: before={before}, after={after} bytes")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b'{"body":', b'', b'{"body":NaN}', b'"\xff"'])
async def test_malformed_service_response_still_returns_502_and_releases_exchange(
  db, auth, monkeypatch, raw,
):
  from app import service_preload

  app = _service_app(db)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)

  async def reply(*_args):
    return raw, b"", 0

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, {"actor": {}})
  assert caught.value.status_code == 502
  assert caught.value.detail == "App service returned invalid JSON."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_response_budget_adds_retained_request_before_parsing(db, auth, monkeypatch):
  from app import service_preload

  app = _service_app(db, max_bytes=4096)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  raw = b'{"body":"' + b'x' * 1000 + b'"}'
  envelope = {"actor": {}, "body": "x" * 1000}
  with app_services.ServiceExchangeAdmission(4096) as alone:
    alone.admit_output(raw)
    await alone.decode_response(raw)
    response_alone = alone.cost
  assert app_services._admitted_exchange_cost == 0

  async def reply(*_args):
    request_reserved = app_services._admitted_exchange_cost
    assert request_reserved > 3 * 4096
    retained_request = request_reserved - 3 * 4096
    # Either direction fits alone; their simultaneously retained costs do not.
    monkeypatch.setattr(
      app_services, "MAX_ADMITTED_EXCHANGE_COST", retained_request + response_alone - 1,
    )
    return raw, b"", 0

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  real_loads = json.loads

  def guarded_loads(value, *args, **kwargs):
    if value is raw:
      pytest.fail("response parsed before joint admission")
    return real_loads(value, *args, **kwargs)

  monkeypatch.setattr(app_services.json, "loads", guarded_loads)
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, envelope)
  assert caught.value.status_code == 502
  assert "admission budget" in caught.value.detail
  assert app_services._admitted_exchange_cost == 0


def test_http_exchange_reservation_covers_simultaneous_envelopes_and_json_response(
  client, auth, db, monkeypatch,
):
  from app.routes import app_services as routes

  app = _service_app(db, max_bytes=4096)
  real_decode = app_services.ServiceExchangeAdmission.decode_response
  real_response = routes._response
  seen = []

  async def decode(self, raw):
    value = await real_decode(self, raw)
    seen.append((self.cost, self.request_cost, self.response_cost))
    # Three copies of the actual stdout replace the execution allowance.
    assert self.transfer_cost == 3 * len(raw) < 3 * self.max_bytes
    assert self.cost == self.transfer_cost + self.request_cost + self.response_cost
    assert self.request_cost > 0 and self.response_cost > 0
    return value

  def response(*args):
    assert app_services._admitted_exchange_cost == seen[0][0]
    result = real_response(*args)  # JSONResponse eagerly constructs its bytes.
    assert app_services._admitted_exchange_cost == seen[0][0]
    return result

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode_response", decode)
  monkeypatch.setattr(routes, "_response", response)
  reply = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={"payload": "雪😀\\\"" * 10})
  assert reply.status_code == 201
  assert len(seen) == 1
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_tool_exchange_reservation_is_held_through_decoded_result_handoff(
  db, auth, monkeypatch,
):
  from app import service_preload

  app = _service_app(db)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)

  async def reply(*_args):
    return b'{"body":{"ok":true}}', b"", 0

  real_exit = app_services.ServiceExchangeAdmission.__exit__
  seen = []

  def release(self, *args):
    assert self.response_cost > 0 and self.request_cost > 0
    assert app_services._admitted_exchange_cost == self.cost
    seen.append(self.cost)
    return real_exit(self, *args)

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "__exit__", release)
  result = await app_services.invoke_service(app, owner, {"actor": {}}, lane="tools")
  assert result[:2] == (200, {"ok": True})
  assert len(seen) == 1
  assert app_services._admitted_exchange_cost == 0


DOWNLOAD_SERVICE = '''import base64, json, sys
MOBIUS_PRELOAD = True
if __name__ == "__main__":
  mode = json.load(sys.stdin)["body"]["mode"]
  size = 20 * 1024 * 1024
  if mode == "binary":
    body = b"GIF89a" + b"x" * (size - 6)
    print(json.dumps({"body_base64": base64.b64encode(body).decode(), "media_type": "image/gif"}))
  else:
    suffix = '\\\\"{},[]:雪😀'
    body = "x" * (size - len(suffix)) + suffix
    print(json.dumps({"body": body}, ensure_ascii=mode == "escaped"))
'''.encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,lane", [("spawn", "public"), ("preload", "tools")])
@pytest.mark.parametrize("shape", ["binary", "scalar", "escaped"])
async def test_twenty_mib_download_response_preserves_binary_unicode_and_escape_allowance(
  db, auth, monkeypatch, backend, lane, shape,
):
  import httpx
  import sys
  from app import service_preload
  from app.main import app as application

  app = _service_app(db, access="public", service_bytes=DOWNLOAD_SERVICE, max_bytes=60 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  service = app_services.service_contract(app, access="public")
  if backend == "preload":
    host = await service_preload.start(
      (app.id, app.runtime_revision), app.slug, sys.executable,
      app_services.service_entry(app, service),
      app_services.service_environment(app, owner, service, public=False),
    )
    assert host is not None
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: host)
  else:
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  try:
    if lane == "public":
      async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
        result = await http.post(f"/api/app-services/{app.slug}/download", json={"mode": shape})
      assert result.status_code == 200
      body = result.content if shape == "binary" else result.json()
      assert result.headers["content-type"].startswith("image/gif" if shape == "binary" else "application/json")
    else:
      status, body, _headers, media = await app_services.invoke_service(
        app, owner, {"body": {"mode": shape}, "actor": {}}, lane="tools",
      )
      assert status == 200
      assert media == ("image/gif" if shape == "binary" else None)
    assert len(body) == 20 * 1024 * 1024
    if shape == "binary":
      assert body[:6] == b"GIF89a"
      assert body[6:] == b"x" * (len(body) - 6)
    else:
      assert body.startswith("x" * 1024)
      assert body.endswith('\\"{},[]:雪😀')
  finally:
    await service_preload.shutdown()
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_cancel_during_partial_response_read_reaps_child_and_releases_exchange(
  db, auth, monkeypatch,
):
  from app import service_preload

  app = _service_app(db, max_bytes=4096, service_bytes=b'''import json, sys, time
json.load(sys.stdin)
sys.stdout.write('{"body":[')
sys.stdout.flush()
time.sleep(60)
''')
  owner = db.query(models.Owner).first()
  started = asyncio.Event()
  processes = []
  real_spawn = asyncio.create_subprocess_exec
  real_read = app_services._read_bounded

  async def spawn(*args, **kwargs):
    process = await real_spawn(*args, **kwargs)
    processes.append(process)
    return process

  async def read(reader, limit):
    if limit == 4096:
      prefix = await reader.readexactly(9)
      started.set()
      return prefix + await real_read(reader, limit - len(prefix))
    return await real_read(reader, limit)

  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
  monkeypatch.setattr(app_services, "_read_bounded", read)
  task = asyncio.create_task(app_services.invoke_service(app, owner, {"actor": {}}, lane="tools"))
  try:
    await asyncio.wait_for(started.wait(), 3)
    assert app_services._admitted_exchange_cost > 3 * 4096
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await asyncio.wait_for(task, 3)
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert processes and all(process.returncode is not None for process in processes)
  assert app_services._admitted_exchange_cost == 0


def test_http_response_construction_failure_releases_exchange(client, auth, db, monkeypatch):
  from app.routes import app_services as routes

  app = _service_app(db)

  def fail(*_args):
    assert app_services._admitted_exchange_cost > 0
    raise RuntimeError("response construction failed")

  monkeypatch.setattr(routes, "_response", fail)
  with pytest.raises(RuntimeError, match="response construction failed"):
    client.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={})
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["self", "shared", "public"])
@pytest.mark.parametrize("trickle", [False, True], ids=["stalled", "trickling"])
async def test_actual_http_body_deadline_is_total_and_releases_reservations(
  db, auth, monkeypatch, route, trickle,
):
  import httpx
  from app.main import app as application
  from app.routes import app_services as routes

  app = _service_app(db, access="public", max_bytes=60 * 1024 * 1024)
  assert routes.SERVICE_BODY_TIMEOUT_SECONDS == 120
  monkeypatch.setattr(routes, "SERVICE_BODY_TIMEOUT_SECONDS", 0.08)
  started = asyncio.Event()
  cancelled = asyncio.Event()
  received = []

  async def body():
    try:
      started.set()
      received.append(True)
      yield b'"'
      while True:
        if trickle:
          await asyncio.sleep(0.01)
          received.append(True)
          yield b'x'
        else:
          await asyncio.Event().wait()
    finally:
      cancelled.set()

  path = {
    "self": f"/api/apps/{app.id}/service/echo",
    "shared": f"/api/services/{app.slug}/echo",
    "public": f"/api/app-services/{app.slug}/echo",
  }[route]
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    response = await asyncio.wait_for(http.post(path, headers=auth, content=body()), 2)
  assert started.is_set() and cancelled.is_set()
  assert len(received) >= (2 if trickle else 1)
  assert response.status_code == 408
  assert response.json()["detail"] == "App service request body timed out."
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_stalled_public_http_cannot_monopolize_private_or_tool_admission(
  db, auth, monkeypatch, body_buffer,
):
  import httpx
  from app.main import app as application
  from app.routes import app_services as routes

  app = _service_app(db, access="public", max_bytes=60 * 1024 * 1024)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(routes, "SERVICE_BODY_TIMEOUT_SECONDS", 5)
  assert app_services.MAX_PUBLIC_INGRESS_COST == 192 * 1024 * 1024
  assert app_services.MAX_ADMITTED_EXCHANGE_COST == 512 * 1024 * 1024
  # A public read holds what it actually received, so saturate a scaled
  # sub-budget with real bytes rather than buffering 64 MiB in a test.
  received = 64 * 1024
  held = 3 * received
  monkeypatch.setattr(app_services, "MAX_PUBLIC_INGRESS_COST", held)
  started = asyncio.Event()
  cancelled = asyncio.Event()

  async def stalled():
    try:
      yield b'"' + b"x" * (received - 1)
      started.set()
      await asyncio.Event().wait()
    finally:
      cancelled.set()

  async def denied():
    yield b'{}'

  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    first = asyncio.create_task(http.post(f"/api/app-services/{app.slug}/echo", content=stalled()))
    try:
      await asyncio.wait_for(started.wait(), 2)
      for _ in range(100):
        if app_services._public_ingress_cost == held:
          break
        await asyncio.sleep(0.01)
      assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == held
      buffered = list(body_buffer)
      second = await http.post(f"/api/app-services/{app.slug}/echo", content=denied())
      assert second.status_code == 413
      assert "public ingress budget" in second.json()["detail"]
      assert body_buffer == buffered, "a denied public body was buffered"
      # Both real HTTP private execution and direct tool execution retain the
      # same 60 MiB grant while the anonymous read remains stalled.
      private = await asyncio.wait_for(http.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={}), 3)
      assert private.status_code == 201
      result = await asyncio.wait_for(app_services.invoke_service(
        app, owner, {"actor": {"scope": "owner"}, "body": {}, "path": "echo",
                     "method": "POST", "query": {}, "public": False}, lane="tools",
      ), 3)
      assert result[0] == 201
      assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == held
    finally:
      first.cancel()
      await asyncio.gather(first, return_exceptions=True)
  assert cancelled.is_set()
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_public_read_transfer_keeps_total_body_ownership(monkeypatch):
  from starlette.requests import Request
  from app.routes.app_services import _envelope

  max_bytes = 4096
  body = b'{"value":"accepted"}'
  received = 3 * len(body)
  observed = []
  real_decode = app_services.ServiceExchangeAdmission.decode

  async def decode(self, raw):
    assert app_services._public_ingress_cost == 0
    assert app_services._admitted_exchange_cost == self.cost == received
    observed.append(raw)
    return await real_decode(self, raw)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode", decode)

  async def receive():
    # Nothing is charged before a byte arrives: the header is not trusted.
    assert app_services._public_ingress_cost == app_services._admitted_exchange_cost == 0
    return {"type": "http.request", "body": body, "more_body": False}

  request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""}, receive)
  with app_services.ServiceExchangeAdmission(max_bytes, public_ingress=True) as admission:
    envelope = await _envelope(request, "echo", public=True, actor={}, max_bytes=max_bytes, admission=admission)
    assert envelope["body"] == {"value": "accepted"}
    assert app_services._admitted_exchange_cost == admission.cost > received
    assert app_services._public_ingress_cost == 0
    admission.finish_ingress()  # phase completion is idempotent
    assert app_services._admitted_exchange_cost == admission.cost
  assert observed == [b'{"value":"accepted"}']
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_public_disconnect_during_body_read_releases_both_budgets():
  from starlette.requests import ClientDisconnect, Request
  from app.routes.app_services import _envelope

  messages = iter([
    {"type": "http.request", "body": b'"', "more_body": True},
    {"type": "http.disconnect"},
  ])

  async def receive():
    return next(messages)

  request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""}, receive)
  with pytest.raises(ClientDisconnect):
    with app_services.ServiceExchangeAdmission(4096, public_ingress=True) as admission:
      await _envelope(request, "echo", public=True, actor={}, max_bytes=4096, admission=admission)
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", [b'\\n', b'\\"', b'\\\\', b'\\u1234'])
async def test_actual_public_http_escape_heavy_scalar_does_not_starve_heartbeat(
  db, auth, escape,
):
  import httpx
  import time
  from app.main import app as application

  service = b'''import json, sys
value = json.load(sys.stdin)["body"]
print(json.dumps({"body": {"length": len(value)}}))
'''
  app = _service_app(db, access="public", service_bytes=service, max_bytes=60 * 1024 * 1024)
  raw = b'"' + escape * ((4 * 1024 * 1024) // len(escape)) + b'"'
  expected = len(json.loads(raw))
  beats = []
  running = True

  async def heartbeat():
    while running:
      beats.append(time.monotonic())
      await asyncio.sleep(0.01)

  beat = asyncio.create_task(heartbeat())
  try:
    await asyncio.sleep(0.02)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
      response = await http.post(f"/api/app-services/{app.slug}/echo", content=raw)
    await asyncio.sleep(0.02)
  finally:
    running = False
    await beat
  assert response.status_code == 200
  assert response.json() == {"length": expected}
  gap = max(b - a for a, b in zip(beats, beats[1:]))
  print(f"Actual public 4 MiB {escape!r} scalar maximum 10ms heartbeat gap: {gap:.4f}s")
  assert gap < 0.2, "bounded scanner monopolized the event loop for an escaped scalar"
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["request", "response"])
async def test_cooperative_resource_scan_cancel_releases_owned_bytes(monkeypatch, direction):
  scanned = asyncio.Event()
  matcher = app_services._JSON_STRING_CONTENT

  class ObservedMatcher:
    def match(self, *args):
      scanned.set()
      return matcher.match(*args)

  monkeypatch.setattr(app_services, "_JSON_STRING_CONTENT", ObservedMatcher())
  raw = b'"' + b'\\n' * 100_000 + b'"'

  async def decode():
    with app_services.ServiceExchangeAdmission(1024 * 1024) as admission:
      if direction == "request":
        admission.admit_received(len(raw))
        return await admission.decode(raw)
      admission.admit_output(raw)
      return await admission.decode_response(raw)

  task = asyncio.create_task(decode())
  try:
    await asyncio.wait_for(scanned.wait(), 1)
    assert not task.done()
    assert app_services._admitted_exchange_cost == 3 * len(raw)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_revocation_cancels_attributed_response_scan_before_handoff(db, auth, monkeypatch):
  from app import service_preload

  service = b'''import json, sys
json.load(sys.stdin)
sys.stdout.write('{"body":"' + '\\\\n' * 100_000 + '"}')
'''
  app = _service_app(db, service_bytes=service)
  owner = db.query(models.Owner).first()
  grant_id = "a" * 32
  monkeypatch.setattr(app_services, "_validate_browser_call", lambda *_: None)
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  scanned = asyncio.Event()
  matcher = app_services._JSON_STRING_CONTENT

  class ObservedMatcher:
    def match(self, *args):
      scanned.set()
      return matcher.match(*args)

  monkeypatch.setattr(app_services, "_JSON_STRING_CONTENT", ObservedMatcher())
  task = asyncio.create_task(app_services.invoke_service(
    app, owner, {"actor": {"browser_grant_id": grant_id}}, lane="tools",
  ))
  try:
    await asyncio.wait_for(scanned.wait(), 3)
    assert app_services.browser_grant_has_active_calls(grant_id)
    assert not task.done()
    await app_services.cancel_browser_grant_calls(grant_id)
    assert task.cancelled()
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert grant_id not in app_services._browser_calls
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


MIB = 1024 * 1024


def _route_limit(method, path, max_bytes):
  return {
    "method": method, "path": path,
    "max_request_bytes": max_bytes, "max_response_bytes": max_bytes,
  }


def test_route_ceiling_matches_one_canonical_once_decoded_path():
  service = {
    "max_request_bytes": 60 * MIB, "max_response_bytes": 60 * MIB,
    "route_limits": [
      _route_limit("GET", "/actor", MIB),
      _route_limit("POST", "/tools/log", 2048),
      _route_limit("POST", "/reviewer/manual-plan", 4096),
    ],
  }
  # HTTP delivers no leading slash, tools a leading slash, policies none.
  for path in ("actor", "/actor", "actor/", "//actor"):
    assert app_services.exchange_max_bytes(service, "GET", path) == MIB
  assert app_services.exchange_max_bytes(service, "POST", "/tools/log") == 2048
  assert app_services.exchange_max_bytes(service, "POST", "reviewer/manual-plan") == 4096
  # Wrong method, case, prefix, extension, an undecoded form, or a missing
  # selector all keep the whole reviewed grant: no broad or repeated matching.
  for method, path in (
    ("POST", "actor"), ("get", "actor"), ("GET", "Actor"), ("GET", "actor/x"),
    ("GET", "act"), ("GET", "a%63tor"), ("GET", "actor?x=1"),
    ("GET", "x/../actor"), (None, "actor"), ("GET", None),
  ):
    assert app_services.exchange_max_bytes(service, method, path) == 60 * MIB
  assert app_services.exchange_max_bytes({}, None, None) == 8 * MIB


def test_route_ceilings_are_bounded_by_the_exact_accepted_whole_grant():
  def whole(max_bytes, *limits):
    return {"max_request_bytes": max_bytes, "max_response_bytes": max_bytes,
            "route_limits": list(limits)}

  # A ceiling equal to its grant is valid, including at the platform maximum
  # and under the 8 MiB default of a service accepted without whole fields.
  exact = whole(1024, _route_limit("GET", "/actor", 1024))
  assert app_services.exchange_max_bytes(exact, "GET", "actor") == 1024
  top = whole(60 * MIB, _route_limit("GET", "/actor", 60 * MIB))
  assert app_services.exchange_max_bytes(top, "GET", "actor") == 60 * MIB
  legacy = {"route_limits": [_route_limit("GET", "/actor", 8 * MIB)]}
  assert app_services.exchange_max_bytes(legacy, "GET", "actor") == 8 * MIB

  for service in (
    whole(1024, _route_limit("GET", "/actor", 2048)),
    {"route_limits": [_route_limit("GET", "/actor", 8 * MIB + 1)]},
  ):
    with pytest.raises(HTTPException) as refused:
      app_services.exchange_max_bytes(service, "POST", "unrelated")
    assert (refused.value.status_code, refused.value.detail) == (
      503, "Accepted app service route limits are invalid.",
    )


@pytest.mark.parametrize(("request_bytes", "response_bytes"), [
  (1024.0, 1024.0), (True, True), (1024, 1024.0), (1024.0, 1024), (1024, True),
  (1024, 2048), (0, 0), (60 * MIB + 1, 60 * MIB + 1), ("1024", "1024"), (None, None),
  (1024, "omitted"), ("omitted", 1024),
])
@pytest.mark.parametrize("route_limits", ["omitted", [], [_route_limit("GET", "/actor", 1)]],
                         ids=["no-routes", "empty-routes", "routes"])
def test_malformed_accepted_whole_grant_disables_the_service(
  request_bytes, response_bytes, route_limits,
):
  from app.manifest_contract import accepted_service_max_bytes

  service = {}
  for key, value in (
    ("max_request_bytes", request_bytes), ("max_response_bytes", response_bytes),
    ("route_limits", route_limits),
  ):
    if value != "omitted":
      service[key] = value
  assert accepted_service_max_bytes(service) is None
  with pytest.raises(HTTPException) as refused:
    app_services.exchange_max_bytes(service, "GET", "actor")
  assert (refused.value.status_code, refused.value.detail) == (
    503, "Accepted app service transfer limit is invalid.",
  )


@pytest.mark.parametrize("route_limits", [
  {"GET /actor": 1024},
  [["GET", "/actor", 1024]],
  [{"method": "GET", "path": "/actor", "max_request_bytes": 1024}],
  [{**_route_limit("GET", "/actor", 1024), "extra": True}],
  [{**_route_limit("GET", "/actor", 1024), "max_response_bytes": 2048}],
  [_route_limit("GET", "/actor", 0)],
  [_route_limit("GET", "/actor", True)],
  [{**_route_limit("GET", "/actor", 1), "max_response_bytes": True}],
  [{**_route_limit("GET", "/actor", 1), "max_response_bytes": 1.0}],
  [_route_limit({}, [], 1024)],
  [_route_limit(None, "/actor", 1024)],
  [_route_limit("GET", "/actor", 1024.0)],
  [_route_limit("GET", "/actor", 8 * MIB + 1)],
  [_route_limit("get", "/actor", 1024)],
  [_route_limit("HEAD", "/actor", 1024)],
  [_route_limit("GET", "actor", 1024)],
  [_route_limit("GET", "/actor/", 1024)],
  [_route_limit("GET", "/a%2Fb", 1024)],
  [_route_limit("GET", "/", 1024)],
  [_route_limit("GET", "/actor", 1024), _route_limit("GET", "/actor", 512)],
  [_route_limit("GET", f"/r{index}", 1024) for index in range(17)],
], ids=lambda value: json.dumps(value)[:40])
def test_malformed_accepted_route_limits_disable_the_service_instead_of_widening(
  client, auth, db, monkeypatch, route_limits,
):
  from app.routes import app_services as routes

  app = _service_app(db, slug="malformed-routes", route_limits=route_limits)

  async def forbidden_read(*_args, **_kwargs):
    pytest.fail("a malformed accepted contract reached the body read")

  monkeypatch.setattr(routes, "read_capped_body", forbidden_read)
  response = client.post(f"/api/apps/{app.id}/service/echo", headers=auth, json={})
  assert response.status_code == 503
  assert response.json()["detail"] == "Accepted app service route limits are invalid."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["self", "shared", "public"])
@pytest.mark.parametrize("framing", ["chunked", "understated-length"])
async def test_http_route_ceiling_is_reserved_and_enforced_before_body_decode(
  db, auth, monkeypatch, route, framing,
):
  import httpx
  from app.main import app as application
  from app.routes import app_services as routes

  cap = 4096
  app = _service_app(
    db, access="public", max_bytes=60 * MIB,
    route_limits=[_route_limit("POST", "/activity", cap)],
  )
  reads = []
  decoded = []
  grown = []
  executions = []
  real_read = routes.read_capped_body
  real_decode = app_services.ServiceExchangeAdmission.decode
  real_grow = app_services.ServiceExchangeAdmission.admit_received
  real_execute = app_services.ServiceExchangeAdmission.admit_execution

  async def tracked_read(request, limit, **kwargs):
    reads.append((
      limit, app_services._admitted_exchange_cost,
      app_services._public_ingress_cost, request.headers.get("content-length"),
    ))
    return await real_read(request, limit, **kwargs)

  async def tracked_decode(self, raw):
    decoded.append(len(raw))
    return await real_decode(self, raw)

  def tracked_grow(self, size):
    real_grow(self, size)
    grown.append((size, app_services._public_ingress_cost))

  def tracked_execute(self, max_bytes):
    real_execute(self, max_bytes)
    executions.append((max_bytes, self.transfer_cost))

  monkeypatch.setattr(routes, "read_capped_body", tracked_read)
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "decode", tracked_decode)
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "admit_received", tracked_grow)
  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "admit_execution", tracked_execute)
  url = {
    "self": f"/api/apps/{app.id}/service/activity",
    "shared": f"/api/services/{app.slug}/activity",
    "public": f"/api/app-services/{app.slug}/activity",
  }[route]
  headers = dict(auth)
  if framing == "understated-length":
    headers["content-length"] = "2"

  def body(size):
    payload = b'"' + b"x" * (size - 2) + b'"'

    async def chunks():
      for start in range(0, len(payload), 1000):
        yield payload[start:start + 1000]

    return chunks()

  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    over = await http.post(url, headers=headers, content=body(cap + 1))
    exact = await http.post(url, headers=headers, content=body(cap))
    small = await http.post(url, headers=auth, json={})
  assert over.status_code == 413
  assert over.json()["detail"] == "App service request is too large."
  # cap+1 bytes never reach the decoder; exactly cap bytes pass the read (its
  # envelope then exceeds the same ceiling at serialization, also 413).
  assert decoded == [cap, 2]
  assert exact.status_code == 413
  assert small.status_code == 201
  assert [read[0] for read in reads] == [cap, cap, cap]
  # Nothing is reserved before bytes arrive; growth charges only received
  # bytes, never beyond the route ceiling, and execution reserves that ceiling.
  assert all(read[1] == read[2] == 0 for read in reads)
  assert max(size for size, _public in grown) == cap
  assert all(public == (3 * size if route == "public" else 0) for size, public in grown)
  assert executions == [(cap, 3 * cap)] * 2, "admission did not reserve the route ceiling"
  if framing == "understated-length":
    assert [read[3] for read in reads[:2]] == ["2", "2"]
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_tool_and_policy_requests_use_their_route_ceiling_before_execution(
  db, auth, monkeypatch,
):
  from app import service_preload

  cap = 2048
  app = _service_app(db, max_bytes=60 * MIB, route_limits=[
    _route_limit("POST", "/tools/log", cap),
    _route_limit("POST", "/reviewer/validate-comment", cap),
  ])
  owner = db.query(models.Owner).first()
  executed = []

  async def reply(_python, _entry, _environment, request, _timeout, max_stdout):
    executed.append((json.loads(request)["path"], max_stdout, app_services._admitted_exchange_cost))
    return b'{"body":{"ok":true}}', b"", 0

  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  monkeypatch.setattr(app_services, "_run_spawned", reply)

  def tool(path, text):
    return {
      "schema": 1, "method": "POST", "path": path, "query": {}, "headers": {},
      "body": {"arguments": {"text": text}, "call": {}}, "public": False, "actor": {},
    }

  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, tool("/tools/log", "x" * cap), lane="tools")
  assert caught.value.status_code == 413
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_policy(app, owner, "reviewer/validate-comment", {"body": "x" * cap})
  assert caught.value.status_code == 413
  assert executed == []

  await app_services.invoke_service(app, owner, tool("/tools/log", "ok"), lane="tools")
  await app_services.invoke_policy(app, owner, "reviewer/validate-comment", {"body": "ok"})
  await app_services.invoke_service(app, owner, tool("/tools/other", "x" * cap), lane="tools")
  assert [(path, limit) for path, limit, _cost in executed] == [
    ("/tools/log", cap), ("reviewer/validate-comment", cap), ("/tools/other", 60 * MIB),
  ]
  for _path, limit, cost in executed:
    # Three serialized copies of the selected ceiling plus the small envelope.
    assert 3 * limit <= cost < 3 * limit + 64 * 1024
  assert app_services._admitted_exchange_cost == 0


ROUTE_OUTPUT_SERVICE = b'''import base64, json, sys
MOBIUS_PRELOAD = True
if __name__ == "__main__":
  body = json.load(sys.stdin)["body"]
  if body["binary"]:
    print(json.dumps({"body_base64": base64.b64encode(b"x" * body["size"]).decode(), "media_type": "image/gif"}))
  else:
    print(json.dumps({"body": "x" * body["size"]}))
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["spawn", "preload"])
@pytest.mark.parametrize("binary", [False, True], ids=["json", "binary"])
async def test_route_ceiling_bounds_actual_service_output_on_both_backends(
  db, auth, monkeypatch, backend, binary,
):
  import sys
  from app import service_preload

  cap = 64 * 1024
  app = _service_app(
    db, service_bytes=ROUTE_OUTPUT_SERVICE, max_bytes=60 * MIB,
    route_limits=[_route_limit("POST", "/tools/thumbnail", cap)],
  )
  owner = db.query(models.Owner).first()
  service = app_services.service_contract(app, access="self")
  limits = []
  if backend == "preload":
    host = await service_preload.start(
      (app.id, app.runtime_revision), app.slug, sys.executable,
      app_services.service_entry(app, service),
      app_services.service_environment(app, owner, service, public=False),
    )
    assert host is not None
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: host)
    real_run = service_preload.run

    async def run(*args, **kwargs):
      limits.append(kwargs["max_stdout"])
      return await real_run(*args, **kwargs)

    monkeypatch.setattr(service_preload, "run", run)
  else:
    monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
    real_spawned = app_services._run_spawned

    async def spawned(*args):
      limits.append(args[5])
      return await real_spawned(*args)

    monkeypatch.setattr(app_services, "_run_spawned", spawned)

  def envelope(path):
    return {
      "schema": 1, "method": "POST", "path": path, "query": {}, "headers": {},
      "body": {"size": cap, "binary": binary}, "public": False, "actor": {},
    }

  try:
    with pytest.raises(HTTPException) as caught:
      await app_services.invoke_service(app, owner, envelope("/tools/thumbnail"), lane="tools")
    # The existing serialized-output limit outcome, now at the route ceiling.
    assert caught.value.status_code == 503
    assert caught.value.detail == "App service exceeded its execution limits."
    status, body, _headers, media = await app_services.invoke_service(
      app, owner, envelope("/tools/original"), lane="tools",
    )
  finally:
    await service_preload.shutdown()
  assert status == 200
  assert len(body) == cap
  assert media == ("image/gif" if binary else None)
  assert limits == [cap, 60 * MIB]
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_route_ceiling_bounds_decoded_binary_response(db, auth, monkeypatch):
  import base64
  from app import service_preload

  cap = 1024
  app = _service_app(db, max_bytes=60 * MIB, route_limits=[_route_limit("GET", "/thumb", cap)])
  owner = db.query(models.Owner).first()
  # A compact envelope with a decoded body one byte over the route ceiling.
  raw = json.dumps({"body_base64": base64.b64encode(b"x" * (cap + 1)).decode(), "media_type": "image/gif"}).encode()

  async def reply(*_args):
    return raw, b"", 0

  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  monkeypatch.setattr(app_services, "_run_spawned", reply)
  with pytest.raises(HTTPException) as caught:
    await app_services.invoke_service(app, owner, {"method": "GET", "path": "thumb", "actor": {}})
  assert caught.value.status_code == 502
  assert caught.value.detail == "App service returned too much binary data."
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_declared_control_routes_coexist_with_a_stalled_undeclared_public_read(
  db, auth, monkeypatch,
):
  import httpx
  from app.main import app as application
  from app.routes import app_services as routes

  app = _service_app(db, access="public", max_bytes=60 * MIB, route_limits=[
    _route_limit("GET", "/actor", MIB),
    _route_limit("POST", "/activity", MIB),
  ])
  monkeypatch.setattr(routes, "SERVICE_BODY_TIMEOUT_SECONDS", 5)
  started = asyncio.Event()
  cancelled = asyncio.Event()
  executions = []
  started_services = []
  real_execute = app_services.ServiceExchangeAdmission.admit_execution
  real_entry = app_services.service_entry

  def tracked_execute(self, max_bytes):
    executions.append(max_bytes)
    real_execute(self, max_bytes)

  def tracked_entry(*args):
    started_services.append(True)
    return real_entry(*args)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "admit_execution", tracked_execute)
  monkeypatch.setattr(app_services, "service_entry", tracked_entry)
  received = 64 * 1024

  async def stalled():
    try:
      yield b'"' + b"x" * (received - 1)
      started.set()
      await asyncio.Event().wait()
    finally:
      cancelled.set()

  held = 3 * received
  # Leave room for a control route's whole allowance, not a media grant's.
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", held + 4 * MIB)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    first = asyncio.create_task(http.post(f"/api/app-services/{app.slug}/echo", content=stalled()))
    try:
      await asyncio.wait_for(started.wait(), 2)
      for _ in range(100):
        if app_services._public_ingress_cost == held:
          break
        await asyncio.sleep(0.01)
      assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == held
      actor = await asyncio.wait_for(http.get(f"/api/app-services/{app.slug}/actor"), 3)
      activity = await asyncio.wait_for(
        http.post(f"/api/app-services/{app.slug}/activity", json={"type": "Like"}), 3,
      )
      assert (actor.status_code, actor.json()["method"], actor.json()["path"]) == (201, "GET", "actor")
      assert (activity.status_code, activity.json()["body"]) == (201, {"type": "Like"})
      assert executions == [MIB, MIB], "a declared control route reserved more than its ceiling"
      # The route ceiling is enforced, not just reserved.
      oversize = await http.post(
        f"/api/app-services/{app.slug}/activity", content=b'"' + b"x" * MIB + b'"',
      )
      assert oversize.status_code == 413
      assert oversize.json()["detail"] == "App service request is too large."
      # Wrong method and unmatched routes keep the whole 60 MiB grant, which
      # the remaining budget cannot hold: a retryable busy answer, no process.
      for method, path in (("GET", "activity"), ("POST", "actor"), ("POST", "inbox")):
        denied = await http.request(method, f"/api/app-services/{app.slug}/{path}", content=b"{}")
        assert denied.status_code == 503
        assert denied.headers["retry-after"] == "1"
      assert executions == [MIB, MIB, 60 * MIB, 60 * MIB, 60 * MIB]
      assert len(started_services) == 2
      assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == held
    finally:
      first.cancel()
      await asyncio.gather(first, return_exceptions=True)
  assert cancelled.is_set()
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_revocation_cancels_a_route_capped_exchange_and_releases_its_reservation(
  db, auth, monkeypatch,
):
  from app import service_preload

  service = b'''import json, sys
json.load(sys.stdin)
sys.stdout.write('{"body":"' + '\\\\n' * 100_000 + '"}')
'''
  cap = 512 * 1024
  app = _service_app(
    db, service_bytes=service, max_bytes=60 * MIB,
    route_limits=[_route_limit("POST", "/tools/feed", cap)],
  )
  owner = db.query(models.Owner).first()
  grant_id = "b" * 32
  monkeypatch.setattr(app_services, "_validate_browser_call", lambda *_: None)
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  scanned = asyncio.Event()
  matcher = app_services._JSON_STRING_CONTENT
  reserved = []
  executed = []
  real_execute = app_services.ServiceExchangeAdmission.admit_execution

  def tracked_execute(self, max_bytes):
    real_execute(self, max_bytes)
    executed.append(app_services._admitted_exchange_cost)

  class ObservedMatcher:
    def match(self, *args):
      reserved.append(app_services._admitted_exchange_cost)
      scanned.set()
      return matcher.match(*args)

  monkeypatch.setattr(app_services.ServiceExchangeAdmission, "admit_execution", tracked_execute)
  monkeypatch.setattr(app_services, "_JSON_STRING_CONTENT", ObservedMatcher())
  task = asyncio.create_task(app_services.invoke_service(app, owner, {
    "method": "POST", "path": "/tools/feed", "actor": {"browser_grant_id": grant_id},
  }, lane="tools"))
  try:
    await asyncio.wait_for(scanned.wait(), 3)
    # Execution reserved the route ceiling; the scan holds the actual output.
    assert 3 * cap <= executed[0] < 3 * MIB
    assert 3 * 200_000 < reserved[0] < 3 * cap
    assert app_services.browser_grant_has_active_calls(grant_id)
    await app_services.cancel_browser_grant_calls(grant_id)
    assert task.cancelled()
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert grant_id not in app_services._browser_calls
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


# Phase-aware exchange admission: a queued exchange holds only its retained
# envelope, a body read is charged for bytes actually received before each
# buffer copy, and the whole transfer allowance is reserved inside the
# execution slot before serialization or any process starts.


class _ParkedLane(asyncio.Semaphore):
  """An execution slot that counts the exchanges queued on it."""

  def __init__(self, value=0):
    super().__init__(value)
    self.queued = 0
    self.changed = asyncio.Event()

  async def acquire(self):
    self.queued += 1
    self.changed.set()
    return await super().acquire()

  async def wait_queued(self, count):
    while self.queued < count:
      self.changed.clear()
      await asyncio.wait_for(self.changed.wait(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway", ["shared", "same-app"])
async def test_queued_bodyless_gets_hold_only_their_envelopes_and_all_complete(
  auth, db, monkeypatch, gateway,
):
  import httpx
  from app.main import app as application

  app = _service_app(db, max_bytes=60 * MIB, service_id="queued-social", route_limits=[
    _route_limit("GET", "/actor", MIB), _route_limit("POST", "/activity", MIB),
  ])
  lane = _ParkedLane()
  monkeypatch.setattr(app_services, "_app_slots", {(app.id, "private"): lane})
  route = "/api/services/queued-social" if gateway == "shared" else f"/api/apps/{app.id}/service"
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    calls = [
      asyncio.create_task(http.get(f"{route}/board-media/post-{n}/0?thumbnail=true", headers=auth))
      for n in range(4)
    ]
    try:
      while lane.queued < 4:
        refused = [task.result().status_code for task in calls if task.done()]
        assert refused == [], f"a queued read was refused instead of waiting: {refused}"
        await asyncio.sleep(0.01)
      held = app_services._admitted_exchange_cost
      # Four undeclared 60 MiB-grant reads wait on one serialized lane while
      # holding only their small envelopes, not 4 x 180 MiB of allowance.
      assert 0 < held < 64 * 1024
      assert app_services._public_ingress_cost == 0
      lane.release()
      replies = await asyncio.wait_for(asyncio.gather(*calls), 30)
    finally:
      for task in calls:
        task.cancel()
      await asyncio.gather(*calls, return_exceptions=True)
  assert [reply.status_code for reply in replies] == [201] * 4
  assert [reply.json()["path"] for reply in replies] == [
    f"board-media/post-{n}/0" for n in range(4)
  ]
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_burst_of_tiny_responses_never_needs_a_second_transfer_allowance(
  auth, db, monkeypatch,
):
  import httpx
  from app.main import app as application

  app = _service_app(db, max_bytes=60 * MIB)
  # One execution allowance plus room for small envelopes and responses: a
  # finished or queued exchange must not keep a media-sized reservation.
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 3 * 60 * MIB + MIB)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    replies = await asyncio.wait_for(asyncio.gather(*(
      http.get(f"/api/apps/{app.id}/service/thumb/{n}", headers=auth) for n in range(5)
    )), 30)
  assert [reply.status_code for reply in replies] == [201] * 5
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_finished_tiny_responses_release_their_transfer_allowance_before_handoff(
  db, auth, monkeypatch,
):
  from app import service_preload

  cap = 60 * MIB
  app = _service_app(db, max_bytes=cap)
  owner = db.query(models.Owner).first()
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 3 * cap + MIB)
  executed = []

  async def reply(*_args):
    executed.append(app_services._admitted_exchange_cost)
    return b'{"body":{"ok":true}}', b"", 0

  monkeypatch.setattr(app_services, "_run_spawned", reply)
  release = asyncio.Event()
  handed_off = []

  async def exchange(n):
    # The HTTP routes' shape: the reservation is held through response handoff.
    with app_services.ServiceExchangeAdmission(cap) as admission:
      result = await app_services.invoke_service(app, owner, {
        "method": "GET", "path": f"thumb/{n}", "actor": {}, "public": False,
      }, admission=admission)
      handed_off.append(admission.cost)
      await release.wait()
      return result

  parked = []
  try:
    for n in range(4):
      parked.append(asyncio.create_task(exchange(n)))
      while len(handed_off) <= n:
        assert not parked[-1].done(), parked[-1].result()
        await asyncio.sleep(0)
    # Each later call ran while every earlier response was still awaiting
    # handoff, because those hold only their envelope and tiny response.
    assert len(handed_off) == 4 and all(cost < 64 * 1024 for cost in handed_off)
    assert all(3 * cap <= cost < 3 * cap + MIB for cost in executed)
    release.set()
    results = await asyncio.gather(*parked)
  finally:
    release.set()
    for task in parked:
      task.cancel()
    await asyncio.gather(*parked, return_exceptions=True)
  assert [result[:2] for result in results] == [(200, {"ok": True})] * 4
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["self", "public"])
@pytest.mark.parametrize("framing", ["unframed-chunked", "understated-length", "overstated-length"])
async def test_http_body_growth_charges_only_received_bytes_whatever_the_length_header(
  db, auth, route, framing,
):
  import httpx
  from app.main import app as application

  app = _service_app(db, access="public", max_bytes=60 * MIB)
  payload = json.dumps({"payload": "x" * 3000}).encode()
  headers = dict(auth)
  if framing == "understated-length":
    headers["content-length"] = "2"
  elif framing == "overstated-length":
    headers["content-length"] = str(len(payload) + 4096)
  charged = []

  def observe():
    charged.append((app_services._admitted_exchange_cost, app_services._public_ingress_cost))

  async def chunks():
    # Each observation runs when the reader asks for more, after it has
    # admitted and buffered everything sent before.
    for start in range(0, len(payload), 1000):
      observe()
      yield payload[start:start + 1000]
    observe()

  url = f"/api/apps/{app.id}/service/echo" if route == "self" else f"/api/app-services/{app.slug}/echo"
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
    response = await http.post(url, headers=headers, content=chunks())
  assert response.status_code == 201
  assert response.json()["body"] == {"payload": "x" * 3000}
  received = [0, 1000, 2000, 3000, len(payload)]
  assert charged == [
    (3 * size, 3 * size if route == "public" else 0) for size in received
  ], "body admission did not follow the bytes actually received"
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
async def test_missing_length_http2_body_is_read_and_charged_not_treated_as_bodyless():
  from starlette.requests import Request
  from app.routes.app_services import _envelope

  sent = []

  async def receive():
    if not sent:
      sent.append(True)
      return {"type": "http.request", "body": b'{"x":1}', "more_body": False}
    return {"type": "http.disconnect"}

  request = Request({
    "type": "http", "method": "POST", "path": "/echo", "headers": [],
    "query_string": b"", "http_version": "2",
  }, receive)
  with app_services.ServiceExchangeAdmission(1024) as admission:
    envelope = await _envelope(request, "echo", public=False, actor={}, max_bytes=1024, admission=admission)
    assert envelope["body"] == {"x": 1}
    assert admission.transfer_cost == 3 * len(b'{"x":1}')
  assert app_services._admitted_exchange_cost == 0


def _busy_lane_envelope(lane):
  return {
    "schema": 1, "method": "POST", "path": "/tools/echo" if lane == "tools" else "echo",
    "query": {}, "headers": {}, "body": {"value": "ok"}, "public": lane == "public",
    "actor": {},
  }


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["private", "public", "tools", "policy"])
async def test_exhausted_execution_admission_is_retryable_busy_without_side_effects(
  db, auth, monkeypatch, lane,
):
  from app import service_preload

  cap = 4096
  service = b'''import json, sys
request = json.load(sys.stdin)
print(json.dumps({"body": {"path": request["path"]}}))
'''
  app = _service_app(db, access="public", service_bytes=service, max_bytes=cap)
  owner = db.query(models.Owner).first()
  real = {
    "serialize": getattr(app_services, "_serialize_request", None),
    "entry": app_services.service_entry,
    "spawn": asyncio.create_subprocess_exec,
  }
  side_effects = []
  monkeypatch.setattr(
    app_services, "_serialize_request",
    lambda *a: side_effects.append("serialize") or real["serialize"](*a), raising=False,
  )
  monkeypatch.setattr(app_services, "service_entry", lambda *a: side_effects.append("entry") or real["entry"](*a))
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: side_effects.append("preload") or None)

  async def spawn(*args, **kwargs):
    side_effects.append("process")
    return await real["spawn"](*args, **kwargs)

  monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
  # The retained envelope fits; the whole three-copy allowance does not.
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 3 * cap - 1)

  async def call():
    if lane == "policy":
      return await app_services.invoke_policy(app, owner, "echo", {"value": "ok"})
    return await app_services.invoke_service(
      app, owner, _busy_lane_envelope(lane), lane="tools" if lane == "tools" else None,
    )

  with pytest.raises(HTTPException) as caught:
    await call()
  assert caught.value.status_code == 503
  assert caught.value.detail == "App service is busy; retry shortly."
  assert caught.value.headers == {"Retry-After": "1"}
  assert side_effects == [], "a busy exchange serialized or started its service"
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0
  # Busy is truthful: the identical request runs once the allowance is free.
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 512 * MIB)
  result = await call()
  assert side_effects == ["serialize", "entry", "preload", "process"]
  assert (result if lane == "policy" else result[1]) == {"path": _busy_lane_envelope(lane)["path"]}
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.parametrize("route", ["self", "shared", "public"])
def test_http_busy_execution_answers_retry_after_and_starts_no_service(
  client, auth, db, monkeypatch, route,
):
  app = _service_app(db, access="public", max_bytes=4096)
  started = []
  monkeypatch.setattr(app_services, "service_entry", lambda *a: started.append(True))
  monkeypatch.setattr(app_services, "MAX_ADMITTED_EXCHANGE_COST", 3 * 4096 - 1)
  path = {
    "self": f"/api/apps/{app.id}/service/echo",
    "shared": f"/api/services/{app.slug}/echo",
    "public": f"/api/app-services/{app.slug}/echo",
  }[route]
  response = client.post(path, headers=auth, json={"value": "ok"})
  assert response.status_code == 503
  assert response.headers["retry-after"] == "1"
  assert response.json()["detail"] == "App service is busy; retry shortly."
  assert started == []
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["tool", "policy"])
async def test_queued_tool_and_policy_requests_serialize_only_after_execution_admission(
  db, auth, monkeypatch, caller,
):
  from app import service_preload

  cap = 64 * 1024
  app = _service_app(db, max_bytes=cap)
  owner = db.query(models.Owner).first()
  lane = _ParkedLane()
  if caller == "tool":
    monkeypatch.setattr(app_services, "_global_slots", {**app_services._global_slots, "tools": lane})
  else:
    monkeypatch.setattr(app_services, "_app_slots", {(app.id, "private"): lane})
  monkeypatch.setattr(service_preload, "ready_host", lambda *_: None)
  serialized = []
  real_serialize = getattr(app_services, "_serialize_request", None)

  def serialize(envelope, max_bytes):
    serialized.append((max_bytes, app_services._admitted_exchange_cost))
    return real_serialize(envelope, max_bytes)

  async def reply(_python, _entry, _environment, request, _timeout, _max_stdout):
    assert json.loads(request)["body"] == {"text": "x" * 1000}
    return b'{"body":{"ok":true}}', b"", 0

  monkeypatch.setattr(app_services, "_serialize_request", serialize, raising=False)
  monkeypatch.setattr(app_services, "_run_spawned", reply)
  body = {"text": "x" * 1000}
  task = asyncio.create_task(
    app_services.invoke_service(app, owner, {
      "schema": 1, "method": "POST", "path": "/tools/log", "query": {}, "headers": {},
      "body": body, "public": False, "actor": {},
    }, lane="tools") if caller == "tool"
    else app_services.invoke_policy(app, owner, "reviewer/validate", body)
  )
  try:
    await lane.wait_queued(1)
    retained = app_services._admitted_exchange_cost
    assert 1000 < retained < 3 * cap, "a queued call held a transfer allowance"
    assert serialized == [], "a queued call serialized its request"
    lane.release()
    result = await asyncio.wait_for(task, 5)
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert (result if caller == "policy" else result[1]) == {"ok": True}
  assert serialized == [(cap, 3 * cap + retained)]
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
async def test_revoking_a_queued_browser_call_releases_its_envelope_and_starts_nothing(
  db, auth, monkeypatch,
):
  app = _service_app(db, max_bytes=60 * MIB)
  owner = db.query(models.Owner).first()
  grant_id = "c" * 32
  lane = _ParkedLane()
  started = []
  monkeypatch.setattr(app_services, "_app_slots", {(app.id, "private"): lane})
  monkeypatch.setattr(app_services, "_validate_browser_call", lambda *_: None)
  monkeypatch.setattr(app_services, "service_entry", lambda *a: started.append(True))
  task = asyncio.create_task(app_services.invoke_service(app, owner, {
    "method": "GET", "path": "media/1", "actor": {"browser_grant_id": grant_id},
  }))
  try:
    await lane.wait_queued(1)
    assert 0 < app_services._admitted_exchange_cost < 64 * 1024
    assert app_services.browser_grant_has_active_calls(grant_id)
    await app_services.cancel_browser_grant_calls(grant_id)
    assert task.cancelled()
  finally:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
  assert started == []
  assert grant_id not in app_services._browser_calls
  assert app_services._admitted_exchange_cost == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True], ids=["private", "public"])
async def test_disconnect_after_body_growth_releases_the_received_charge(public):
  from starlette.requests import ClientDisconnect, Request
  from app.routes.app_services import _envelope

  observed = []
  messages = iter([
    {"type": "http.request", "body": b'"' + b"x" * 999, "more_body": True},
    {"type": "http.disconnect"},
  ])

  async def receive():
    observed.append((app_services._admitted_exchange_cost, app_services._public_ingress_cost))
    return next(messages)

  request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""}, receive)
  with pytest.raises(ClientDisconnect):
    with app_services.ServiceExchangeAdmission(4096, public_ingress=public) as admission:
      await _envelope(request, "echo", public=public, actor={}, max_bytes=4096, admission=admission)
  assert observed == [(0, 0), (3000, 3000 if public else 0)]
  assert app_services._admitted_exchange_cost == app_services._public_ingress_cost == 0
