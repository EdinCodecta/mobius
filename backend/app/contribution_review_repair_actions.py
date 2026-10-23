"""Repair publication and observation while the route owns admission locks.

The route supplies one live-authority callback. This executor owns the persisted
attempt and exact non-dispatch release; confirmed facts survive revoked authority.
"""
import asyncio

from fastapi import HTTPException

from app import agent_work_claims, chat_queue, contribution_review_runs as reviews, settled
from app import contribution_review_repairs as repairs
from app.contribution_errors import ContributionSubmitError
from app.contribution_review_effects import DispatchResult, NotDispatched


async def dispatch_repair(row, target, validation, *, gh, cwd, authorize):
  try:
    authorize()
  except HTTPException as exc:
    return DispatchResult.not_dispatched(str(exc.detail))
  loop = asyncio.get_running_loop()
  async def guard():
    try:
      authorize()
      previous = dict((row.outcomes_json or {}).get(reviews.key(target), {}))
      await asyncio.to_thread(reviews.require_repairable_merge, gh, cwd, target, previous)  # unsettled: read-only (saved queue removal proof)
      authorize()
    except HTTPException as exc:
      raise NotDispatched(str(exc.detail)) from exc
  def before_push():
    asyncio.run_coroutine_threadsafe(guard(), loop).result()
  return await settled.run_settled(repairs.push_repair, gh, cwd, row, target, validation,
                                  before_push=before_push)


async def publish_attempt(db, row, target, validation, receipt, principal, *, gh, cwd, authorize):
  """Reserve, dispatch once, then preserve the exact public fact or uncertainty."""
  async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
    authorize()
    previous = row.outcomes_json.get(reviews.key(target), {})
    owned = reviews.arm_repair(db, row, target, previous, receipt, principal)
    if owned:
      return owned
  db.refresh(row)
  previous = row.outcomes_json[reviews.key(target)]
  attempts = list(previous["repair_attempts"])
  result = await dispatch_repair(row, target, validation, gh=gh, cwd=cwd, authorize=authorize)
  if result.state == "not_dispatched":
    reviews.save_effect_outcome(db, row, reviews.key(target), {**previous,
      "repair_attempts": attempts[:-1], "state": "needs_you",
      "summary": f"No push was attempted: {result.reason}"})
    agent_work_claims.finish_work(db, owner_id=row.owner_id, chat_id=row.chat_id,
      work_key=attempts[-1]["work_key"], outcome="Repair push was not attempted.", release=True)
  elif result.state == "unknown":
    attempts[-1] = {**attempts[-1], "state": "push_unknown"}
    reviews.save_effect_outcome(db, row, reviews.key(target), {**previous,
      "repair_attempts": attempts, "state": "needs_you", "summary": result.reason})
  else:
    attempts[-1] = {**attempts[-1], "state": "pushed"}
    reviews.save_effect_outcome(db, row, reviews.key(target),
      reviews.confirmed_successor(previous, attempts, validation))
    await settled.run_settled(repairs.discard_checkout, validation["checkout"])
  return None


async def reconcile_push(db, row, original, previous, pending, *, gh, cwd):
  """Confirm the existing push from remote identity; never send another push."""
  if pending["state"] == "pushed":
    return True
  candidate = {**original, "head_sha": pending["head_sha"], "base_sha": pending["base_sha"]}
  try:
    await asyncio.to_thread(reviews.current_pull, gh, cwd, candidate)
  except (HTTPException, ContributionSubmitError, OSError, ValueError):
    return False
  attempts = [{**r, "state": "pushed"} if r is pending else r
              for r in previous.get("repair_attempts", [])]
  reviews.save_effect_outcome(db, row, reviews.key(original), reviews.confirmed_successor(previous, attempts, pending))
  await settled.run_settled(repairs.discard_checkout, pending.get("checkout"))
  return True


async def abandon_checkouts(row):
  """Caller proved all editors stopped and holds the outcome lock.

  An ambiguous receipt still owns its checkout after Stop. Only unspent work
  is abandoned; age cannot establish that fact.
  """
  for outcome in (row.outcomes_json or {}).values():
    if any(a.get("state") in {"pushing", "push_unknown"}
           for a in outcome.get("repair_attempts", [])):
      continue
    checkout = outcome.get("checkout") or {}
    await settled.run_settled(repairs.discard_checkout, checkout.get("checkout"))
