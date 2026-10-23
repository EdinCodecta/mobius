"""Deprecated: the retired per-app secret routes, answered by the key vault.

Apps written for ``/api/apps/{id}/secrets/{name}`` keep working for one
release while they move to the vault handshake (``/api/keys``). Each app's
secret name maps to a vault key granted to that app (``key_vault.app_key_names``),
the same mapping the one-time import uses, so imported and newly saved values
meet in one place.

What changed for callers: an app may still save, check and delete its own
values, but no app token reads one back any more, including the app's own
service and jobs. They borrow the key through ``/api/keys/use`` instead. The
owner and the owner's agents may still read, as before.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import key_vault, models
from app.database import get_db
from app.deps import (
  Principal,
  get_principal,
  reject_cross_site,
  require_nondelegated_owner_or_app_control,
)

router = APIRouter(prefix="/api/apps", tags=["app-secrets (deprecated)"])

_DEPRECATION = {
  "Deprecation": "true",
  "Link": '</api/keys/requests>; rel="successor-version"',
  "Cache-Control": "no-store",
}


class SecretWrite(BaseModel):
  value: str = Field(min_length=1, max_length=8192)


def _authorize_app(db: Session, principal: Principal, app_id: int) -> models.App:
  if principal.scope not in ("owner", "app"):
    raise HTTPException(status_code=403, detail="This token cannot access app secrets.")
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(status_code=403, detail="Apps can access only their own secrets.")
  app = db.query(models.App).filter(
    models.App.id == app_id, models.App.deleted_at.is_(None),
  ).first()
  if app is None:
    raise HTTPException(status_code=404, detail="App not found.")
  return app


async def _granted(app_id: int, name: str) -> str | None:
  try:
    return await key_vault.granted_key(app_id, name)
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc


@router.put(
  "/{app_id}/secrets/{name}",
  status_code=204,
  dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)],
)
async def put_secret(
  app_id: int, name: str, body: SecretWrite,
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal),
):
  _authorize_app(db, principal, app_id)
  try:
    await key_vault.store_app_key(app_id, name, body.value)
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  except key_vault.VaultRefused as exc:
    raise HTTPException(exc.status, exc.reason) from exc
  return Response(status_code=204, headers=_DEPRECATION)


@router.head("/{app_id}/secrets/{name}")
async def secret_exists(
  app_id: int, name: str,
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal),
):
  _authorize_app(db, principal, app_id)
  if await _granted(app_id, name) is None:
    raise HTTPException(status_code=404, detail="Secret not found.")
  return Response(status_code=204, headers=_DEPRECATION)


@router.get("/{app_id}/secrets/{name}")
async def get_secret(
  app_id: int, name: str,
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal),
):
  if principal.app_id is not None:
    raise HTTPException(
      status_code=403,
      detail="Apps never read key values. Borrow the key through /api/keys/use "
      "(see 'Keys' in the building-apps guide).",
    )
  _authorize_app(db, principal, app_id)
  key = await _granted(app_id, name)
  if key is None:
    raise HTTPException(status_code=404, detail="Secret not found.")
  caller = f"agent:{principal.chat_id}" if principal.chat_id else "owner"
  try:
    value = (await key_vault.vault_call("GET", f"/keys/{key}/value", caller=caller))["value"]
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  return Response(content=value, media_type="text/plain", headers=_DEPRECATION)


@router.delete(
  "/{app_id}/secrets/{name}",
  status_code=204,
  dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)],
)
async def delete_secret(
  app_id: int, name: str,
  db: Session = Depends(get_db), principal: Principal = Depends(get_principal),
):
  """Delete the app's key, or only its grant when other apps share the key."""
  _authorize_app(db, principal, app_id)
  try:
    keys = (await key_vault.vault_call("GET", "/keys"))["keys"]
    name_in_vault = key_vault.granted_key_in(keys, app_id, name)
    if name_in_vault is None:
      raise HTTPException(status_code=404, detail="Secret not found.")
    key = next(k for k in keys if k["name"] == name_in_vault)
    others = [a for a in key["grants"]["apps"] if a != app_id]
    if others:
      await key_vault.vault_call("PUT", f"/keys/{name_in_vault}", {
        "inject": key["inject"], "grants": {**key["grants"], "apps": others},
      })
    else:
      await key_vault.vault_call("DELETE", f"/keys/{name_in_vault}")
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  return Response(status_code=204, headers=_DEPRECATION)
