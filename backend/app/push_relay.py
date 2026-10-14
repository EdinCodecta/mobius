"""Client for the mobius.you native push relay — the whole wire contract.

The relay holds the FCM and APNs credentials for every Möbius installation, so
this installation keeps no push secret. Each request goes over the root-owned
identity broker's Unix socket; the broker mints a short-lived capability bound
to this installation, its linked mobius.you account, the exact method, path,
body hash and idempotency key, and forwards the request to the relay. See
``PUSH_ROUTES`` in ``backend/runtime/identity_broker.py``.

Routes (all POST, JSON, ``Idempotency-Key`` required):

``/v1/push/devices/bind``   ``{provider, token, platform, apnsToken?, apnsEnvironment?}``
    Record that this installation now owns the token. The last installation
    to bind a token owns it, so a phone that moves to another Möbius stops
    receiving this one's notifications. Binding is a set operation, so each
    call carries a fresh idempotency key: a repeat is harmless, and a re-bind
    after the phone moved away and back must not be answered from the relay's
    idempotency cache.
``/v1/push/devices/unbind`` ``{token}``
``/v1/push/send``
    ``{targets: [{id, provider: "fcm", token} |
                 {id, provider: "apns", token, environment}],
       notification: {title, body},
       data: {url, notificationId, ...strings},
       options: {collapseKey, ttlSeconds, priority}}``
    → ``{results: [{index, status: ok|invalid_token|retry|error,
                    retryAfter?, error?}]}``
    Each target ``id`` is stable across retries of the same delivery over the
    same transport, so the relay can drop a duplicate.

Whole-request outcomes are exceptions: ``RelayUnavailable`` (network, 429,
5xx; retry later) and ``RelayRejected`` (any other refusal — not linked,
route unknown, malformed request; retrying the same request cannot succeed).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Literal

import httpx

from app.runtime_identity import broker_async_client


SEND_PATH = "/v1/push/send"
BIND_PATH = "/v1/push/devices/bind"
UNBIND_PATH = "/v1/push/devices/unbind"
# The broker mints a capability at mobius.you, then forwards to the relay with
# its own 30 second ceiling; this bound covers both hops.
_REQUEST_TIMEOUT = httpx.Timeout(40.0, connect=3.0)
_MAX_RETRY_AFTER_SECONDS = 24 * 3600

TargetStatus = Literal["ok", "invalid_token", "retry", "error"]


@dataclass(frozen=True)
class RelayTarget:
  id: str
  provider: Literal["fcm", "apns"]
  token: str
  environment: str | None = None

  def wire(self) -> dict[str, str]:
    value = {"id": self.id, "provider": self.provider, "token": self.token}
    if self.provider == "apns":
      value["environment"] = self.environment or "production"
    return value


@dataclass(frozen=True)
class RelayMessage:
  title: str
  body: str
  data: dict[str, str]
  collapse_key: str
  ttl_seconds: int
  priority: Literal["high", "normal"] = "high"


@dataclass(frozen=True)
class TargetOutcome:
  status: TargetStatus
  retry_after: float | None = None
  error: str | None = None


class RelayUnavailable(Exception):
  """The relay could not be reached or asked to come back later."""

  def __init__(self, code: str, *, retry_after: float | None = None) -> None:
    super().__init__(code)
    self.code = code
    self.retry_after = retry_after


class RelayRejected(Exception):
  """The relay or broker refused the request; the same request cannot succeed."""

  def __init__(self, code: str, *, status: int) -> None:
    super().__init__(code)
    self.code = code
    self.status = status


def _encode(value: dict) -> bytes:
  return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _idempotency_key(kind: str, *parts: str) -> str:
  digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
  return f"push-{kind}:{digest[:48]}"


def _fresh_key(kind: str) -> str:
  return f"push-{kind}:{uuid.uuid4().hex}"


def _retry_after(response: httpx.Response) -> float | None:
  value = response.headers.get("retry-after")
  if value is None:
    return None
  try:
    seconds = float(value)
  except ValueError:
    return None
  return min(max(seconds, 0.0), _MAX_RETRY_AFTER_SECONDS)


async def _post(path: str, payload: dict, *, idempotency_key: str) -> httpx.Response:
  body = _encode(payload)
  try:
    async with broker_async_client(timeout=_REQUEST_TIMEOUT) as client:
      response = await client.post(
        path,
        content=body,
        headers={
          "Content-Type": "application/json",
          "Accept": "application/json",
          "Idempotency-Key": idempotency_key,
        },
      )
  except (httpx.HTTPError, OSError) as exc:
    raise RelayUnavailable(f"unreachable:{type(exc).__name__}") from None
  if response.status_code == 429 or response.status_code >= 500:
    raise RelayUnavailable(
      f"http_{response.status_code}", retry_after=_retry_after(response),
    )
  if response.status_code >= 400:
    # 401 means this installation is not linked to a mobius.you account (the
    # broker cannot mint a capability) or the relay refused the capability.
    code = "not_linked" if response.status_code == 401 else f"http_{response.status_code}"
    raise RelayRejected(code, status=response.status_code)
  return response


async def bind_device(
  *, provider: str, token: str, platform: str,
  apns_token: str | None, apns_environment: str | None,
) -> None:
  payload: dict[str, str] = {"provider": provider, "token": token, "platform": platform}
  if apns_token:
    payload["apnsToken"] = apns_token
    payload["apnsEnvironment"] = apns_environment or "production"
  await _post(BIND_PATH, payload, idempotency_key=_fresh_key("bind"))


async def unbind_device(*, token: str) -> None:
  await _post(UNBIND_PATH, {"token": token}, idempotency_key=_fresh_key("unbind"))


def _parse_results(payload: object, count: int) -> list[TargetOutcome]:
  """Map the relay's results onto targets; a missing or malformed one is an error.

  Never default a target to ``ok``: an unconfirmed send must stay visible.
  """
  outcomes = [TargetOutcome("error", error="missing_result")] * count
  results = payload.get("results") if isinstance(payload, dict) else None
  if not isinstance(results, list):
    return outcomes
  for item in results:
    if not isinstance(item, dict):
      continue
    index = item.get("index")
    status = item.get("status")
    if (
      not isinstance(index, int) or isinstance(index, bool)
      or not 0 <= index < count
      or status not in ("ok", "invalid_token", "retry", "error")
    ):
      continue
    retry_after = item.get("retryAfter")
    if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)):
      retry_after = None
    else:
      retry_after = min(max(float(retry_after), 0.0), _MAX_RETRY_AFTER_SECONDS)
    error = item.get("error")
    outcomes[index] = TargetOutcome(
      status,
      retry_after=retry_after,
      error=error[:64] if isinstance(error, str) else None,
    )
  return outcomes


async def send(
  targets: list[RelayTarget], message: RelayMessage,
) -> list[TargetOutcome]:
  """Send one notification to up to the relay's batch limit of targets."""
  payload = {
    "targets": [target.wire() for target in targets],
    "notification": {"title": message.title, "body": message.body},
    "data": {key: str(value) for key, value in message.data.items()},
    "options": {
      "collapseKey": message.collapse_key,
      "ttlSeconds": message.ttl_seconds,
      "priority": message.priority,
    },
  }
  response = await _post(
    SEND_PATH, payload,
    idempotency_key=_idempotency_key("send", *sorted(t.id for t in targets)),
  )
  try:
    parsed = response.json()
  except ValueError:
    parsed = None
  return _parse_results(parsed, len(targets))
