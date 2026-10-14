"""Native push outbox and delivery worker outcomes (relay faked at its seam)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import models, push_native, push_relay
from app.config import get_settings
from app.push import notify_owner
from app.timeutil import now_naive_utc


class FakeRelay:
  def __init__(self, monkeypatch):
    self.sends = []
    self.binds = []
    self.unbinds = []
    self.outcomes = None
    self.send_error = None
    self.bind_error = None

    async def send(targets, message):
      self.sends.append((list(targets), message))
      if self.send_error is not None:
        raise self.send_error
      if self.outcomes:
        return self.outcomes.pop(0)
      return [push_relay.TargetOutcome("ok")] * len(targets)

    async def bind_device(**kwargs):
      self.binds.append(kwargs)
      if self.bind_error is not None:
        raise self.bind_error

    async def unbind_device(*, token):
      self.unbinds.append(token)

    monkeypatch.setattr(push_relay, "send", send)
    monkeypatch.setattr(push_relay, "bind_device", bind_device)
    monkeypatch.setattr(push_relay, "unbind_device", unbind_device)


@pytest.fixture
def relay(monkeypatch):
  push_native._last_alert.clear()
  return FakeRelay(monkeypatch)


@pytest.fixture
def owner(db, auth):
  # notify_owner closes the session it is handed, so keep only the id.
  del auth
  return SimpleNamespace(id=db.query(models.Owner).one().id)


def _device(db, owner, token="fcm-token-1", *, platform="android", bound=True, **fields):
  device, _ = push_native.register_device(db, owner.id, push_native.DeviceRegistration(
    token=token, platform=platform,
    apns_token=fields.pop("apns_token", None),
    apns_environment=fields.pop("apns_environment", None),
  ))
  if bound:
    device.relay_state = "bound"
    db.commit()
  return device


def _notify(db, owner, **fields):
  values = {"title": "Done", "body": "Ready.", "source_type": "agent", "target": "/shell/?chat=c1"}
  values.update(fields)
  return notify_owner(db, owner.id, **values)


def _pass():
  asyncio.run(push_native.deliver_due())


def _deliveries(db):
  db.expire_all()
  return db.query(models.PushDelivery).order_by(models.PushDelivery.id).all()


def _make_due(db):
  for row in _deliveries(db):
    row.next_attempt_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()


def test_a_notification_queues_one_delivery_per_active_device(db, owner, relay):
  _device(db, owner, "token-a")
  _device(db, owner, "token-b")
  disabled = _device(db, owner, "token-c")
  disabled.disabled_at = now_naive_utc()
  db.commit()

  _notify(db, owner)

  assert sorted(row.device_id for row in _deliveries(db)) == sorted(
    d.id for d in db.query(models.PushDevice).filter(models.PushDevice.disabled_at.is_(None))
  )


def test_quiet_maintenance_notices_never_reach_the_phone(db, owner, relay):
  _device(db, owner)
  _notify(db, owner, source_type="platform_update")
  _notify(db, owner, source_type="push_relay")

  assert _deliveries(db) == []


def test_a_new_token_is_bound_to_this_installation_before_its_first_send(db, owner, relay):
  _device(db, owner, bound=False)
  _notify(db, owner)

  _pass()

  assert [b["token"] for b in relay.binds] == ["fcm-token-1"]
  assert len(relay.sends) == 1
  assert _deliveries(db)[0].status == "ok"


def test_a_device_whose_bind_is_refused_is_not_sent_to(db, owner, relay):
  _device(db, owner, bound=False)
  relay.bind_error = push_relay.RelayRejected("not_linked", status=401)
  _notify(db, owner)

  _pass()

  assert relay.sends == []
  row = _deliveries(db)[0]
  assert row.status == "pending" and row.next_attempt_at > now_naive_utc()
  device = db.query(models.PushDevice).one()
  assert device.relay_state == "bind_pending"
  assert device.relay_next_attempt_at > now_naive_utc() + timedelta(minutes=30)
  alert = db.query(models.Notification).filter_by(source_type="push_relay").one()
  assert "linked" in alert.body


def test_ok_records_delivery_and_device_statistics(db, owner, relay):
  _device(db, owner)
  notification_id = _notify(db, owner)

  _pass()

  targets, message = relay.sends[0]
  assert message.data["notificationId"] == notification_id
  assert message.data["url"].endswith("/shell/?chat=c1")
  row = _deliveries(db)[0]
  assert row.status == "ok" and row.attempts == 1
  assert targets[0].id == push_native.relay_target_id(notification_id, row.device_id, "fcm")
  device = db.query(models.PushDevice).one()
  assert device.last_success_at is not None and device.last_sent_at is not None


def test_retry_honors_retry_after_and_resends_the_same_target_id(db, owner, relay):
  _device(db, owner)
  _notify(db, owner)
  relay.outcomes = [
    [push_relay.TargetOutcome("retry", retry_after=600)],
    [push_relay.TargetOutcome("ok")],
  ]

  _pass()
  row = _deliveries(db)[0]
  assert row.status == "retry"
  assert row.next_attempt_at >= now_naive_utc() + timedelta(seconds=590)
  _pass()
  assert len(relay.sends) == 1, "not due yet"

  _make_due(db)
  _pass()

  first, second = (s[0][0].id for s in relay.sends)
  assert first == second == push_native.relay_target_id(row.notification_id, row.device_id, "fcm")
  assert _deliveries(db)[0].status == "ok"


def test_a_whole_request_outage_retries_every_target(db, owner, relay):
  _device(db, owner, "token-a")
  _device(db, owner, "token-b")
  _notify(db, owner)
  relay.send_error = push_relay.RelayUnavailable("http_503", retry_after=30)

  _pass()

  assert [row.status for row in _deliveries(db)] == ["retry", "retry"]


def test_retry_that_would_land_after_the_ttl_expires_instead(db, owner, relay):
  _device(db, owner)
  _notify(db, owner)
  row = _deliveries(db)[0]
  row.expires_at = now_naive_utc() + timedelta(seconds=60)
  db.commit()
  relay.outcomes = [[push_relay.TargetOutcome("retry", retry_after=3600)]]

  _pass()

  assert _deliveries(db)[0].status == "expired"


def test_retries_stop_after_the_attempt_budget(db, owner, relay):
  _device(db, owner)
  _notify(db, owner)
  row = _deliveries(db)[0]
  row.attempts = push_native.MAX_SEND_ATTEMPTS - 1
  db.commit()
  relay.outcomes = [[push_relay.TargetOutcome("retry")]]

  _pass()

  row = _deliveries(db)[0]
  assert row.status == "failed" and row.last_error == "retry_exhausted"


def test_invalid_fcm_token_disables_the_device_and_later_notifications_skip_it(
  db, owner, relay,
):
  _device(db, owner)
  _notify(db, owner)
  relay.outcomes = [[push_relay.TargetOutcome("invalid_token")]]

  _pass()

  assert _deliveries(db)[0].status == "invalid"
  device = db.query(models.PushDevice).one()
  assert device.disabled_reason == "invalid_token"
  _notify(db, owner, title="Second")
  _pass()
  assert len(_deliveries(db)) == 1 and len(relay.sends) == 1


def test_invalid_apns_token_falls_back_to_fcm_instead_of_disabling(db, owner, relay, monkeypatch):
  monkeypatch.setattr(get_settings(), "push_ios_transport", "apns")
  _device(
    db, owner, platform="ios", apns_token="ab" * 32, apns_environment="sandbox",
  )
  _notify(db, owner)
  relay.outcomes = [[push_relay.TargetOutcome("invalid_token")]]

  _pass()

  apns_target = relay.sends[0][0][0]
  assert apns_target.provider == "apns"
  device = db.query(models.PushDevice).one()
  assert device.disabled_at is None and device.apns_token is None
  assert device.relay_state == "bind_pending"
  _pass()  # re-binds without the APNs token, then resends through FCM
  fcm_target = relay.sends[1][0][0]
  assert fcm_target.provider == "fcm"
  assert fcm_target.id != apns_target.id, "the fallback resend is not a duplicate"
  assert _deliveries(db)[0].status == "ok"


def test_relay_error_fails_the_target_and_tells_the_owner_once_in_the_bell(db, owner, relay):
  _device(db, owner)
  _notify(db, owner)
  _notify(db, owner, title="Second")
  relay.send_error = push_relay.RelayRejected("http_403", status=403)

  _pass()

  assert [row.status for row in _deliveries(db)] == ["failed", "failed"]
  alerts = db.query(models.Notification).filter_by(source_type="push_relay").all()
  assert len(alerts) == 1
  # The alert is bell-only: it queued nothing for the phone.
  assert len(_deliveries(db)) == 2


def test_an_expired_lease_is_reclaimed_after_a_crash_mid_send(db, owner, relay):
  _device(db, owner)
  _notify(db, owner)
  row = _deliveries(db)[0]
  row.status, row.attempts = "sending", 1
  row.lease_until = now_naive_utc() - timedelta(seconds=1)
  db.commit()

  _pass()

  row = _deliveries(db)[0]
  assert row.status == "ok" and row.attempts == 2
  assert relay.sends[0][0][0].id == push_native.relay_target_id(
    row.notification_id, row.device_id, "fcm",
  )


def test_a_live_lease_is_not_sent_twice(db, owner, relay):
  _device(db, owner)
  _notify(db, owner)
  row = _deliveries(db)[0]
  row.status = "sending"
  row.lease_until = now_naive_utc() + timedelta(seconds=60)
  db.commit()

  _pass()

  assert relay.sends == []


def test_a_notification_cleared_before_sending_is_dropped(db, owner, relay):
  _device(db, owner)
  notification_id = _notify(db, owner)
  db.query(models.Notification).filter_by(id=notification_id).delete()
  db.commit()

  _pass()

  assert relay.sends == [] and _deliveries(db) == []


def test_one_request_per_notification_split_by_the_relay_batch_limit(
  db, owner, relay, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "push_relay_max_targets", 2)
  for index in range(5):
    _device(db, owner, f"token-{index}")
  _notify(db, owner)
  _notify(db, owner, title="Second")

  _pass()

  assert sorted(len(targets) for targets, _ in relay.sends) == [1, 1, 2, 2, 2, 2]
  assert all(row.status == "ok" for row in _deliveries(db))


def test_a_signed_out_device_is_unbound_then_removed(db, owner, relay):
  device_id = _device(db, owner).id
  assert push_native.forget_device(db, owner.id, "fcm-token-1") is True
  assert push_native.forget_device(db, owner.id, "fcm-token-1") is False
  _notify(db, owner)
  assert _deliveries(db) == []

  _pass()

  assert relay.unbinds == ["fcm-token-1"]
  assert db.get(models.PushDevice, device_id) is None


def test_re_registering_during_an_unbind_keeps_the_device(db, owner, relay):
  device = _device(db, owner)
  push_native.forget_device(db, owner.id, "fcm-token-1")
  binding = asyncio.run(asyncio.to_thread(push_native._claim_due_bindings))[0]
  _device(db, owner, bound=False)

  push_native._record_binding(binding, "ok")

  db.expire_all()
  row = db.get(models.PushDevice, device.id)
  assert row is not None and row.relay_state == "bind_pending" and row.disabled_at is None


def test_retention_purges_old_settled_rows_and_long_disabled_devices(db, owner, relay):
  device_id = _device(db, owner).id
  _notify(db, owner)
  _pass()
  old = now_naive_utc() - timedelta(days=40)
  row = _deliveries(db)[0]
  row.created_at = old
  device = db.get(models.PushDevice, device_id)
  device.disabled_at = old
  db.commit()

  push_native._purge_old_rows()

  db.expire_all()
  assert _deliveries(db) == [] and db.query(models.PushDevice).count() == 0


def test_collapse_key_fits_apns_and_defaults_to_the_notification_id():
  assert push_native.collapse_key_for(None, "n-1") == "n-1"
  assert push_native.collapse_key_for("agent:c:done", "n-1") == "agent:c:done"
  long_key = push_native.collapse_key_for("app:7:" + "x" * 200, "n-1")
  assert len(long_key) == 64 and long_key.startswith("c:")
