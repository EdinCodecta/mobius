"""Public review admission, dispatch, and its immutable evidence receipt."""
import asyncio
import uuid
from fastapi import HTTPException
from app import chat_queue, settled, models, contribution_review_runs as reviews
from app.decision_cards import fence_owner
from app import contribution_review_public as public
from app.github_connection import _github_connection_transaction


def arm_public_review(db, row, item_key, target, outcome, marker, authorize):
  """Fence one exact public review per owner/PR/head across app and core grants."""
  fence_owner(db, row.owner_id)
  authorize()  # Refreshes the owning grant after the database writer fence.
  current = (row.outcomes_json or {}).get(item_key, {})
  try:
    already_armed = public.require_same_evidence(current, outcome)
  except HTTPException:
    db.rollback()
    raise
  if already_armed:
    db.rollback()
    return current
  if current.get("state") == "all_clear" and public.evidence(current) != public.evidence(outcome):
    db.rollback()
    raise HTTPException(409, "Retry the public review with its recorded evidence unchanged.")
  holder = db.query(models.ContributionReviewRun.id, models.ContributionReviewRun.chat_id).filter(
    models.ContributionReviewRun.owner_id == row.owner_id,
    models.ContributionReviewRun.outcomes_json[item_key]["head_sha"].as_string() == target["head_sha"],
    models.ContributionReviewRun.outcomes_json[item_key]["public_review"]["state"].as_string().in_(
      ("posting", "posted", "unknown")),
  ).first()
  if holder:
    db.rollback()
    raise HTTPException(409, f"Public review for this head is held by run {holder.id} in chat {holder.chat_id}. "
      "Open that run and observe its receipt; do not post another review.")
  reviews.save_outcome(db, row, item_key, {**outcome,
    "public_review": {"state": "posting", "marker": marker}})
  return None


async def post_public_review(db, row, item_key, target, outcome, principal, cwd, *, gh, authorize):
  """Freeze evidence before dispatch; only certified non-dispatch permits retry."""
  # Format before reserving: a known local rejection is not an uncertain send.
  marker = f"<!-- mobius-review:{uuid.uuid4().hex} -->"
  body = public.review_comment_body(outcome, marker=marker)
  async with _github_connection_transaction():
    actor = await asyncio.to_thread(reviews.read, gh, cwd, "user")  # unsettled: read-only (GitHub actor lookup)
    authorize()
    if str(actor.get("id") or "") != row.github_actor_id:
      return {**outcome, "public_review": {"state": "not_dispatched",
        "summary": "The connected GitHub account changed; no review was posted."}}
    async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
      existing = arm_public_review(db, row, item_key, target, outcome, marker, authorize)
      if existing is not None:
        return existing
    # There is no await between the final authority check and settled dispatch.
    try:
      authorize()
    except HTTPException as exc:
      return {**outcome, "public_review": {"state": "not_dispatched", "marker": marker, "summary": str(exc.detail)}}
    result = await settled.run_settled(public.post_review, gh, cwd, target, body)
    receipt = {"marker": marker, "state": {"confirmed": "posted", "unknown": "unknown",
                         "not_dispatched": "not_dispatched"}[result.state]}
    if result.receipt:
      receipt.update(result.receipt)
    if result.reason:
      receipt["summary"] = result.reason
    return {**outcome, "public_review": receipt}

