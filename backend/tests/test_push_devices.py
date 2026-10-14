"""Native push device registration: upsert, move, rotation and sign-out."""

from __future__ import annotations

import pytest

from app import models

FCM = "fcm:" + "a" * 140
APNS = "ab" * 32


def _put(client, auth, **body):
  return client.put("/api/push/devices", headers=auth, json={"platform": "android", **body})


def _rows(db):
  db.expire_all()
  return db.query(models.PushDevice).order_by(models.PushDevice.created_at).all()


def test_registering_the_same_token_twice_updates_one_device(client, auth, db):
  first = _put(client, auth, token=FCM)
  second = _put(client, auth, token=FCM)

  assert first.status_code == 200 and first.json()["status"] == "registered"
  assert second.json() == {"id": first.json()["id"], "status": "updated"}
  [device] = _rows(db)
  assert device.provider == "fcm" and device.relay_state == "bind_pending"


def test_a_token_registered_under_another_owner_moves_to_the_caller(client, auth, db):
  other = models.Owner(username="previous-owner", hashed_password="x")
  db.add(other)
  db.flush()
  db.add(models.PushDevice(
    id="dev-other", owner_id=other.id, token=FCM, platform="android",
    relay_state="bound",
  ))
  db.commit()

  response = _put(client, auth, token=FCM)

  assert response.json() == {"id": "dev-other", "status": "moved"}
  [device] = _rows(db)
  caller = db.query(models.Owner).filter_by(username="test").one()
  assert device.owner_id == caller.id
  assert device.relay_state == "bind_pending", "the relay must learn the new owner"


def test_rotation_retires_the_replaced_token(client, auth, db):
  _put(client, auth, token=FCM)
  rotated = FCM[:-1] + "b"

  _put(client, auth, token=rotated, replacesToken=FCM)

  states = {row.token: row.relay_state for row in _rows(db)}
  assert states == {FCM: "unbind_pending", rotated: "bind_pending"}
  listed = client.get("/api/push/devices", headers=auth).json()["devices"]
  assert [d["tokenTail"] for d in listed] == [rotated[-6:]]


def test_the_same_apns_token_under_a_new_fcm_token_is_the_same_phone(client, auth, db):
  ios = {"platform": "ios", "apnsToken": APNS.upper(), "apnsEnvironment": "sandbox"}
  _put(client, auth, token=FCM, **ios)
  _put(client, auth, token=FCM[:-1] + "c", **ios)

  rows = _rows(db)
  assert [r.relay_state for r in rows] == ["unbind_pending", "bind_pending"]
  assert rows[1].apns_token == APNS, "APNs tokens are stored lower-case"


def test_a_signed_out_token_that_registers_again_is_revived(client, auth, db):
  _put(client, auth, token=FCM)
  client.request("DELETE", "/api/push/devices", headers=auth, json={"token": FCM})

  assert _put(client, auth, token=FCM).json()["status"] == "updated"
  [device] = _rows(db)
  assert device.disabled_at is None and device.relay_state == "bind_pending"


@pytest.mark.parametrize("body", [
  {"token": "has space"},
  {"token": ""},
  {"token": FCM, "platform": "web"},
  {"token": FCM, "apnsToken": APNS, "apnsEnvironment": "sandbox"},
  {"token": FCM, "platform": "ios", "apnsToken": APNS},
  {"token": FCM, "platform": "ios", "apnsToken": "not-hex", "apnsEnvironment": "sandbox"},
  {"token": FCM, "replacesToken": "two words"},
])
def test_malformed_registrations_are_refused(client, auth, db, body):
  assert _put(client, auth, **body).status_code == 422
  assert _rows(db) == []


def test_sign_out_is_idempotent_and_hides_the_device(client, auth, db):
  _put(client, auth, token=FCM)

  for _ in range(2):
    response = client.request("DELETE", "/api/push/devices", headers=auth, json={"token": FCM})
    assert response.status_code == 204
  assert client.request(
    "DELETE", "/api/push/devices", headers=auth, json={"token": "never-registered"},
  ).status_code == 204
  assert client.get("/api/push/devices", headers=auth).json() == {"devices": []}


def test_the_device_list_never_returns_a_full_token(client, auth):
  _put(client, auth, token=FCM, platform="ios", apnsToken=APNS, apnsEnvironment="production")

  response = client.get("/api/push/devices", headers=auth)

  assert FCM not in response.text and APNS not in response.text
  [device] = response.json()["devices"]
  assert device["tokenTail"] == FCM[-6:] and device["hasApnsToken"] is True


def test_test_notification_needs_an_active_phone_and_then_queues_one(client, auth, db):
  assert client.post("/api/push/devices/test", headers=auth).status_code == 409
  _put(client, auth, token="fcm:disabled-device")
  disabled = db.query(models.PushDevice).one()
  disabled.disabled_at = disabled.created_at
  db.commit()
  assert client.post("/api/push/devices/test", headers=auth).status_code == 409

  _put(client, auth, token=FCM)
  response = client.post("/api/push/devices/test", headers=auth)

  assert response.status_code == 202
  db.expire_all()
  [delivery] = db.query(models.PushDelivery).all()
  assert delivery.notification_id == response.json()["notificationId"]


def test_device_routes_require_authentication(client):
  assert client.get("/api/push/devices").status_code == 401
  assert client.put("/api/push/devices", json={"token": FCM, "platform": "android"}).status_code == 401
