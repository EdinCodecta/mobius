"""Client for the mobius.you native push relay — the whole wire contract.

The relay holds the FCM and APNs credentials for every Möbius installation, so
this installation keeps no push secret. Requests go over the root-owned
identity broker's Unix socket; the broker mints a short-lived capability bound
to this installation, its linked mobius.you account and the exact request, and
forwards it to the relay (``PUSH_ROUTES`` in
``backend/runtime/identity_broker.py``). Every route needs an
``Idempotency-Key``; each call uses a fresh one because nothing here retries.

``POST /v1/push/devices/bind``   ``{provider, token, platform, apnsToken?, apnsEnvironment?}``
    This installation now owns the token; the last installation to bind it
    wins, so a phone that moves to another Möbius stops getting this one's.
``POST /v1/push/devices/unbind`` ``{token}``
``POST /v1/push/send``
    ``{targets: [{provider: "fcm", token}], notification: {title, body},
       data: {url, notificationId}, options: {collapseKey, ttlSeconds, priority}}``
    → ``{results: [{index, status: ok|invalid_token|retry|error}]}``
"""

from __future__ import annotations

import json
import uuid

import httpx

from app.runtime_identity import broker_client


BIND_PATH = "/v1/push/devices/bind"
UNBIND_PATH = "/v1/push/devices/unbind"
SEND_PATH = "/v1/push/send"
# The broker mints a capability at mobius.you, then forwards to the relay
# with its own 30 second ceiling; this bound covers both hops.
_TIMEOUT = httpx.Timeout(35.0, connect=3.0)
_STATUSES = frozenset({"ok", "invalid_token", "retry", "error"})


class RelayError(Exception):
  """The relay could not be reached or refused the request."""

  def __init__(self, code: str, *, status: int | None = None) -> None:
    super().__init__(code)
    self.code = code
    self.status = status

  @property
  def not_linked(self) -> bool:
    # The broker cannot mint a capability until a mobius.you account is linked.
    return self.status == 401


def _post(path: str, payload: dict) -> httpx.Response:
  body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
  try:
    with broker_client(timeout=_TIMEOUT) as client:
      response = client.post(path, content=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Idempotency-Key": f"push:{uuid.uuid4().hex}",
      })
  except (httpx.HTTPError, OSError) as exc:
    raise RelayError(f"unreachable:{type(exc).__name__}") from None
  if response.status_code >= 400:
    raise RelayError(f"http_{response.status_code}", status=response.status_code)
  return response


def bind_device(
  *, token: str, platform: str,
  apns_token: str | None = None, apns_environment: str | None = None,
) -> None:
  payload = {"provider": "fcm", "token": token, "platform": platform}
  if apns_token:
    payload["apnsToken"] = apns_token
    payload["apnsEnvironment"] = apns_environment or "production"
  _post(BIND_PATH, payload)


def unbind_device(token: str) -> None:
  _post(UNBIND_PATH, {"token": token})


def send(
  tokens: list[str], *, title: str, body: str, data: dict[str, str],
  collapse_key: str, ttl_seconds: int = 24 * 3600,
) -> list[str]:
  """Send one notification; returns one status per token, in order.

  A missing or malformed result counts as ``error``, never ``ok``.
  """
  response = _post(SEND_PATH, {
    "targets": [{"provider": "fcm", "token": token} for token in tokens],
    "notification": {"title": title, "body": body},
    "data": {key: str(value) for key, value in data.items()},
    "options": {
      "collapseKey": collapse_key, "ttlSeconds": ttl_seconds, "priority": "high",
    },
  })
  statuses = ["error"] * len(tokens)
  try:
    results = response.json().get("results")
  except (ValueError, AttributeError):
    return statuses
  for item in results if isinstance(results, list) else ():
    index = item.get("index") if isinstance(item, dict) else None
    if (
      isinstance(index, int) and not isinstance(index, bool)
      and 0 <= index < len(tokens) and item.get("status") in _STATUSES
    ):
      statuses[index] = item["status"]
  return statuses
