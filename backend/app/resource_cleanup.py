"""Owner-run cleanup of disposable agent files, measured before it runs.

The Settings "Storage and Memory" box uses this when agents cannot start, so
nothing here may need an agent. Each target delegates to the module that
already owns that folder's retention (scratch sweep, browser-profile quota,
provider-session retention); this module only runs it now, at the owner's
request, and reports what it freed. A preview uses the same selection rules,
so the confirm step states what the cleanup then deletes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.capacity import remeasure_domains

log = logging.getLogger(__name__)

# Old provider sessions only cost a warm resume (Möbius reseeds a fresh session
# from the chat's own transcript), so the owner's Clear keeps just two days.
SESSION_KEEP_DAYS = 2


class CleanupBusy(Exception):
  """Another owner cleanup is still running."""


@dataclass(frozen=True)
class CleanupTarget:
  # Capacity domains this cleanup shrinks; re-measured after it runs.
  domains: tuple[str, ...]
  preview: Callable[[str], Awaitable[dict]]
  clear: Callable[[str], Awaitable[dict]]


def _alive_chat_ids() -> set[str]:
  """Chats with a live or starting runner, read on the event loop."""
  from app.runner_registry import registry

  return registry.all_alive_chat_ids()


def _alive_chat_ids_from_thread() -> set[str]:
  """The registry from a worker thread; a concurrent change makes it retry."""
  from app.runner_registry import registry

  for _ in range(5):
    try:
      return registry.all_alive_chat_ids()
    except RuntimeError:
      continue
  # Nothing has been deleted yet; failing is safer than guessing.
  raise RuntimeError("Could not read which chats are running.")


def _still_in_use(chat_id: str) -> bool:
  """A late re-check from a worker thread; an unreadable registry counts as in use."""
  from app.runner_registry import registry

  try:
    return registry.is_alive(chat_id)
  except RuntimeError:
    return True


async def _preview_work_files(_data_dir: str) -> dict:
  from app.agent_scratch import START_RACE_GRACE_SECONDS, idle_scratch_usage

  usage = await asyncio.to_thread(
    idle_scratch_usage, idle_seconds=START_RACE_GRACE_SECONDS,
    alive_chat_ids=_alive_chat_ids(),
  )
  return {"bytes": usage["bytes"], "items": usage["chats"], "kept": usage["kept_chats"]}


async def _clear_work_files(_data_dir: str) -> dict:
  # The disk-pressure sweep: every chat with no turn in flight and no change in
  # the last few minutes, the same rule admission applies when storage is short.
  from app.agent_scratch import START_RACE_GRACE_SECONDS, sweep_idle_scratch

  result = await sweep_idle_scratch(idle_seconds=START_RACE_GRACE_SECONDS)
  return {"bytes": result["bytes"], "items": result["removed"]}


def _sessions_in_use(alive_chat_ids: set[str]) -> frozenset[str]:
  """Provider session ids of chats that are running or starting.

  Such a chat may resume a session older than the cutoff at any moment, so
  its session is never cleared from under it.
  """
  from app import models
  from app.database import SessionLocal
  from app.run_state import running_chat_ids

  with SessionLocal() as db:
    chat_ids = alive_chat_ids | running_chat_ids(db)
    if not chat_ids:
      return frozenset()
    linked = db.query(models.ChatSessionLink.session_id).filter(
      models.ChatSessionLink.chat_id.in_(chat_ids),
    ).all()
    latest = db.query(models.Chat.session_id).filter(
      models.Chat.id.in_(chat_ids), models.Chat.session_id.isnot(None),
    ).all()
  return frozenset(row[0] for row in [*linked, *latest])


async def _preview_agent_sessions(data_dir: str) -> dict:
  from app.provider_session_retention import stale_session_usage

  usage = await asyncio.to_thread(
    stale_session_usage, data_dir, max_age_days=SESSION_KEEP_DAYS,
    in_use=_sessions_in_use(_alive_chat_ids()),
  )
  return {"bytes": usage["bytes"], "items": usage["sessions"]}


async def _clear_agent_sessions(data_dir: str) -> dict:
  from app.provider_session_retention import clear_stale_sessions

  result = await asyncio.to_thread(
    clear_stale_sessions, data_dir, max_age_days=SESSION_KEEP_DAYS,
    in_use=lambda: _sessions_in_use(_alive_chat_ids_from_thread()),
  )
  outcome = {"bytes": result["bytes"], "items": result["sessions"]}
  if result["codex_skipped"]:
    outcome["note"] = "Codex is busy, so its old copies stay until it is idle."
  return outcome


def _browser_inputs() -> tuple[dict, set[str]]:
  from app.browser_profiles import chat_activity_snapshot
  from app.database import SessionLocal

  with SessionLocal() as db:
    chats = chat_activity_snapshot(db)
  return chats, _alive_chat_ids()


async def _preview_agent_browsers(data_dir: str) -> dict:
  from app.browser_profiles import closed_chat_profile_usage

  chats, alive = _browser_inputs()
  usage = await asyncio.to_thread(closed_chat_profile_usage, data_dir, chats, alive)
  return {"bytes": usage["bytes"], "items": usage["profiles"]}


async def _clear_agent_browsers(data_dir: str) -> dict:
  from app.browser_profiles import clear_closed_chat_profiles

  chats, alive = _browser_inputs()
  result = await asyncio.to_thread(
    clear_closed_chat_profiles, data_dir, chats, alive, still_in_use=_still_in_use,
  )
  return {"bytes": result["bytes"], "items": result["profiles"]}


CLEANUP_TARGETS: dict[str, CleanupTarget] = {
  "work_files": CleanupTarget(("agent_scratch",), _preview_work_files, _clear_work_files),
  "agent_sessions": CleanupTarget(
    ("provider_telemetry_claude", "provider_telemetry_codex"),
    _preview_agent_sessions, _clear_agent_sessions,
  ),
  "agent_browsers": CleanupTarget(
    ("browser_profiles",), _preview_agent_browsers, _clear_agent_browsers,
  ),
}

_CLEANUP_LOCK = asyncio.Lock()


async def preview_cleanup(target_id: str, data_dir: str | Path) -> dict:
  target = CLEANUP_TARGETS[target_id]
  return {"target": target_id, **await target.preview(str(data_dir))}


async def run_cleanup(target_id: str, data_dir: str | Path) -> dict:
  """Run one owner cleanup, then re-measure the domains it shrank.

  One at a time: two overlapping cleanups would race over the same folders and
  report each other's bytes.
  """
  target = CLEANUP_TARGETS[target_id]
  if _CLEANUP_LOCK.locked():
    raise CleanupBusy(target_id)
  async with _CLEANUP_LOCK:
    outcome = await target.clear(str(data_dir))
    try:
      await asyncio.to_thread(remeasure_domains, data_dir, list(target.domains))
    except Exception:
      # The files are already gone; report the clear even if sizing failed.
      log.warning("re-measure after %s cleanup failed", target_id, exc_info=True)
  return {"target": target_id, **outcome}
