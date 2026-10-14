"""Native push to the Möbius mobile app: device registry, outbox and delivery.

The phone's web page registers its push token (``routes/push.py``). Every
owner notification that survives the presence and quiet-maintenance gates in
``push._prepare_owner_notification`` is queued here as one ``PushDelivery``
row per active device, in the request path and without network I/O. A single
supervised worker (``run_delivery_worker``) owns all relay traffic: it binds
newly registered tokens to this installation, sends due deliveries in batches
through ``push_relay`` and settles each target's outcome:

- ``ok`` → delivered; device send statistics updated;
- ``invalid_token`` → the device is disabled (an APNs rejection first falls
  back to FCM, since the FCM token may still be valid);
- ``retry`` → backed off, honoring the relay's ``retryAfter``, until the
  notification's time-to-live or the attempt budget runs out;
- ``error`` → failed and reported once an hour in the owner's bell.

Never log a full token or notification content: device ids and short codes
only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models, push_relay
from app.config import get_settings
from app.timeutil import now_naive_utc

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 24 * 3600
MAX_SEND_ATTEMPTS = 8
MAX_UNBIND_ATTEMPTS = 5
SWEEP_INTERVAL_SECONDS = 30.0
# A claimed row whose worker died mid-send is retried once its lease lapses.
LEASE_SECONDS = 120
CLAIM_LIMIT = 500
BINDING_CLAIM_LIMIT = 100
REJECTED_BIND_RETRY_SECONDS = 3600
DELIVERY_RETENTION = timedelta(days=7)
DISABLED_DEVICE_RETENTION = timedelta(days=30)
RETENTION_INTERVAL_SECONDS = 3600.0
ALERT_INTERVAL_SECONDS = 3600.0
# APNs caps a collapse id at 64 bytes; FCM accepts the same key.
_COLLAPSE_KEY_MAX = 64
_TERMINAL_STATUSES = ("ok", "invalid", "failed", "expired")
_QUIET_SOURCE_TYPE = "push_relay"


def token_fingerprint(token: str) -> str:
  """A short, non-reversible label for logs."""
  return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def collapse_key_for(tag: str | None, notification_id: str) -> str:
  """The notification's OS grouping key: its namespaced tag, else its own id.

  A retried send that the relay could not deduplicate then replaces the
  earlier notification on the phone instead of stacking a second copy.
  """
  key = tag or notification_id
  if len(key) <= _COLLAPSE_KEY_MAX:
    return key
  return "c:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:_COLLAPSE_KEY_MAX - 2]


def relay_target_id(notification_id: str, device_id: str, transport: str) -> str:
  """The relay's duplicate-detection id for one delivery over one transport.

  Stable across retries, unique forever (unlike a reusable row id), and
  different after an APNs-to-FCM fallback so that resend is not dropped.
  """
  material = f"{notification_id}\0{device_id}\0{transport}".encode("utf-8")
  return "t" + hashlib.sha256(material).hexdigest()[:32]


def _backoff_seconds(attempt: int) -> float:
  base = min(5.0 * (2 ** max(attempt - 1, 0)), 3600.0)
  return base * random.uniform(0.8, 1.2)


# ---- worker wake-up --------------------------------------------------------

_wake_event: asyncio.Event | None = None
_wake_loop: asyncio.AbstractEventLoop | None = None


def wake() -> None:
  """Nudge the delivery worker; safe from request threads and the loop."""
  loop, event = _wake_loop, _wake_event
  if loop is None or event is None:
    return
  try:
    loop.call_soon_threadsafe(event.set)
  except RuntimeError:
    # The loop is shutting down; the next boot's sweep picks the work up.
    pass


# ---- device registry (request path) ----------------------------------------


@dataclass(frozen=True)
class DeviceRegistration:
  token: str
  platform: str
  apns_token: str | None = None
  apns_environment: str | None = None
  replaces_token: str | None = None


def _mark_unbind(device: models.PushDevice, now: datetime, reason: str) -> None:
  device.relay_state = "unbind_pending"
  device.relay_attempts = 0
  device.relay_next_attempt_at = now
  if device.disabled_at is None:
    device.disabled_at = now
    device.disabled_reason = reason


def register_device(
  db: Session, owner_id: int, registration: DeviceRegistration,
) -> tuple[models.PushDevice, str]:
  """Upsert a device by token and attach it to ``owner_id``.

  Returns the device and ``registered``, ``moved`` (the token belonged to
  another owner) or ``updated``. A new, moved, revived or changed token is
  re-bound at the relay before anything is sent to it.
  """
  now = now_naive_utc()
  device = db.query(models.PushDevice).filter(
    models.PushDevice.token == registration.token,
  ).one_or_none()
  if device is None:
    device = models.PushDevice(
      id=str(uuid.uuid4()),
      owner_id=owner_id,
      provider="fcm",
      token=registration.token,
      platform=registration.platform,
      apns_token=registration.apns_token,
      apns_environment=registration.apns_environment,
      created_at=now,
      relay_state="bind_pending",
      relay_attempts=0,
      relay_next_attempt_at=now,
    )
    db.add(device)
    outcome = "registered"
  else:
    outcome = "moved" if device.owner_id != owner_id else "updated"
    changed = (
      outcome == "moved"
      or device.relay_state != "bound"
      or device.disabled_at is not None
      or device.platform != registration.platform
      or device.apns_token != registration.apns_token
      or device.apns_environment != registration.apns_environment
    )
    device.owner_id = owner_id
    device.platform = registration.platform
    device.apns_token = registration.apns_token
    device.apns_environment = registration.apns_environment
    device.disabled_at = None
    device.disabled_reason = None
    if changed:
      device.relay_state = "bind_pending"
      device.relay_attempts = 0
      device.relay_next_attempt_at = now
  device.last_seen_at = now

  if registration.replaces_token and registration.replaces_token != registration.token:
    previous = db.query(models.PushDevice).filter(
      models.PushDevice.token == registration.replaces_token,
      models.PushDevice.owner_id == owner_id,
    ).one_or_none()
    if previous is not None:
      _mark_unbind(previous, now, "replaced")
  if registration.apns_token:
    # Same phone, rotated FCM token: the APNs token identifies the hardware.
    for duplicate in db.query(models.PushDevice).filter(
      models.PushDevice.apns_token == registration.apns_token,
      models.PushDevice.token != registration.token,
      models.PushDevice.relay_state != "unbind_pending",
    ):
      _mark_unbind(duplicate, now, "replaced")
  db.commit()
  wake()
  return device, outcome


def forget_device(db: Session, owner_id: int, token: str) -> bool:
  """Stop sending to a signed-out device and unbind it at the relay."""
  device = db.query(models.PushDevice).filter(
    models.PushDevice.token == token,
    models.PushDevice.owner_id == owner_id,
  ).one_or_none()
  if device is None or device.relay_state == "unbind_pending":
    return False
  _mark_unbind(device, now_naive_utc(), "signed_out")
  db.commit()
  wake()
  return True


def has_active_device(db: Session, owner_id: int) -> bool:
  return db.query(models.PushDevice.id).filter(
    models.PushDevice.owner_id == owner_id,
    models.PushDevice.disabled_at.is_(None),
    models.PushDevice.relay_state != "unbind_pending",
  ).first() is not None


def list_devices(db: Session, owner_id: int) -> list[dict]:
  rows = db.query(models.PushDevice).filter(
    models.PushDevice.owner_id == owner_id,
    models.PushDevice.relay_state != "unbind_pending",
  ).order_by(models.PushDevice.created_at).all()
  return [
    {
      "id": row.id,
      "platform": row.platform,
      "provider": row.provider,
      "tokenTail": row.token[-6:],
      "hasApnsToken": bool(row.apns_token),
      "relayState": row.relay_state,
      "createdAt": row.created_at,
      "lastSeenAt": row.last_seen_at,
      "lastSuccessAt": row.last_success_at,
      "disabledAt": row.disabled_at,
      "disabledReason": row.disabled_reason,
    }
    for row in rows
  ]


# ---- outbox (called from push._prepare_owner_notification) -----------------


def _transport_for(device: models.PushDevice) -> str:
  if (
    get_settings().push_ios_transport == "apns"
    and device.platform == "ios"
    and device.apns_token
  ):
    return "apns"
  return "fcm"


def queue_notification(
  db: Session, owner_id: int, notification_id: str, *, collapse_key: str,
  ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> int:
  """Queue one notification for every active device; returns the row count."""
  devices = db.query(models.PushDevice).filter(
    models.PushDevice.owner_id == owner_id,
    models.PushDevice.disabled_at.is_(None),
    models.PushDevice.relay_state != "unbind_pending",
  ).all()
  if not devices:
    return 0
  now = now_naive_utc()
  for device in devices:
    db.add(models.PushDelivery(
      notification_id=notification_id,
      device_id=device.id,
      transport=_transport_for(device),
      status="pending",
      attempts=0,
      next_attempt_at=now,
      expires_at=now + timedelta(seconds=ttl_seconds),
      collapse_key=collapse_key,
      created_at=now,
    ))
  db.commit()
  wake()
  return len(devices)


# ---- delivery worker -------------------------------------------------------


@dataclass(frozen=True)
class _Work:
  delivery_id: int
  device_id: str
  notification_id: str
  target: push_relay.RelayTarget
  title: str
  body: str
  url: str
  collapse_key: str
  ttl_seconds: int


def _session() -> Session:
  from app.database import SessionLocal
  return SessionLocal()


def _absolute_url(target: str | None) -> str:
  """The tap target on this installation's own origin, never another site."""
  origin = get_settings().frontend_origin.rstrip("/")
  if not isinstance(target, str) or not target.startswith("/") or target.startswith("//"):
    target = "/shell/"
  return origin + target


def _claim_due_deliveries() -> list[_Work]:
  works: list[_Work] = []
  with _session() as db:
    now = now_naive_utc()
    rows = db.query(models.PushDelivery).filter(or_(
      (models.PushDelivery.status.in_(("pending", "retry")))
      & (models.PushDelivery.next_attempt_at <= now),
      (models.PushDelivery.status == "sending")
      & (models.PushDelivery.lease_until < now),
    )).order_by(models.PushDelivery.id).limit(CLAIM_LIMIT).all()
    for row in rows:
      if row.expires_at <= now:
        row.status, row.lease_until = "expired", None
        continue
      notification = db.get(models.Notification, row.notification_id)
      if notification is None:
        # Cleared from history before it was sent: nothing left to show.
        db.delete(row)
        continue
      device = db.get(models.PushDevice, row.device_id)
      if device is None or device.disabled_at is not None:
        row.status, row.lease_until, row.last_error = "failed", None, "device_disabled"
        continue
      if device.relay_state != "bound":
        # The relay must learn this token is ours before it will accept a send.
        row.status, row.lease_until = "pending", None
        row.next_attempt_at = now + timedelta(seconds=SWEEP_INTERVAL_SECONDS)
        continue
      row.status = "sending"
      row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
      row.attempts += 1
      if row.transport == "apns" and not device.apns_token:
        row.transport = "fcm"
      target_id = relay_target_id(row.notification_id, device.id, row.transport)
      if row.transport == "apns":
        target = push_relay.RelayTarget(
          id=target_id, provider="apns", token=device.apns_token,
          environment=device.apns_environment or "production",
        )
      else:
        target = push_relay.RelayTarget(id=target_id, provider="fcm", token=device.token)
      works.append(_Work(
        delivery_id=row.id,
        device_id=device.id,
        notification_id=notification.id,
        target=target,
        title=notification.title,
        body=notification.body or "",
        url=_absolute_url(notification.target),
        collapse_key=row.collapse_key,
        ttl_seconds=max(1, int((row.expires_at - now).total_seconds())),
      ))
    db.commit()
  return works


def _record_outcomes(
  works: list[_Work], outcomes: list[push_relay.TargetOutcome],
) -> list[str]:
  """Settle each target; returns the error codes the owner should hear about."""
  alerts: list[str] = []
  counts = {"ok": 0, "invalid": 0, "retry": 0, "failed": 0}
  with _session() as db:
    now = now_naive_utc()
    for work, outcome in zip(works, outcomes):
      row = db.get(models.PushDelivery, work.delivery_id)
      if row is None:
        continue
      device = db.get(models.PushDevice, work.device_id)
      row.lease_until = None
      if device is not None:
        device.last_sent_at = now
      if outcome.status == "ok":
        row.status, row.last_error = "ok", None
        if device is not None:
          device.last_success_at = now
        counts["ok"] += 1
      elif outcome.status == "invalid_token" and row.transport == "apns":
        # A sandbox/production mix-up or a stale APNs token says nothing about
        # the FCM token: drop the APNs path and resend through FCM.
        if device is not None:
          device.apns_token = device.apns_environment = None
          device.relay_state = "bind_pending"
          device.relay_attempts = 0
          device.relay_next_attempt_at = now
        row.transport, row.status = "fcm", "retry"
        row.next_attempt_at, row.last_error = now, "apns_invalid_token"
        counts["retry"] += 1
      elif outcome.status == "invalid_token":
        row.status, row.last_error = "invalid", "invalid_token"
        if device is not None and device.disabled_at is None:
          device.disabled_at, device.disabled_reason = now, "invalid_token"
          logger.info(
            "native push: disabled device %s (token %s) after invalid_token",
            device.id[:8], token_fingerprint(device.token),
          )
        counts["invalid"] += 1
      elif outcome.status == "retry" and row.attempts < MAX_SEND_ATTEMPTS:
        delay = max(outcome.retry_after or 0.0, _backoff_seconds(row.attempts))
        next_attempt = now + timedelta(seconds=delay)
        row.last_error = (outcome.error or "retry")[:64]
        if next_attempt >= row.expires_at:
          row.status = "expired"
          counts["failed"] += 1
        else:
          row.status, row.next_attempt_at = "retry", next_attempt
          counts["retry"] += 1
      else:
        code = (
          "retry_exhausted" if outcome.status == "retry"
          else (outcome.error or "relay_error")
        )[:64]
        row.status, row.last_error = "failed", code
        if outcome.status == "error":
          alerts.append(code)
        counts["failed"] += 1
    db.commit()
  logger.info(
    "native push: notification %s ok=%d retry=%d invalid=%d failed=%d",
    works[0].notification_id[:8] if works else "-",
    counts["ok"], counts["retry"], counts["invalid"], counts["failed"],
  )
  return alerts


async def _send_batch(works: list[_Work]) -> None:
  first = works[0]
  message = push_relay.RelayMessage(
    title=first.title,
    body=first.body,
    data={"url": first.url, "notificationId": first.notification_id},
    collapse_key=first.collapse_key,
    ttl_seconds=min(work.ttl_seconds for work in works),
  )
  try:
    outcomes = await push_relay.send([work.target for work in works], message)
  except push_relay.RelayUnavailable as exc:
    outcomes = [
      push_relay.TargetOutcome("retry", retry_after=exc.retry_after, error=exc.code)
    ] * len(works)
  except push_relay.RelayRejected as exc:
    outcomes = [push_relay.TargetOutcome("error", error=exc.code)] * len(works)
  alerts = await asyncio.to_thread(_record_outcomes, works, outcomes)
  for code in dict.fromkeys(alerts):
    await asyncio.to_thread(_alert_owner, code)


def _batches(works: list[_Work]) -> list[list[_Work]]:
  """One relay request carries one notification; split by the batch limit."""
  limit = get_settings().push_relay_max_targets
  grouped: dict[str, list[_Work]] = {}
  for work in works:
    grouped.setdefault(work.notification_id, []).append(work)
  return [
    group[start:start + limit]
    for group in grouped.values()
    for start in range(0, len(group), limit)
  ]


# ---- relay bindings --------------------------------------------------------


@dataclass(frozen=True)
class _Binding:
  device_id: str
  state: str
  token: str
  platform: str
  apns_token: str | None
  apns_environment: str | None
  attempts: int


def _claim_due_bindings() -> list[_Binding]:
  with _session() as db:
    now = now_naive_utc()
    rows = db.query(models.PushDevice).filter(
      models.PushDevice.relay_state.in_(("bind_pending", "unbind_pending")),
      or_(
        models.PushDevice.relay_next_attempt_at.is_(None),
        models.PushDevice.relay_next_attempt_at <= now,
      ),
    ).limit(BINDING_CLAIM_LIMIT).all()
    claimed = []
    for row in rows:
      row.relay_attempts += 1
      row.relay_next_attempt_at = now + timedelta(seconds=LEASE_SECONDS)
      claimed.append(_Binding(
        row.id, row.relay_state, row.token, row.platform,
        row.apns_token, row.apns_environment, row.relay_attempts,
      ))
    db.commit()
  return claimed


def _record_binding(binding: _Binding, result: str, *, retry_after: float | None = None) -> None:
  """Apply one bind/unbind result unless the device changed meanwhile."""
  with _session() as db:
    row = db.get(models.PushDevice, binding.device_id)
    if row is None or (
      row.relay_state, row.token, row.apns_token, row.apns_environment, row.platform,
    ) != (
      binding.state, binding.token, binding.apns_token,
      binding.apns_environment, binding.platform,
    ):
      # Re-registered while in flight: its own fresh bind is already queued.
      return
    now = now_naive_utc()
    finished_unbind = binding.state == "unbind_pending" and (
      result == "ok" or binding.attempts >= MAX_UNBIND_ATTEMPTS
    )
    if finished_unbind:
      # Unbinding is courtesy cleanup: after the budget the row goes anyway,
      # since this installation will never send to it again.
      db.query(models.PushDelivery).filter(
        models.PushDelivery.device_id == row.id,
      ).delete(synchronize_session=False)
      db.delete(row)
    elif result == "ok":
      row.relay_state, row.relay_attempts, row.relay_next_attempt_at = "bound", 0, None
    elif result == "rejected" and binding.state == "bind_pending":
      row.relay_next_attempt_at = now + timedelta(seconds=REJECTED_BIND_RETRY_SECONDS)
    else:
      delay = max(retry_after or 0.0, _backoff_seconds(binding.attempts))
      row.relay_next_attempt_at = now + timedelta(seconds=delay)
    db.commit()


async def _sync_bindings() -> None:
  for binding in await asyncio.to_thread(_claim_due_bindings):
    retry_after = None
    try:
      if binding.state == "bind_pending":
        await push_relay.bind_device(
          provider="fcm", token=binding.token, platform=binding.platform,
          apns_token=binding.apns_token, apns_environment=binding.apns_environment,
        )
      else:
        await push_relay.unbind_device(token=binding.token)
      result = "ok"
    except push_relay.RelayUnavailable as exc:
      result, retry_after = "retry", exc.retry_after
    except push_relay.RelayRejected as exc:
      result = "rejected"
      if binding.state == "bind_pending":
        await asyncio.to_thread(_alert_owner, exc.code)
    await asyncio.to_thread(_record_binding, binding, result, retry_after=retry_after)


# ---- retention and alerts --------------------------------------------------


def _purge_old_rows() -> None:
  with _session() as db:
    now = now_naive_utc()
    db.query(models.PushDelivery).filter(
      models.PushDelivery.status.in_(_TERMINAL_STATUSES),
      models.PushDelivery.created_at < now - DELIVERY_RETENTION,
    ).delete(synchronize_session=False)
    stale = [
      device_id for (device_id,) in db.query(models.PushDevice.id).filter(
        models.PushDevice.disabled_at < now - DISABLED_DEVICE_RETENTION,
        models.PushDevice.relay_state != "unbind_pending",
      )
    ]
    if stale:
      db.query(models.PushDelivery).filter(
        models.PushDelivery.device_id.in_(stale),
      ).delete(synchronize_session=False)
      db.query(models.PushDevice).filter(
        models.PushDevice.id.in_(stale),
      ).delete(synchronize_session=False)
    db.commit()


_ALERT_BODIES = {
  "not_linked": (
    "Phone notifications need this Möbius to be linked to your mobius.you "
    "account. They stay in your notification history meanwhile."
  ),
}
_last_alert: dict[str, float] = {}


def _alert_owner(code: str) -> None:
  """Tell the owner in the bell, at most hourly per cause; never as a push."""
  last = _last_alert.get(code)
  if last is not None and time.monotonic() - last < ALERT_INTERVAL_SECONDS:
    return
  _last_alert[code] = time.monotonic()
  logger.warning("native push relay problem: %s", code)
  from app.push import notify_owner
  with _session() as db:
    owner = db.query(models.Owner).first()
    if owner is None:
      return
    notify_owner(
      db, owner.id,
      title="Phone notifications aren't being delivered",
      body=_ALERT_BODIES.get(code, (
        f"The mobius.you push relay refused them ({code}). They stay in your "
        "notification history."
      )),
      source_type=_QUIET_SOURCE_TYPE,
    )


# ---- the supervised loop ---------------------------------------------------


async def deliver_due() -> None:
  """One full pass: settle bindings first, then send every due delivery."""
  await _sync_bindings()
  while True:
    works = await asyncio.to_thread(_claim_due_deliveries)
    for batch in _batches(works):
      await _send_batch(batch)
    if len(works) < CLAIM_LIMIT:
      return


async def run_delivery_worker() -> None:
  """Own every relay request for the life of the server process."""
  global _wake_event, _wake_loop
  _wake_event = asyncio.Event()
  _wake_loop = asyncio.get_running_loop()
  last_purge = 0.0
  try:
    while True:
      _wake_event.clear()
      try:
        await deliver_due()
        if time.monotonic() - last_purge >= RETENTION_INTERVAL_SECONDS:
          await asyncio.to_thread(_purge_old_rows)
          last_purge = time.monotonic()
      except asyncio.CancelledError:
        raise
      except Exception:
        logger.exception("native push delivery pass failed")
      try:
        async with asyncio.timeout(SWEEP_INTERVAL_SECONDS):
          await _wake_event.wait()
      except TimeoutError:
        pass
  finally:
    _wake_event = None
    _wake_loop = None
