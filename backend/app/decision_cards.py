"""Canonical safe dispositions of saved Q&A/approval cards.

Every source seam and subscriber uses the same database fence. Dispositions
and already-subscribed outcomes commit with the source change; delivery is
only latency. No transcript-position or process-local cancellation inference.
"""
from __future__ import annotations

from app import models
from app.timeutil import now_naive_utc


def fence_decisions(db):
  # The single-owner graph fence is shared with claim follows/transfers, so
  # mixed card/claim direct cycles cannot slip through concurrent registration.
  db.query(models.Owner).update(
    {models.Owner.username: models.Owner.username}, synchronize_session=False)


def supported_family(card):
  if card.get("secure_input") is not None or card.get("platform_action") is not None:
    return None
  return "approval" if card.get("action_key") else "question"


def saved_decision(db, chat_id, question_id):
  """Read the persisted disposition; absence is unknown, never cancelled."""
  return db.get(models.SavedDecisionCard, (chat_id, question_id), populate_existing=True)


def stage_saved_decision(db, chat, card, *, source_goal_id=None):
  """Register only allowlisted public card metadata, not arbitrary block data."""
  family = supported_family(card)
  identity = card.get("question_id")
  if family is None or not identity:
    return None
  fence_decisions(db)
  row = db.query(models.SavedDecisionCard).filter_by(
    chat_id=chat.id, question_id=identity).populate_existing().first()
  if row is None:
    row = models.SavedDecisionCard(chat_id=chat.id, question_id=identity,
      source_goal_id=source_goal_id, family=family,
      summary="; ".join(q.get("question", "") for q in card.get("questions", [])
                        if isinstance(q, dict)) or "Saved decision")
    db.add(row)
    db.flush()
  return row


def _answer_payload(card):
  # Q&A free text is ordinary shared chat data; sealed input never reaches
  # this reader. Copy only strings and option identities, never attachments,
  # execution metadata, raw tool output or arbitrary nested values.
  answers = {str(key): value for key, value in (card.get("answers") or {}).items()
             if isinstance(value, str)}
  selections = {str(key): [v for v in values if isinstance(v, str)]
                for key, values in (card.get("selected_options") or {}).items()
                if isinstance(values, list)}
  return {"answers": answers, "selected_options": selections}


def stage_decision_outcome(db, chat, question_id, kind, *, card=None):
  """Persist the source fact and all live dependents in the caller's txn.

  An explicit later answer to a stopped card remains legal in the existing
  card protocol. It updates the source record, but never rewrites outcomes
  already owed to subscribers of the earlier cancellation.
  """
  if kind not in {"answered", "closed_without_reply", "cancelled", "superseded", "source_deleted"}:
    raise ValueError("Invalid saved decision disposition")
  if not question_id:
    return None
  fence_decisions(db)
  from app.questions import saved_question
  row = saved_decision(db, chat.id, question_id)
  if row is None and card is None:
    card = saved_question(chat, question_id)
  if row is None and card is not None:
    from app.chat_writer import _question_owner_run
    owner = _question_owner_run(db, chat, question_id)
    row = stage_saved_decision(db, chat, card, source_goal_id=owner.goal_id if owner else None)
  if row is None:
    return None
  if row.outcome_json and (kind not in {"answered", "closed_without_reply"}
      or row.outcome_json.get("kind") in {"answered", "closed_without_reply"}):
    return row
  # Explicit answer evidence survives deletion even for a pre-upgrade card.
  if card and card.get("answers"):
    kind = "closed_without_reply" if card.get("answer_turn") == "none" else "answered"
  payload = {"kind": kind, "family": row.family, "summary": row.summary,
    "source_chat_id": chat.id, "source_chat_title": chat.title or "Untitled chat",
    "question_id": question_id}
  if kind in {"answered", "closed_without_reply"} and card is not None:
    payload.update(_answer_payload(card))
  _stamp_outcome(db, row, payload)
  return row


def _stamp_outcome(db, row, payload):
  row.outcome_json, row.resolved_at = payload, now_naive_utc()
  db.query(models.GoalDependency).filter(
    models.GoalDependency.target_kind == "owner_card",
    models.GoalDependency.target_chat_id == row.chat_id,
    models.GoalDependency.target_question_id == row.question_id,
    models.GoalDependency.resolved_at.is_(None),
    models.GoalDependency.result_disposition.is_(None),
    models.GoalDependency.withdrawn_at.is_(None),
  ).update({models.GoalDependency.outcome_json: payload,
            models.GoalDependency.resolved_at: row.resolved_at}, synchronize_session=False)


def stage_deleted_decision_rows(db, chat_id, title):
  """Retention uses only canonical metadata, never hydrates old transcripts."""
  fence_decisions(db)
  for row in db.query(models.SavedDecisionCard).filter_by(chat_id=chat_id).filter(
      models.SavedDecisionCard.resolved_at.is_(None)).all():
    _stamp_outcome(db, row, {"kind": "source_deleted", "family": row.family,
      "summary": row.summary, "source_chat_id": chat_id,
      "source_chat_title": title or "Untitled chat", "question_id": row.question_id})


def stage_delete_decisions(db, chat):
  """Deletion settles pending sources, preserving every already-saved answer."""
  fence_decisions(db)
  # Register the one legacy open source if it has not yet been followed.
  if chat.pending_question_id:
    stage_decision_outcome(db, chat, chat.pending_question_id, "source_deleted")
  stage_deleted_decision_rows(db, chat.id, chat.title)


def stage_wait_for_decision(db, *, chat_id, run_id, source_chat_id, question_id):
  """Subscribe atomically to one readable saved foreign decision, or refuse."""
  from uuid import uuid4
  from app.goal_dependency_sources import _eligible_goal, _direct_cycle
  from app.questions import saved_question
  from app.chat_writer import _question_owner_run
  fence_decisions(db)
  goal = _eligible_goal(db, chat_id, run_id)
  if goal is None:
    raise ValueError("Only an open top-level Goal can wait for a decision; helpers report upward.")
  if source_chat_id == chat_id:
    raise ValueError("Wait on another chat's card, not your own chat.")
  source = db.query(models.Chat).filter_by(id=source_chat_id).populate_existing().first()
  if source is None or source.deleted_at is not None:
    raise ValueError("The decision's chat is missing or deleted.")
  row = saved_decision(db, source.id, question_id)
  if row is None:
    card = saved_question(source, question_id)
    if card is None:
      raise ValueError("No saved decision with that question id exists in that chat.")
    if supported_family(card) is None:
      raise ValueError("Restart and sealed secure-input cards cannot be followed.")
    owner = _question_owner_run(db, source, question_id)
    # Legacy adoption uses only explicit saved answer/open identity evidence.
    # Missing pending markers never become guessed cancellation dispositions.
    if not card.get("answers") and source.pending_question_id != question_id:
      raise ValueError("This old card has no durable disposition and is not open.")
    row = stage_saved_decision(db, source, card, source_goal_id=owner.goal_id if owner else None)
    if card.get("answers"):
      row = stage_decision_outcome(db, source, question_id, "answered", card=card)
  if row.outcome_json is None and _direct_cycle(db, waiting_goal_id=goal.id,
      source_goal_id=row.source_goal_id):
    raise ValueError("This decision would create a self or direct Goal dependency cycle.")
  target_key = f"card:{source.id}:{question_id}"
  existing = db.query(models.GoalDependency).filter_by(
    waiting_goal_id=goal.id, target_key=target_key).populate_existing().first()
  if existing is not None:
    return existing
  count = db.query(models.GoalDependency).filter(
    models.GoalDependency.waiting_goal_id == goal.id,
    models.GoalDependency.result_disposition.is_(None),
    models.GoalDependency.withdrawn_at.is_(None),
  ).count()
  if count >= 64:
    raise ValueError("This Goal already has 64 outstanding dependencies.")
  dependency = models.GoalDependency(id=str(uuid4()), waiting_goal_id=goal.id,
    waiting_chat_id=chat_id, created_by_run_id=run_id,
    target_kind="owner_card", target_key=target_key,
    target_chat_id=source.id, target_question_id=question_id,
    outcome_json=row.outcome_json, resolved_at=row.resolved_at)
  db.add(dependency)
  db.flush()
  return dependency
