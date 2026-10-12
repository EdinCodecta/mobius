"""Server-owned execution of exact, owner-reviewed app updates.

The Store reviews; this owner durably accepts the selected commits and runs
each one through the same path a single reviewed update takes
(``install.install_requested_app``), independently of any browser. One JSON
receipt per batch records every app's progress and result.

Entries start together, so their Git transfers overlap; each local install
then takes the app lifecycle lock in turn, like any other install. Recovery
never replays an approval: an entry the server could not finish is settled
from the app's durable row, which says whether the reviewed commit landed,
stopped at a conflict, or did not happen.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import time
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import activity, app_git, models
from app.broadcast import get_system_broadcast
from app.database import SessionLocal
from app.resource_access import live_app_or_404
from app.storage_io import atomic_write

log = logging.getLogger(__name__)
_ACTIVE = frozenset({"queued", "applying"})
# SSE transport heartbeat, not an update deadline. A long Python check can run
# without a status change; keep proxies from mistaking that for a dead link.
_STREAM_KEEPALIVE_SECONDS = 15
# Finished receipts are audit and retry history, not state; bound their growth.
_RETAIN_COMPLETE = timedelta(days=30)
_INTERRUPTED = (
  "The server stopped before this update finished, and it was not applied. "
  "Check the app's update again before retrying."
)


class ReviewedAppUpdate(BaseModel):
  model_config = ConfigDict(extra="forbid")
  app_id: int = Field(gt=0)
  manifest_url: str = Field(min_length=1)
  upstream_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
  source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
  capability_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewedUpdateBatch(BaseModel):
  model_config = ConfigDict(extra="forbid")
  id: UUID
  updates: list[ReviewedAppUpdate] = Field(min_length=1)

  @model_validator(mode="after")
  def unique_apps(self):
    if len({item.app_id for item in self.updates}) != len(self.updates):
      raise ValueError("Review each installed app only once per batch.")
    return self


def _now() -> str:
  return datetime.now(UTC).isoformat()


async def install_reviewed_update(update: dict) -> dict:
  """Run one entry through the single reviewed-update path."""
  from app import install

  with SessionLocal() as db:
    result = await install.install_requested_app(
      db,
      manifest_url=update["manifest_url"],
      reviewed_capability_digest=update["capability_digest"],
      reviewed_source_digest=update["source_digest"],
      update_app_id=update["app_id"],
      reviewed_upstream_commit=update["upstream_commit"],
    )
    return {
      "status": "conflict" if result.mode == "conflict" else "updated",
      "version": result.app.version,
      "warnings": result.warnings,
      "error": "",
    }


def _settle_unfinished(record: dict, entries: list[dict] | None = None) -> bool:
  """Settle entries whose worker stopped before recording a result.

  The app row commits in one transaction with the install, so its upstream
  commit says whether the reviewed update landed; a pending-update receipt
  says it stopped at a conflict. Anything else rolled back. Boot settles every
  unfinished entry; a cancelled worker settles only its own.
  """
  from app import install

  unfinished = [
    e for e in (record["entries"] if entries is None else entries)
    if e["status"] in _ACTIVE
  ]
  if not unfinished:
    return False
  reviewed = {u["app_id"]: u["upstream_commit"] for u in record["updates"]}
  with SessionLocal() as db:
    rows = {
      row.id: row for row in db.query(models.App).filter(
        models.App.id.in_([e["app_id"] for e in unfinished]),
        models.App.deleted_at.is_(None),
      )
    }
    for entry in unfinished:
      row = rows.get(entry["app_id"])
      if row is None:
        entry.update(status="failed", error="This app is no longer installed.")
      elif row.upstream_commit == reviewed.get(entry["app_id"]):
        conflict = install.pending_conflict_update_receipt_present(row.source_dir)
        entry.update(
          status="conflict" if conflict else "updated",
          version=row.version, error="",
        )
      else:
        entry.update(status="failed", error=_INTERRUPTED)
  return True


def _resolution_is_accepted(source_dir: str, reviewed_commit: str, accepted_commit: str) -> bool:
  from app.install import pending_conflict_update_receipt_present

  return not pending_conflict_update_receipt_present(source_dir) and (
    app_git.ref_is_ancestor(source_dir, reviewed_commit, accepted_commit) is True
  )


class AppUpdateBatches:
  """One runtime owner; atomic receipts, no polling or browser-owned writes."""

  def __init__(self, data_dir: str, *, execute=install_reviewed_update):
    self.root = Path(data_dir) / "app-update-batches"
    self.execute = execute
    self.records: dict[str, dict] = {}
    self.tasks: dict[str, asyncio.Task] = {}
    self.listeners: dict[str, set[asyncio.Event]] = {}
    self.accepting = False
    self.failure: str | None = None

  def _save(self, record: dict) -> None:
    record["updated_at"] = _now()
    record["phase"] = (
      "applying" if any(e["status"] in _ACTIVE for e in record["entries"])
      else "complete"
    )
    try:
      atomic_write(
        self.root / f"{record['id']}.json",
        json.dumps(record, ensure_ascii=False) + "\n",
        mode=0o600,
      )
    except OSError:
      # Progress that cannot be recorded must not keep admitting work.
      self.accepting = False
      self.failure = "Update progress could not be saved. Check the installed apps before retrying."
      for listeners in self.listeners.values():
        for wake in listeners:
          wake.set()
      raise
    self.records[record["id"]] = record
    for wake in self.listeners.get(record["id"], ()):
      wake.set()

  async def start(self) -> None:
    try:
      await asyncio.to_thread(self._load_receipts)
    except (OSError, ValueError, KeyError, TypeError):
      # A damaged receipt must not keep unrelated chats/apps from booting.
      # Disable only this owner, visibly, and keep the evidence for repair.
      self.failure = "Saved update progress could not be read. Check the installed apps before retrying."
      self.accepting = False
      log.exception("app update progress could not be recovered")
      return
    self.accepting = True

  def _load_receipts(self) -> None:
    self.root.mkdir(parents=True, exist_ok=True)
    expired = (datetime.now(UTC) - _RETAIN_COMPLETE).isoformat()
    for path in self.root.glob("*.json"):
      # A corrupt receipt is not "no work". Fail visibly rather than silently
      # discarding an accepted batch.
      record = json.loads(path.read_text())
      if str(UUID(record["id"])) != path.stem:
        raise ValueError("Invalid app update batch receipt identity.")
      if record["phase"] == "complete" and record["updated_at"] < expired:
        path.unlink()
        continue
      self.records[record["id"]] = record
      if _settle_unfinished(record):
        self._save(record)

  async def stop(self) -> None:
    """Stop admitting, then cancel running entries and wait for them.

    The installer settles its current pass, rolls back, and releases its locks
    only once its threads end; each entry then records its settled outcome.
    """
    self.accepting = False
    tasks = list(self.tasks.values())
    for task in tasks:
      task.cancel()
    if tasks:
      await asyncio.gather(*tasks, return_exceptions=True)

  def submit(self, request: ReviewedUpdateBatch, db) -> dict:
    if not self.accepting:
      raise HTTPException(503, self.failure or "App updates are unavailable while the server restarts.")
    key = str(request.id)
    updates = [item.model_dump() for item in request.updates]
    old = self.records.get(key)
    if old is not None:
      # The same id is the same approval: a repeated click or a retry after a
      # lost response attaches to it rather than starting work twice.
      if old["updates"] != updates:
        raise HTTPException(409, "This batch id already names different reviewed updates.")
      return self.snapshot(key)
    app_ids = {item["app_id"] for item in updates}
    for record in self.records.values():
      if any(e["app_id"] in app_ids and e["status"] in _ACTIVE for e in record["entries"]):
        raise HTTPException(409, detail={
          "code": "app_update_running", "batch_id": record["id"],
          "message": "An update for this app is already running. Open its progress.",
        })
    entries = []
    for item in updates:
      app = live_app_or_404(db, item["app_id"])
      entries.append({
        "app_id": app.id, "name": app.name, "status": "queued",
        "version": app.version, "error": "", "warnings": [],
        "resolver_chat_id": None,
      })
    record = {
      "id": key, "created_at": _now(), "updates": updates, "entries": entries,
    }
    # No await between the durable receipt, the owned task and the response.
    self._save(record)
    task = asyncio.create_task(self._run(record), name=f"app-update-batch:{key}")
    self.tasks[key] = task
    task.add_done_callback(lambda done: self._finished(key, done))
    return self.snapshot(key)

  def _finished(self, key: str, task: asyncio.Task) -> None:
    self.tasks.pop(key, None)
    if not task.cancelled() and task.exception() is not None:
      log.error("app update batch %s stopped: %s", key, task.exception())

  def snapshot(self, key: str) -> dict:
    if self.failure:
      raise HTTPException(503, self.failure)
    record = self.records.get(key)
    if record is None:
      raise HTTPException(404, "App update batch not found.")
    return deepcopy({k: v for k, v in record.items() if k != "updates"})

  def recent(self) -> list[dict]:
    records = sorted(self.records.values(), key=lambda r: r["created_at"], reverse=True)
    latest_complete = next((r["id"] for r in records if r["phase"] == "complete"), None)
    visible = []
    newer_apps = set()
    for record in records:
      # A newer attempt replaces an app's older actionable result, but an
      # unrelated batch must never hide an unresolved conflict or failure.
      actionable = any(
        e["status"] in {"conflict", "failed"} and e["app_id"] not in newer_apps
        for e in record["entries"]
      )
      if record["phase"] == "applying" or actionable or record["id"] == latest_complete:
        visible.append(self.snapshot(record["id"]))
      newer_apps.update(e["app_id"] for e in record["entries"])
    return visible

  async def refresh_results(self, key: str, db) -> None:
    """Project resolver progress from accepted source, not a browser guess."""
    record = self.records.get(key)
    if record is None:
      raise HTTPException(404, "App update batch not found.")
    conflicts = [e for e in record["entries"] if e["status"] == "conflict"]
    if not conflicts:
      return
    rows = db.query(
      models.App.id, models.App.source_dir, models.App.source_commit,
      models.App.version, models.App.conflict_resolver_chat_id,
      models.App.conflict_resolver_upstream_commit,
    ).filter(
      models.App.id.in_([e["app_id"] for e in conflicts]),
      models.App.deleted_at.is_(None),
    ).all()
    current = {row.id: row._asdict() for row in rows}
    db.rollback()  # release the read before Git work
    updates = {u["app_id"]: u for u in record["updates"]}
    changed = False
    for entry in conflicts:
      row = current.get(entry["app_id"])
      if row is None:
        entry.update(status="failed", error="This app is no longer installed.")
        changed = True
        continue
      commit = updates[entry["app_id"]]["upstream_commit"]
      resolver = (
        row["conflict_resolver_chat_id"]
        if row["conflict_resolver_upstream_commit"] == commit else None
      )
      if entry.get("resolver_chat_id") != resolver:
        entry["resolver_chat_id"] = resolver
        changed = True
      # The accepted source commit names the served revision; `main` can hold
      # unapplied edits and is therefore no evidence that resolution finished.
      if row["source_commit"]:
        try:
          contained = await asyncio.to_thread(
            _resolution_is_accepted, row["source_dir"], commit, row["source_commit"],
          )
        except (OSError, RuntimeError):
          contained = False
        if contained:
          entry.update(status="updated", version=row["version"], error="", resolver_chat_id=None)
          changed = True
    if changed:
      self._save(record)

  async def snapshots(self, key: str):
    """Coalesce slow readers to the newest durable snapshot, never a backlog."""
    self.snapshot(key)
    wake = asyncio.Event()
    self.listeners.setdefault(key, set()).add(wake)
    try:
      while True:
        wake.clear()
        snapshot = self.snapshot(key)
        yield snapshot
        if snapshot["phase"] == "complete":
          return
        try:
          await asyncio.wait_for(wake.wait(), timeout=_STREAM_KEEPALIVE_SECONDS)
        except asyncio.TimeoutError:
          yield None
    finally:
      self.listeners[key].discard(wake)
      if not self.listeners[key]:
        self.listeners.pop(key)

  async def _run(self, record: dict) -> None:
    async def one(update, entry):
      started = time.monotonic()
      entry["status"] = "applying"
      self._save(record)
      try:
        outcome = await self.execute(update)
        entry.update(outcome)
      except asyncio.CancelledError:
        # Shutdown: the installer has settled. Record what actually landed.
        await asyncio.to_thread(_settle_unfinished, record, [entry])
        self._save(record)
        raise
      except HTTPException as exc:
        detail = exc.detail
        entry.update(status="failed", error=(
          detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
        ))
      except Exception:
        log.exception("app update failed for %s", update["app_id"])
        entry.update(status="failed", error="This update could not finish. Check it again before retrying.")
      entry["duration_ms"] = round((time.monotonic() - started) * 1000)
      self._save(record)
      activity.log_event(
        "app_update_outcome", batch_id=record["id"], app_id=entry["app_id"],
        outcome=entry["status"], duration_ms=entry["duration_ms"],
      )
      get_system_broadcast().publish({"type": "app_updated", "appId": str(entry["app_id"])})

    # Independent owner-selected apps: one failure never stops its siblings.
    results = await asyncio.gather(
      *(one(u, e) for u, e in zip(record["updates"], record["entries"])),
      return_exceptions=True,
    )
    for result in results:
      if isinstance(result, BaseException):
        raise result
