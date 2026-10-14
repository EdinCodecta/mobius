"""The mobius.you push relay wire contract, against a fake relay.

``app.push_relay`` reaches the relay only through the identity broker's
socket, so these tests replace that client with an in-process fake relay and
check exactly what crosses the wire and how each reply is classified.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import models, push_native, push_relay
from app.push import notify_owner


def _use_relay(monkeypatch, handler):
  def client(*, timeout):
    del timeout
    return httpx.AsyncClient(
      base_url="http://broker", transport=httpx.MockTransport(handler),
    )
  monkeypatch.setattr(push_relay, "broker_async_client", client)


def _message(**overrides):
  values = {
    "title": "Done", "body": "Your app is ready.",
    "data": {"url": "https://mobius.example/shell/?chat=c1", "notificationId": "n1"},
    "collapse_key": "agent:c1:done", "ttl_seconds": 3600,
  }
  values.update(overrides)
  return push_relay.RelayMessage(**values)


def test_send_carries_stable_target_ids_string_data_and_an_idempotency_key(monkeypatch):
  seen = []

  def relay(request):
    seen.append(request)
    return httpx.Response(200, json={"results": [
      {"index": 0, "status": "ok"},
      {"index": 1, "status": "invalid_token"},
    ]})

  _use_relay(monkeypatch, relay)
  targets = [
    push_relay.RelayTarget(id="d1", provider="fcm", token="fcm-token"),
    push_relay.RelayTarget(id="d2", provider="apns", token="ab" * 32, environment="sandbox"),
  ]
  outcomes = asyncio.run(push_relay.send(targets, _message(data={"url": "u", "n": 3})))

  assert [o.status for o in outcomes] == ["ok", "invalid_token"]
  request = seen[0]
  assert request.method == "POST" and request.url.path == "/v1/push/send"
  body = json.loads(request.content)
  assert body["targets"] == [
    {"id": "d1", "provider": "fcm", "token": "fcm-token"},
    {"id": "d2", "provider": "apns", "token": "ab" * 32, "environment": "sandbox"},
  ]
  assert body["notification"] == {"title": "Done", "body": "Your app is ready."}
  assert body["data"] == {"url": "u", "n": "3"}
  assert body["options"] == {
    "collapseKey": "agent:c1:done", "ttlSeconds": 3600, "priority": "high",
  }
  key = request.headers["idempotency-key"]
  assert key.startswith("push-send:")
  # The same targets in any order are the same request.
  asyncio.run(push_relay.send(list(reversed(targets)), _message()))
  assert seen[1].headers["idempotency-key"] == key


def test_a_missing_or_malformed_result_is_an_error_never_a_silent_success(monkeypatch):
  _use_relay(monkeypatch, lambda request: httpx.Response(200, json={"results": [
    {"index": 1, "status": "ok"},
    {"index": 0, "status": "delivered"},
    {"index": 9, "status": "ok"},
    {"index": True, "status": "ok"},
  ]}))
  targets = [
    push_relay.RelayTarget(id=f"d{i}", provider="fcm", token=f"t{i}") for i in range(3)
  ]
  outcomes = asyncio.run(push_relay.send(targets, _message()))

  assert [o.status for o in outcomes] == ["error", "ok", "error"]
  assert outcomes[0].error == "missing_result"

  _use_relay(monkeypatch, lambda request: httpx.Response(200, content=b"not json"))
  assert [o.status for o in asyncio.run(push_relay.send(targets[:1], _message()))] == ["error"]


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_rate_limits_and_server_errors_are_retryable_and_honor_retry_after(monkeypatch, status):
  _use_relay(monkeypatch, lambda request: httpx.Response(status, headers={"Retry-After": "42"}))
  target = push_relay.RelayTarget(id="d1", provider="fcm", token="t")

  with pytest.raises(push_relay.RelayUnavailable) as raised:
    asyncio.run(push_relay.send([target], _message()))
  assert raised.value.retry_after == 42.0
  assert raised.value.code == f"http_{status}"


def test_an_unreachable_broker_is_retryable(monkeypatch):
  def relay(request):
    raise httpx.ConnectError("no socket", request=request)

  _use_relay(monkeypatch, relay)
  with pytest.raises(push_relay.RelayUnavailable):
    asyncio.run(push_relay.send(
      [push_relay.RelayTarget(id="d1", provider="fcm", token="t")], _message(),
    ))


@pytest.mark.parametrize(("status", "code"), [
  (401, "not_linked"), (403, "http_403"), (404, "http_404"), (400, "http_400"),
])
def test_refusals_are_not_retried(monkeypatch, status, code):
  _use_relay(monkeypatch, lambda request: httpx.Response(status, json={"error": "no"}))

  with pytest.raises(push_relay.RelayRejected) as raised:
    asyncio.run(push_relay.bind_device(
      provider="fcm", token="t", platform="android", apns_token=None, apns_environment=None,
    ))
  assert raised.value.code == code


def test_bind_and_unbind_send_only_what_the_relay_needs(monkeypatch):
  seen = []

  def relay(request):
    seen.append((request.url.path, json.loads(request.content), request.headers["idempotency-key"]))
    return httpx.Response(200, json={})

  _use_relay(monkeypatch, relay)
  asyncio.run(push_relay.bind_device(
    provider="fcm", token="t", platform="ios",
    apns_token="ab" * 32, apns_environment="production",
  ))
  asyncio.run(push_relay.unbind_device(token="t"))

  assert seen[0][0] == "/v1/push/devices/bind"
  assert seen[0][1] == {
    "provider": "fcm", "token": "t", "platform": "ios",
    "apnsToken": "ab" * 32, "apnsEnvironment": "production",
  }
  assert seen[0][2].startswith("push-bind:")
  assert seen[1][:2] == ("/v1/push/devices/unbind", {"token": "t"})
  assert seen[1][2].startswith("push-unbind:")


def test_a_repeated_bind_is_never_answered_from_the_relay_idempotency_cache(monkeypatch):
  # A phone that moved to another Möbius and back must be re-bound for real.
  keys = []

  def relay(request):
    keys.append(request.headers["idempotency-key"])
    return httpx.Response(200, json={})

  _use_relay(monkeypatch, relay)
  for _ in range(2):
    asyncio.run(push_relay.bind_device(
      provider="fcm", token="t", platform="android", apns_token=None, apns_environment=None,
    ))
  assert keys[0] != keys[1]


def test_registered_phone_receives_an_owner_notification_end_to_end(
  client, auth, db, monkeypatch,
):
  """Register → notify → worker pass → fake relay → settled outcome."""
  relay_log = []
  bound = set()

  def relay(request):
    body = json.loads(request.content)
    relay_log.append((request.url.path, body))
    if request.url.path == "/v1/push/devices/bind":
      bound.add(body["token"])
      return httpx.Response(200, json={})
    assert request.url.path == "/v1/push/send"
    return httpx.Response(200, json={"results": [
      {"index": i, "status": "ok" if t["token"] in bound else "invalid_token"}
      for i, t in enumerate(body["targets"])
    ]})

  _use_relay(monkeypatch, relay)
  token = "fcm:" + "x" * 140
  response = client.put("/api/push/devices", headers=auth, json={
    "token": token, "platform": "android",
  })
  assert response.status_code == 200, response.text

  owner = db.query(models.Owner).one()
  notification_id = notify_owner(
    db, owner.id, title="Build finished", body="Open it when ready.",
    source_type="agent", target="/shell/?chat=chat-1",
  )
  asyncio.run(push_native.deliver_due())

  paths = [path for path, _ in relay_log]
  assert paths == ["/v1/push/devices/bind", "/v1/push/send"]
  sent = relay_log[1][1]
  assert sent["notification"] == {"title": "Build finished", "body": "Open it when ready."}
  assert sent["data"]["notificationId"] == notification_id
  assert sent["data"]["url"].endswith("/shell/?chat=chat-1")
  assert sent["data"]["url"].startswith("http")
  db.expire_all()
  delivery = db.query(models.PushDelivery).one()
  assert delivery.status == "ok"
  device = db.query(models.PushDevice).one()
  assert device.relay_state == "bound" and device.last_success_at is not None
