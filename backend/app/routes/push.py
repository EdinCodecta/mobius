"""Push subscription (Web Push) and native mobile device endpoints."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, push_native
from app.database import get_db
from app.deps import get_current_owner_for_lifecycle_control, reject_cross_site
from app.push import get_public_key_base64url, notify_owner
from app.schemas import (
  PushDeviceForgetRequest,
  PushDeviceRegisterRequest,
  PushSubscribeRequest,
  PushUnsubscribeRequest,
)

router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/vapid-key")
def vapid_key():
  """Return the VAPID public key for browser push subscription."""
  return {"publicKey": get_public_key_base64url()}


@router.post(
  "/subscribe", status_code=201, dependencies=[Depends(reject_cross_site)]
)
def subscribe(
  body: PushSubscribeRequest,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Register or update a push subscription.

  A subscription receives every owner notification, so apps and delegated
  agents cannot register or remove one.
  """
  existing = (
    db.query(models.PushSubscription)
    .filter(models.PushSubscription.endpoint == body.endpoint)
    .first()
  )
  if existing:
    existing.p256dh = body.keys.p256dh
    existing.auth = body.keys.auth
  else:
    sub = models.PushSubscription(
      id=str(uuid.uuid4()),
      owner_id=owner.id,
      endpoint=body.endpoint,
      p256dh=body.keys.p256dh,
      auth=body.keys.auth,
      created_at=datetime.now(UTC),
    )
    db.add(sub)
  db.commit()
  return {"status": "subscribed"}


@router.delete(
  "/subscribe", status_code=204, dependencies=[Depends(reject_cross_site)]
)
def unsubscribe(
  body: PushUnsubscribeRequest,
  _: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Remove a push subscription."""
  db.query(models.PushSubscription).filter(
    models.PushSubscription.endpoint == body.endpoint
  ).delete()
  db.commit()


# Native push devices: the Möbius mobile app's web page registers the token
# that ``MobiusNative.registerPush()`` returned. Like a Web Push subscription,
# a device receives every owner notification, so only the owner may manage
# one. Tokens travel in request bodies, never in URLs, to stay out of logs.


@router.put("/devices", dependencies=[Depends(reject_cross_site)])
def register_device(
  body: PushDeviceRegisterRequest,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Register or refresh this phone; a token another owner had moves here."""
  device, outcome = push_native.register_device(
    db, owner.id, push_native.DeviceRegistration(
      token=body.token,
      platform=body.platform,
      apns_token=body.apns_token,
      apns_environment=body.apns_environment,
      replaces_token=body.replaces_token,
    ),
  )
  return {"id": device.id, "status": outcome}


@router.delete(
  "/devices", status_code=204, dependencies=[Depends(reject_cross_site)],
)
def forget_device(
  body: PushDeviceForgetRequest,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Stop sending to this phone (sign-out); repeating it is harmless."""
  push_native.forget_device(db, owner.id, body.token)


@router.get("/devices")
def list_devices(
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """This owner's phones, with only the last characters of each token."""
  return {"devices": push_native.list_devices(db, owner.id)}


@router.post(
  "/devices/test", status_code=202, dependencies=[Depends(reject_cross_site)],
)
def send_test_notification(
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Send a real notification through every channel to check delivery."""
  if not push_native.has_active_device(db, owner.id):
    raise HTTPException(409, "No phone is registered for notifications.")
  notification_id = notify_owner(
    db, owner.id,
    title="Test notification",
    body="Phone notifications from Möbius are working.",
    source_type="system",
    target="/shell/",
  )
  return {"notificationId": notification_id}
