"""Review-bound batch admission and recoverable per-app progress."""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.app_update_batches import AppUpdateBatches, ReviewedUpdateBatch
from app.database import get_db
from app.deps import (
  get_owner_or_app_with_manage_apps, get_principal, Principal,
  reject_cross_site, require_nondelegated_owner_or_app_control,
  revocable_browser_stream,
)

router = APIRouter(prefix="/update-batches")


def batches(request: Request) -> AppUpdateBatches:
  supervisors = getattr(request.app.state, "runtime_supervisors", None)
  owner = getattr(supervisors, "app_updates", None)
  if owner is not None and owner.failure:
    raise HTTPException(503, owner.failure)
  if owner is None or not owner.accepting:
    raise HTTPException(503, "App update progress is unavailable while the server restarts.")
  return owner


@router.post("", dependencies=[
  Depends(reject_cross_site),
  Depends(require_nondelegated_owner_or_app_control),
  Depends(get_owner_or_app_with_manage_apps),
])
async def start_batch(
  body: ReviewedUpdateBatch, request: Request, db: Session = Depends(get_db),
):
  return batches(request).submit(body, db)


@router.get("", dependencies=[Depends(get_owner_or_app_with_manage_apps)])
async def list_batches(request: Request, db: Session = Depends(get_db)):
  owner = batches(request)
  for snapshot in owner.recent():
    await owner.refresh_results(snapshot["id"], db)
  return {"batches": owner.recent()}


@router.get("/{batch_id}", dependencies=[Depends(get_owner_or_app_with_manage_apps)])
async def get_batch(batch_id: UUID, request: Request, db: Session = Depends(get_db)):
  owner = batches(request)
  await owner.refresh_results(str(batch_id), db)
  return owner.snapshot(str(batch_id))


@router.get("/{batch_id}/events", dependencies=[Depends(get_owner_or_app_with_manage_apps)])
async def batch_events(
  batch_id: UUID, request: Request,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  owner = batches(request)
  await owner.refresh_results(str(batch_id), db)
  db.close()

  async def generate():
    async for snapshot in owner.snapshots(str(batch_id)):
      yield ": keepalive\n\n" if snapshot is None else f"data: {json.dumps(snapshot)}\n\n"

  return StreamingResponse(
    revocable_browser_stream(generate(), principal),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
  )
