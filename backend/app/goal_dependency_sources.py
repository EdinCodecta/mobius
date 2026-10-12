"""Claim-acquisition source facts for Goal dependencies.

A claim's current row may be reacquired; resolved dependency outcomes are the
immutable record of the acquisition that a Goal actually followed.
"""
from __future__ import annotations

from uuid import uuid4
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.timeutil import now_naive_utc


def _eligible_goal(db: Session, chat_id: str, run_id: str):
  if db.query(models.Delegation.id).filter(
    models.Delegation.child_chat_id == chat_id,
  ).first():
    return None
  return db.query(models.ChatGoal).join(
    models.ChatRun, models.ChatRun.goal_id == models.ChatGoal.id,
  ).filter(
    models.ChatRun.id == run_id,
    models.ChatRun.chat_id == chat_id,
    models.ChatGoal.chat_id == chat_id,
    models.ChatGoal.status == 'open',
  ).first()


def _direct_cycle(db: Session, *, waiting_goal_id: str, source_goal_id: str | None) -> bool:
  if source_goal_id is None:
    return False
  if source_goal_id == waiting_goal_id:
    return True
  return db.query(models.GoalDependency.id).join(
    models.AgentWorkClaim,
    models.AgentWorkClaim.id == models.GoalDependency.target_claim_id,
  ).filter(
    models.GoalDependency.waiting_goal_id == source_goal_id,
    models.GoalDependency.target_kind == 'claim_acquisition',
    models.AgentWorkClaim.owner_goal_id == waiting_goal_id,
    models.AgentWorkClaim.acquisition_id == models.GoalDependency.target_acquisition_id,
    models.GoalDependency.withdrawn_at.is_(None),
    models.GoalDependency.result_disposition.is_(None),
    models.GoalDependency.resolved_at.is_(None),
  ).first() is not None


MAX_CLAIM_DEPENDENCIES_PER_GOAL = 64


def stage_follow_claim(db: Session, claim: models.AgentWorkClaim, *,
                       chat_id: str, run_id: str) -> str:
  """Register under the claim's write lock; return followed or a truthful reason.

  The CAS write serializes with settlement/transfer, including on SQLite where
  SELECT FOR UPDATE alone is ineffective. The caller commits both together.
  """
  goal = _eligible_goal(db, chat_id, run_id)
  if goal is None:
    return 'no_open_top_level_goal'
  if claim.owner_chat_id == chat_id or claim.owner_goal_id == goal.id:
    return 'self_dependency'
  if _direct_cycle(db, waiting_goal_id=goal.id,
                   source_goal_id=claim.owner_goal_id):
    return 'direct_cycle'
  if not claim.acquisition_id:
    return 'unidentified_acquisition'
  target_key = f'claim:{claim.acquisition_id}'
  existing = db.query(models.GoalDependency).filter(
    models.GoalDependency.waiting_goal_id == goal.id,
    models.GoalDependency.target_key == target_key,
  ).first()
  if existing is not None:
    if existing.withdrawn_at is not None:
      return 'withdrawn'
    if existing.result_disposition is not None:
      return existing.result_disposition
    return 'followed' if existing.resolved_at is None else 'result_owed'
  count = db.query(func.count(models.GoalDependency.id)).filter(
    models.GoalDependency.waiting_goal_id == goal.id,
    models.GoalDependency.target_kind == 'claim_acquisition',
    models.GoalDependency.withdrawn_at.is_(None),
    models.GoalDependency.result_disposition.is_(None),
  ).scalar() or 0
  if count >= MAX_CLAIM_DEPENDENCIES_PER_GOAL:
    return 'dependency_limit'
  revision = claim.revision
  changed = db.query(models.AgentWorkClaim).filter(
    models.AgentWorkClaim.id == claim.id,
    models.AgentWorkClaim.revision == revision,
    models.AgentWorkClaim.acquisition_id == claim.acquisition_id,
    models.AgentWorkClaim.released_at.is_(None),
    models.AgentWorkClaim.completed_at.is_(None),
  ).update({models.AgentWorkClaim.updated_at: now_naive_utc()}, synchronize_session=False)
  if changed != 1:
    raise ValueError('The claim changed while following; inspect it before retrying.')
  db.add(models.GoalDependency(
    id=str(uuid4()), waiting_goal_id=goal.id, waiting_chat_id=chat_id,
    created_by_run_id=run_id, target_kind='claim_acquisition',
    target_key=target_key, target_claim_id=claim.id,
    target_acquisition_id=claim.acquisition_id,
    target_chat_id=claim.owner_chat_id,
  ))
  db.flush()
  return 'followed'


def stage_stamp_claim_outcome(db: Session, row: models.AgentWorkClaim,
                              kind: str, outcome: str) -> int:
  """Stamp every live follower before the claim settlement transaction commits."""
  if kind not in ('completed', 'released', 'owner_deleted'):
    raise ValueError('Invalid claim outcome kind')
  now = now_naive_utc()
  payload = {
    'kind': kind, 'work_key': row.work_key, 'summary': row.summary,
    'outcome': outcome, 'source_chat_id': row.owner_chat_id,
    'source_chat_title': (db.query(models.Chat.title).filter(
      models.Chat.id == row.owner_chat_id).scalar() or row.owner_chat_id),
  }
  return db.query(models.GoalDependency).filter(
    models.GoalDependency.target_kind == 'claim_acquisition',
    models.GoalDependency.target_acquisition_id == row.acquisition_id,
    models.GoalDependency.resolved_at.is_(None),
    models.GoalDependency.withdrawn_at.is_(None),
    models.GoalDependency.result_disposition.is_(None),
  ).update({models.GoalDependency.outcome_json: payload,
            models.GoalDependency.resolved_at: now}, synchronize_session=False)


def dependency_source_blocker(db: Session, row: models.GoalDependency) -> str | None:
  """Report why a pending claim source cannot be trusted to progress.

  This is observational, not a polling or takeover operation. Resolved and
  withdrawn rows already have their own authoritative outcome/disposition.
  """
  if row.resolved_at is not None or row.withdrawn_at is not None or row.result_disposition is not None:
    return None
  if row.target_kind != 'claim_acquisition':
    return None
  claim = db.get(models.AgentWorkClaim, row.target_claim_id) if row.target_claim_id else None
  if claim is None:
    return 'source_missing'
  if claim.acquisition_id != row.target_acquisition_id:
    return 'source_missing'
  if claim.completed_at is not None or claim.released_at is not None:
    return 'source_failed'
  chat = db.get(models.Chat, claim.owner_chat_id) if claim.owner_chat_id else None
  if chat is None or chat.deleted_at is not None:
    return 'owner_deleted'
  if claim.owner_goal_id:
    goal = db.get(models.ChatGoal, claim.owner_goal_id)
    if goal is None or goal.status != 'open':
      return 'source_held' if goal is not None and goal.status == 'stopped' else 'source_failed'
    latest = db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == claim.owner_chat_id,
    ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
    if latest is not None and latest.goal_id == goal.id and latest.status in (
      'failed', 'stopped', 'interrupted',
    ):
      from app.chat_waits import _FIRED_UNDELIVERED, _goal_waits
      from app.delegations import _self_resuming_helper_rows
      if chat.pending_question_id is not None:
        return None
      if _goal_waits(db, chat.id, goal.id).filter(
        (models.ChatWait.status == 'armed') | _FIRED_UNDELIVERED,
      ).first():
        return None
      if any(
        helper.goal_id == goal.id for helper, _ in
        _self_resuming_helper_rows(db, {chat.id})
      ):
        return None
      if db.query(models.GoalDependency.id).filter(
        models.GoalDependency.waiting_goal_id == goal.id,
        models.GoalDependency.result_disposition.is_(None),
        models.GoalDependency.withdrawn_at.is_(None),
      ).first():
        return None
      return 'owner_not_progressing'
  else:
    run = db.get(models.ChatRun, claim.owner_run_id)
    if run is None:
      return 'owner_not_progressing'
    if run.status in ('failed', 'stopped', 'interrupted', 'completed'):
      if chat.pending_question_id is None:
        return 'owner_not_progressing'
  return None


def stage_reconcile_claim_sources(db: Session) -> int:
  """Resolve only conclusively nonviable claim sources in the caller's txn.

  A failed source Goal remains viable when a card, Wait, helper, or direct
  dependency still owns progress; otherwise its failure is surfaced once.
  This deliberately never polls or assumes an elapsed-time deadline.
  """
  rows = db.query(models.GoalDependency).filter(
    models.GoalDependency.target_kind == 'claim_acquisition',
    models.GoalDependency.resolved_at.is_(None),
    models.GoalDependency.withdrawn_at.is_(None),
    models.GoalDependency.result_disposition.is_(None),
  ).all()
  settled = 0
  for row in rows:
    blocker = dependency_source_blocker(db, row)
    if blocker is None:
      continue
    claim = db.get(models.AgentWorkClaim, row.target_claim_id) if row.target_claim_id else None
    if claim is not None:
      # Only a stalled candidate needs a source write lock. Re-read after the
      # lock so a simultaneous finish/transfer wins over stale observation.
      changed = db.query(models.AgentWorkClaim).filter(
        models.AgentWorkClaim.id == claim.id,
        models.AgentWorkClaim.revision == claim.revision,
      ).update({models.AgentWorkClaim.updated_at: now_naive_utc()},
               synchronize_session=False)
      if changed != 1:
        continue
      db.expire(claim)
      db.refresh(claim)
      blocker = dependency_source_blocker(db, row)
      if blocker is None:
        continue
    settled += db.query(models.GoalDependency).filter(
      models.GoalDependency.id == row.id,
      models.GoalDependency.resolved_at.is_(None),
      models.GoalDependency.withdrawn_at.is_(None),
      models.GoalDependency.result_disposition.is_(None),
    ).update({models.GoalDependency.outcome_json: {
        'kind': 'owner_not_progressing', 'reason': blocker,
        'target_acquisition_id': row.target_acquisition_id,
      }, models.GoalDependency.resolved_at: now_naive_utc()},
      synchronize_session=False)
  return settled
