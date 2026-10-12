"""Read-only handoff vocabulary; lifecycle owners retain admission authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from sqlalchemy.orm import Session
  from app import models


@dataclass(frozen=True)
class GoalCardHandoff:
  question_id: str
  kind: str  # saved or in_turn


@dataclass(frozen=True)
class GoalWaitHandoff:
  """An armed Wait or an outcome still owed to this exact Goal."""

  wait_id: str
  kind: str
  delivery_pending: bool
  resume_blocker: str | None


@dataclass(frozen=True)
class GoalHelperHandoff:
  delegation_id: str
  kind: str  # self_resuming


@dataclass(frozen=True)
class GoalParkHandoff:
  run_id: str
  kind: str
  reason: str | None


@dataclass(frozen=True)
class GoalHandoffs:
  """Native handoffs only; future cross-chat dependencies join at this seam.

  This is a read-side projection, not permission to admit or deliver a turn.
  The writer must still check its own queue, run and Goal authority.
  """

  card: GoalCardHandoff | None
  running: bool
  waits: tuple[GoalWaitHandoff, ...]
  helpers: tuple[GoalHelperHandoff, ...]
  park: GoalParkHandoff | None
  failed_attempt: bool

  @property
  def owner_input(self) -> bool:
    return self.card is not None

  @property
  def helper_count(self) -> int:
    return len(self.helpers)

  @property
  def park_presentation(self) -> dict[str, str | None]:
    return ({"kind": self.park.kind, "reason": self.park.reason} if self.park
            else {"kind": "none", "reason": None})

  @property
  def prevents_settlement(self) -> bool:
    """A native owner still owes this Goal a next step or result."""
    return (self.owner_input or bool(self.waits) or bool(self.helpers)
            or (self.park is not None and self.park.kind in {"automatic", "owner_input"}))


def goal_handoffs(db: Session, goal: models.ChatGoal) -> GoalHandoffs:
  """Read the native handoffs of one Goal, never another Goal in its chat.

  A NULL-owned historical helper is included only when its immutable root is
  the Goal id; a mutable physical run link is not evidence of ownership.
  """
  from app import models, questions
  from app.chat_waits import _goal_waits, _FIRED_UNDELIVERED, serialize_wait
  from app.delegations import _self_resuming_helper_rows
  from app.chat import continuation_handoff_for_chat

  latest = db.query(models.ChatRun.id, models.ChatRun.goal_id, models.ChatRun.status).filter(
    models.ChatRun.chat_id == goal.chat_id,
  ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
  owns_chat_attempt = latest is not None and latest.goal_id == goal.id
  pending = questions.get(goal.chat_id)
  pending_id = db.query(models.Chat.pending_question_id).filter(
    models.Chat.id == goal.chat_id,
  ).scalar()
  # Saved-card admission prevents a successor until this marker is answered
  # or cancelled; native in-turn questions carry their explicit run identity.
  card = None
  if owns_chat_attempt:
    if pending_id:
      card = GoalCardHandoff(question_id=pending_id, kind="saved")
    elif pending is not None and pending.run_token == latest.id:
      card = GoalCardHandoff(question_id=pending.question_id, kind="in_turn")
  wait_rows = _goal_waits(db, goal.chat_id, goal.id).filter(
    (models.ChatWait.status == "armed") | _FIRED_UNDELIVERED,
  ).all()
  waits = tuple(GoalWaitHandoff(
    wait_id=saved["id"], kind=saved["kind"],
    delivery_pending=saved["delivery_pending"],
    resume_blocker=saved["resume_blocker"],
  ) for saved in (serialize_wait(row, db=db) for row in wait_rows))
  helpers = tuple(
    GoalHelperHandoff(delegation_id=row.id, kind="self_resuming")
    for row, _status in _self_resuming_helper_rows(db, {goal.chat_id})
    if row.goal_id == goal.id or (
      row.goal_id is None and row.parent_root_run_id == goal.id
    )
  )
  park = None
  if owns_chat_attempt and latest.status in models.CONTINUATION_RUN_STATUSES:
    projected = continuation_handoff_for_chat(db, goal.chat_id)
    if projected["kind"] != "none":
      park = GoalParkHandoff(
        run_id=latest.id, kind=projected["kind"], reason=projected.get("reason"),
      )
  return GoalHandoffs(
    card=card,
    running=bool(owns_chat_attempt and latest.status == "running"),
    waits=waits,
    helpers=helpers,
    park=park,
    failed_attempt=bool(owns_chat_attempt and latest.status in {"failed", "interrupted"}),
  )

def project_handoff(*, owner_input: bool, running: bool, waits: list[dict],
                             helper_count: int, park: dict, goal: dict | None = None) -> dict:
  """One read-side handoff vocabulary for chat and exact-Goal surfaces."""
  if owner_input:
    return {"kind": "owner_input", "reason": "saved_card"}
  blockers = [wait.get("resume_blocker") for wait in waits if wait.get("delivery_pending")]
  if "owner_input" in blockers:
    return {"kind": "owner_input", "reason": "saved_card"}
  if running:
    return {"kind": "working", "reason": None}
  if park["kind"] in {"owner_input", "recovery"}:
    return park
  if any(blocker in {"manual_resume", "resume_failed", "restart"} for blocker in blockers):
    return {"kind": "recovery", "reason": next(
      blocker for blocker in blockers if blocker in {"manual_resume", "resume_failed", "restart"}
    )}
  if park["kind"] == "automatic" or helper_count > 0 or any(
    not wait.get("delivery_pending") or wait.get("resume_blocker") in {
      None, "platform_restart", "restoring_edits", "provider_park", "live_turn",
    }
    for wait in waits
  ):
    return {"kind": "automatic", "reason": park.get("reason")}
  if goal and goal.get("pause_reason") == "deferred":
    return {"kind": "on_hold", "reason": "deferred", "goal_id": goal["id"],
            "hold_reason": goal["hold_reason"]}
  return {"kind": "none", "reason": None}
