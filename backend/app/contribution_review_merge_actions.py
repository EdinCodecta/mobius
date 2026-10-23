"""Exact merge reservation and dispatch, separate from review judgment."""
import asyncio

from fastapi import HTTPException

from app import chat_queue, contribution_review_runs as reviews, settled
from app.contribution_review_effects import DispatchResult, NotDispatched
from app.github_connection import _github_connection_transaction


async def dispatch_merge(gh, cwd, target, repo, checks, authorize):
  """Authority refusal precedes dispatch; a received remote fact survives Stop."""
  try:
    authorize()
  except HTTPException as exc:
    return DispatchResult.not_dispatched(str(exc.detail))
  try:
    if checks.get("isMergeQueueEnabled") is True:
      receipt = await settled.run_settled(reviews.enqueue, gh, cwd, target)
      return DispatchResult.confirmed({"state": "queued", **reviews.queue_receipt(receipt)})
    receipt = await settled.run_settled(reviews.perform_merge, gh, cwd, target, repo)
    if receipt.get("merged") is not True:
      raise HTTPException(409, "GitHub did not confirm a merge.")
    return DispatchResult.confirmed({"state": "merged", "merge_sha": receipt.get("sha")})
  except NotDispatched as exc:
    return DispatchResult.not_dispatched(str(exc))
  except Exception as exc:
    return DispatchResult.unknown(reviews.merge_failure_summary(exc))


async def _reserve_and_dispatch(db, row, target, outcome, repo, checks, principal, *, gh, cwd, authorize):
  async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
    authorize()
    try:
      owned = reviews.arm_merge(db, row, target, outcome, principal)
    except ValueError as exc:
      db.rollback()
      raise HTTPException(409, f"Merge claim conflict: {exc} No merge attempt was started; no new Goal follow was created.") from exc
    if owned:
      if owned.get("review_selection_id") != row.id:
        reviews.save_outcome(db, row, reviews.key(target), owned)
      else:
        db.refresh(row)
      return None
  db.refresh(row)
  armed = dict(row.outcomes_json[reviews.key(target)])
  result = await dispatch_merge(gh, cwd, target, repo, checks, authorize)
  if result.state == "not_dispatched":
    reviews.disarm_merge(db, row, target, armed, result.reason)
    return None
  if result.state == "confirmed":
    return {**armed, **result.receipt}
  return {**armed, "state": "merge_unknown", "summary": result.reason}


async def merge_reviewed_target(db, row, target, outcome, repo, pull, principal, *, gh, cwd, authorize):
  """Persist the current gate result or one typed public effect, never replay."""
  if pull.get("merged"):
    reviews.save_effect_outcome(db, row, reviews.key(target), {
      **outcome, "state": "merged", "merge_sha": pull.get("merge_commit_sha")})
    return
  checks = await asyncio.to_thread(reviews.pull_checks, gh, cwd, target)
  entry = reviews.queue_entry(checks, target)
  if entry:
    await asyncio.to_thread(reviews.assert_current_base, gh, cwd, target)
    authorize()
    reviews.save_outcome(db, row, reviews.key(target), {**outcome, "state": "queued", **reviews.queue_receipt(entry)})
    return
  blocker = reviews.merge_blocker(target, repo, pull, checks)
  if blocker:
    authorize()
    reviews.save_outcome(db, row, reviews.key(target), {**outcome, "state": "needs_you", "summary": blocker})
    return
  if not checks.get("isMergeQueueEnabled"):
    reviews.merge_method(repo)  # Known no-method refusal occurs before reservation.
  async with _github_connection_transaction():
    actor = await asyncio.to_thread(reviews.read, gh, cwd, "user")  # unsettled: read-only (GitHub actor identity)
    if str(actor.get("id") or "") != row.github_actor_id:
      authorize()
      reviews.save_outcome(db, row, reviews.key(target), {**outcome, "state": "needs_you",
        "summary": "The connected GitHub account changed. Approve a new review selection."})
      return
    await asyncio.to_thread(reviews.assert_current_base, gh, cwd, target)  # unsettled: read-only (GitHub target base)
    result = await _reserve_and_dispatch(db, row, target, outcome, repo, checks, principal,
      gh=gh, cwd=cwd, authorize=authorize)
    if result is not None:
      reviews.save_effect_outcome(db, row, reviews.key(target), result)
