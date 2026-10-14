"""Push subscription (Web Push) and native mobile device endpoints."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models, push_relay
from app.database import get_db
from app.deps import get_current_owner_for_lifecycle_control, reject_cross_site
from app.push import get_public_key_base64url
from app.schemas import (
  PushDeviceForgetRequest,
  PushDeviceRegisterRequest,
  PushSubscribeRequest,
  PushUnsubscribeRequest,
)
from app.timeutil import now_naive_utc

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
  """Register this phone; a token another owner had moves to the caller.

  The relay learns that this installation owns the token before it is
  stored, so a device row always means the relay will accept sends to it.
  The page re-registers on every load, which also re-claims a phone that
  was bound to another installation in between.
  """
  try:
    push_relay.bind_device(
      token=body.token, platform=body.platform,
      apns_token=body.apns_token, apns_environment=body.apns_environment,
    )
  except push_relay.RelayError as exc:
    if exc.not_linked:
      raise HTTPException(
        409, "Link this Möbius to your mobius.you account to get phone notifications.",
      )
    raise HTTPException(502, "The notification service could not be reached.")
  now = now_naive_utc()
  device = db.query(models.PushDevice).filter(
    models.PushDevice.token == body.token,
  ).one_or_none()
  if device is None:
    device = models.PushDevice(id=str(uuid.uuid4()), token=body.token, created_at=now)
    db.add(device)
  device.owner_id = owner.id
  device.platform = body.platform
  device.apns_token = body.apns_token
  device.apns_environment = body.apns_environment
  device.last_seen_at = now
  db.commit()
  return {"status": "registered"}


@router.delete(
  "/devices", status_code=204, dependencies=[Depends(reject_cross_site)],
)
def forget_device(
  body: PushDeviceForgetRequest,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  """Stop sending to this phone (sign-out); repeating it is harmless."""
  removed = db.query(models.PushDevice).filter(
    models.PushDevice.token == body.token,
    models.PushDevice.owner_id == owner.id,
  ).delete(synchronize_session=False)
  db.commit()
  if removed:
    try:
      push_relay.unbind_device(body.token)
    except push_relay.RelayError:
      # Nothing is sent to a removed device; the relay keeps only a binding.
      pass
