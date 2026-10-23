"""Public review admission, dispatch, and its immutable evidence receipt."""
import asyncio
import uuid
from fastapi import HTTPException
from app import chat_queue, settled, contribution_review_runs as reviews
from app import contribution_review_public as public
from app.github_connection import _github_connection_transaction


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
    frozen = public.evidence(outcome)
    async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
      authorize()
      current = (row.outcomes_json or {}).get(item_key, {})
      if public.require_same_evidence(current, outcome):
        return current
      if current.get("state") == "all_clear" and public.evidence(current) != frozen:
        raise HTTPException(409, "Retry the public review with its recorded evidence unchanged.")
      reviews.save_outcome(db, row, item_key, {**outcome,
        "public_review": {"state": "posting", "evidence": frozen, "marker": marker}})
    # There is no await between the final authority check and settled dispatch.
    try:
      authorize()
    except HTTPException as exc:
      return {**outcome, "public_review": {"state": "not_dispatched", "marker": marker, "summary": str(exc.detail)}}
    result = await settled.run_settled(public.post_review, gh, cwd, target, body)
    receipt = {"marker": marker, "state": {"confirmed": "posted", "unknown": "unknown",
                         "not_dispatched": "not_dispatched"}[result.state]}
    if result.state != "not_dispatched":
      receipt["evidence"] = frozen
    if result.receipt:
      receipt.update(result.receipt)
    if result.reason:
      receipt["summary"] = result.reason
    return {**outcome, "public_review": receipt}

