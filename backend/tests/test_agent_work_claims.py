"""Workspace work ownership stays singular while explicit transfer remains possible."""
from app.chat_writer import create_chat

import pytest

from app.timeutil import now_naive_utc

from app import models
from app.agent_work_claims import (
  acknowledge_notice,
  claim_work,
  finish_work,
  stage_release_claims_for_chat,
)


def _fixture(db):
  owner = models.Owner(username="owner", hashed_password="not-used")
  first = create_chat(id="claim-first", title="Original integrator", messages=[])
  second = create_chat(id="claim-second", title="Broader author", messages=[])
  db.add_all([owner, first, second])
  db.flush()
  runs = [
    models.ChatRun(
      id="claim-run-first", root_run_id="claim-run-first",
      goal_id="claim-goal-first", goal_objective="Ship the change",
      chat_id=first.id, status="running", provider="codex",
    ),
    models.ChatRun(
      id="claim-run-second", root_run_id="claim-run-second",
      goal_id="claim-goal-second", goal_objective="Align the platform",
      chat_id=second.id, status="running", provider="claude",
    ),
  ]
  db.add_all(runs)
  db.add_all([
    models.ChatGoal(id="claim-goal-first", chat_id=first.id, objective="Ship the change"),
    models.ChatGoal(id="claim-goal-second", chat_id=second.id, objective="Align the platform"),
  ])
  db.commit()
  return owner, first, second


def test_first_claim_wins_and_loser_follows_without_duplicate_ownership(db):
  owner, first, second = _fixture(db)
  key = "github:mobius-os/mobius:pr:1079:3134e050:merge"

  won = claim_work(
    db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
    work_key=key, summary="Merge the reviewed PR",
  )
  lost = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Merge the same reviewed PR",
  )

  assert won["state"] == "claimed"
  assert lost["state"] == "held_by_peer"
  assert lost["owner_chat_id"] == first.id
  assert db.query(models.AgentWorkClaim).count() == 1
  interest = db.query(models.GoalDependency).one()
  assert (interest.waiting_chat_id, interest.waiting_goal_id) == (
    second.id, "claim-goal-second",
  )
  assert "does not transfer ownership of your whole Goal" in lost["next_action"]


def test_transfer_requires_current_owner_identity_and_records_reason(db):
  owner, first, second = _fixture(db)
  key = "github:mobius-os/mobius:pr:1079:3134e050:merge"
  claim_work(
    db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
    work_key=key, summary="Merge the reviewed PR",
  )

  try:
    claim_work(
      db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
      work_key=key, summary="Finish the broader authored integration",
      takeover_reason="I authored the current PR head and the first owner is blocked.",
      expected_owner_chat_id="stale-owner",
    )
  except ValueError as exc:
    assert "expected owner changed" in str(exc)
  else:  # pragma: no cover - protects the deliberate transfer boundary
    raise AssertionError("stale transfer unexpectedly succeeded")

  moved = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Finish the broader authored integration",
    takeover_reason="I authored the current PR head and the first owner is blocked.",
    expected_owner_chat_id=first.id,
  )
  assert moved["state"] == "transferred"
  assert moved["owner_chat_id"] == second.id
  assert moved["previous_owner_chat_id"] == first.id
  assert moved["takeover_reason"].startswith("I authored")
  assert moved["notification_pending"] is True

  # If delivery failed after the atomic transfer, the new owner can repeat the
  # same claim and recover the exact pending handoff instead of losing it.
  retried = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Finish the broader authored integration",
    takeover_reason="I authored the current PR head and the first owner is blocked.",
    expected_owner_chat_id=first.id,
  )
  assert retried["state"] == "transferred"
  assert retried["previous_owner_chat_id"] == first.id
  assert retried["notification_pending"] is True


def test_completion_resolves_followers_and_same_key_cannot_be_reclaimed(db):
  owner, first, second = _fixture(db)
  key = "github:mobius-os/mobius:pr:1079:3134e050:merge"
  claim_work(
    db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
    work_key=key, summary="Merge the reviewed PR",
  )
  claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Merge the same reviewed PR",
  )
  finished = finish_work(
    db, owner_id=owner.id, chat_id=first.id, work_key=key,
    outcome="Merged as 0b44dc9d", release=False,
  )

  assert finished.interested_chat_ids == []
  assert db.query(models.GoalDependency).one().outcome_json["kind"] == "completed"
  assert finished.claim["state"] == "completed"
  acknowledge_notice(
    db, claim_id=finished.claim["id"], revision=finished.claim["revision"],
    resolve_interests=True,
  )
  later = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Try the completed operation again",
  )
  assert later["state"] == "completed"
  assert later["owner_chat_id"] == first.id


def test_deleted_follower_cannot_suppress_live_follower_notification(db):
  owner, first, deleted = _fixture(db)
  live = create_chat(id="claim-live", title="Live follower", messages=[])
  live_run = models.ChatRun(
    id="claim-run-live", root_run_id="claim-run-live",
    goal_id="claim-goal-live", goal_objective="Follow exact work",
    chat_id=live.id, status="running", provider="codex",
  )
  db.add_all([live, live_run, models.ChatGoal(
    id="claim-goal-live", chat_id=live.id, objective="Follow exact work",
  )])
  db.commit()
  key = "platform:claim-fanout:test"
  claim_work(db, owner_id=owner.id, chat_id=first.id,
             run_id="claim-run-first", work_key=key, summary="Do exact work")
  for follower, run_id in ((deleted, "claim-run-second"), (live, live_run.id)):
    claim_work(db, owner_id=owner.id, chat_id=follower.id, run_id=run_id,
               work_key=key, summary="Follow exact work")
  deleted.deleted_at = now_naive_utc()
  db.commit()

  finished = finish_work(db, owner_id=owner.id, chat_id=first.id,
                         work_key=key, outcome="Done", release=False)

  assert finished.interested_chat_ids == []
  assert db.query(models.GoalDependency).filter_by(waiting_chat_id=live.id).one().resolved_at is not None


def test_deleting_owner_releases_only_unfinished_exact_action(db):
  owner, first, second = _fixture(db)
  key = "github:mobius-os/mobius:pr:1079:3134e050:merge"
  claim_work(
    db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
    work_key=key, summary="Merge the reviewed PR",
  )
  claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Merge the same reviewed PR",
  )

  released = stage_release_claims_for_chat(db, first.id)
  db.commit()

  assert len(released) == 1
  assert released[0].interested_chat_ids == []
  assert db.query(models.GoalDependency).one().outcome_json["kind"] == "owner_deleted"
  row = db.query(models.AgentWorkClaim).one()
  assert row.released_at is not None
  assert "deleted" in row.outcome
  reclaimed = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=key, summary="Take over the released action",
  )
  assert reclaimed["state"] == "claimed"
  assert reclaimed["owner_chat_id"] == second.id


# --- Claims settle with their owner's Goal -------------------------------

KEY = "github:mobius-os/mobius:pr:1079:3134e050:merge"


def _owned_goal_claim(db, *, task_status="completed"):
  """First chat's open Goal owns KEY; the second chat's Goal follows it."""
  owner, first, second = _fixture(db)
  goal = db.get(models.ChatGoal, "claim-goal-first")
  goal.plan_json = {"tasks": [{
    "id": "ship", "title": "Ship", "status": task_status, "depends_on": [],
  }]}
  goal.revision = 1
  db.commit()
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
             work_key=KEY, summary="Merge the reviewed PR")
  follower = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=KEY, summary="Merge the same reviewed PR",
  )
  assert follower["state"] == "held_by_peer"
  return owner, first, second


def _claim_row(db):
  db.expire_all()
  return db.query(models.AgentWorkClaim).filter_by(work_key=KEY).one()


@pytest.mark.parametrize("complete, expected", [
  (True, "Owning Goal completed."),
  ("Merged as 0b44dc9d; CI green", "Owning Goal completed: Merged as 0b44dc9d; CI green"),
])
def test_goal_completion_completes_the_claims_it_names(db, complete, expected):
  from app.agent_work_claims import pending_settlement_notices
  from app.goals import update_goal_record

  owner, first, second = _owned_goal_claim(db)
  run = db.get(models.ChatRun, "claim-run-first")
  goal = db.get(models.ChatGoal, "claim-goal-first")

  update_goal_record(db, run, goal, 1, complete=complete,
                     finished_claims=[KEY])

  row = _claim_row(db)
  assert row.completed_at is not None and row.released_at is None
  assert row.outcome == expected
  # The follower notice is owed durably until a seam delivers it.
  [pending] = pending_settlement_notices(db, first.id)
  assert (pending.state, pending.interested_chat_ids) == ("completed", [])
  assert db.query(models.GoalDependency).one().outcome_json["kind"] == "completed"
  again = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=KEY, summary="Try the finished merge again",
  )
  assert again["state"] == "completed"


@pytest.mark.parametrize("run_token", ["claim-run-first", ""])
def test_stop_releases_the_goal_claims_in_the_same_writer_commit(db, run_token):
  from app.chat_writer import FinishRun, PrepareChatStop, get_writer

  owner, first, second = _owned_goal_claim(db, task_status="running")
  get_writer().submit(PrepareChatStop(chat_id=first.id, actor="owner")).result(timeout=5)
  get_writer().submit(FinishRun(
    chat_id=first.id, run_token=run_token, terminal_status="stopped",
  )).result(timeout=5)

  db.expire_all()
  assert db.get(models.ChatGoal, "claim-goal-first").status == "stopped"
  row = _claim_row(db)
  assert row.released_at is not None and row.completed_at is None
  assert "stopped" in row.outcome
  taken = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=KEY, summary="Take over the released merge",
  )
  assert (taken["state"], taken["owner_chat_id"]) == ("claimed", second.id)


def test_dismissing_an_unfinished_goal_releases_its_claims(db):
  from app.chat_writer import ClearPresentedGoal, get_writer

  _owner, first, _second = _owned_goal_claim(db, task_status="running")
  receipt = get_writer().submit(ClearPresentedGoal(
    chat_id=first.id, expected_goal_id="claim-goal-first",
    preserve_execution=False,
  )).result(timeout=5)

  assert receipt["status"] == "cleared"
  row = _claim_row(db)
  assert row.released_at is not None
  assert "dismissed" in row.outcome


def test_restart_interruption_keeps_the_claim_with_its_open_goal(db):
  from app.agent_work_claims import stage_settle_claims_with_owner
  from app.chat_writer import FinishRun, get_writer

  _owner, first, _second = _owned_goal_claim(db, task_status="running")
  get_writer().submit(FinishRun(
    chat_id=first.id, run_token="claim-run-first",
    terminal_status="interrupted",
  )).result(timeout=5)

  assert stage_settle_claims_with_owner(db, first.id) == []
  row = _claim_row(db)
  assert row.completed_at is None and row.released_at is None
  assert row.owner_chat_id == first.id


def test_goal_settlement_never_overwrites_an_explicit_finish(db):
  from app.chat_writer import FinishRun, get_writer

  owner, first, _second = _owned_goal_claim(db, task_status="running")
  finish_work(db, owner_id=owner.id, chat_id=first.id, work_key=KEY,
              outcome="Merged as 0b44dc9d", release=False)
  get_writer().submit(FinishRun(
    chat_id=first.id, run_token="claim-run-first", terminal_status="stopped",
  )).result(timeout=5)

  row = _claim_row(db)
  assert row.completed_at is not None and row.released_at is None
  assert row.outcome == "Merged as 0b44dc9d"


def test_post_commit_seam_repairs_claims_of_a_goal_ended_elsewhere(db):
  from app.agent_work_claims import stage_settle_claims_with_owner

  _owner, first, second = _owned_goal_claim(db, task_status="running")
  # A Goal ended by a path that did not settle in-commit (e.g. an upgrade).
  db.get(models.ChatGoal, "claim-goal-first").status = "stopped"
  db.commit()

  [settled] = stage_settle_claims_with_owner(db, first.id)
  db.commit()
  assert (settled.state, settled.interested_chat_ids) == ("released", [])
  assert stage_settle_claims_with_owner(db, first.id) == []


def test_claim_taken_outside_a_goal_waits_for_explicit_finish(db):
  from app.agent_work_claims import stage_settle_claims_with_owner

  owner, first, _second = _fixture(db)
  db.add(models.ChatRun(
    id="claim-run-plain", root_run_id="claim-run-plain", chat_id=first.id,
    status="running", provider="codex",
  ))
  db.commit()
  claim_work(db, owner_id=owner.id, chat_id=first.id,
             run_id="claim-run-plain", work_key=KEY, summary="Merge it")

  assert stage_settle_claims_with_owner(db, first.id) == []
  assert _claim_row(db).completed_at is None


@pytest.mark.parametrize("status", ["completed", "stopped", "dismissed"])
def test_new_claim_after_goal_ends_is_not_released_as_old_work(db, status):
  from app.agent_work_claims import stage_settle_claims_with_owner

  owner, first, second = _fixture(db)
  db.get(models.ChatGoal, "claim-goal-first").status = status
  db.commit()
  won = claim_work(db, owner_id=owner.id, chat_id=first.id,
                   run_id="claim-run-first", work_key=KEY, summary="New approved action")
  assert won["owner_goal_id"] is None
  assert stage_settle_claims_with_owner(db, first.id) == []
  db.commit()
  lost = claim_work(db, owner_id=owner.id, chat_id=second.id,
                    run_id="claim-run-second", work_key=KEY, summary="Same new action")
  assert lost["state"] == "held_by_peer"
  assert _claim_row(db).released_at is None
  finished = finish_work(db, owner_id=owner.id, chat_id=first.id,
                         work_key=KEY, outcome="New action verified", release=False)
  assert finished.claim["state"] == "completed"


def test_racing_first_claims_leave_one_owner_and_one_follower(db):
  """Two chats insert the same key at once: the unique key picks one owner."""
  from sqlalchemy import event
  from sqlalchemy.orm import Session

  from app.database import SessionLocal

  owner, first, second = _fixture(db)
  raced = []

  def first_chat_wins_inside_the_gap(session, _ctx, _instances):
    pending = [
      row for row in session.new
      if isinstance(row, models.AgentWorkClaim) and row.owner_chat_id == second.id
    ]
    if not pending or raced:
      return
    raced.append(True)
    with SessionLocal() as other:
      won = claim_work(other, owner_id=owner.id, chat_id=first.id,
                       run_id="claim-run-first", work_key=KEY,
                       summary="Merge the reviewed PR")
      assert won["state"] == "claimed"

  event.listen(Session, "before_flush", first_chat_wins_inside_the_gap)
  try:
    lost = claim_work(db, owner_id=owner.id, chat_id=second.id,
                      run_id="claim-run-second", work_key=KEY,
                      summary="Merge the same reviewed PR")
  finally:
    event.remove(Session, "before_flush", first_chat_wins_inside_the_gap)

  assert raced == [True]
  assert (lost["state"], lost["owner_chat_id"]) == ("held_by_peer", first.id)
  assert db.query(models.AgentWorkClaim).count() == 1
  assert db.query(models.GoalDependency).one().waiting_chat_id == second.id


def test_goal_completion_releases_a_declined_action_it_did_not_name(db):
  """A declined approval's claim must not read as done: completed is terminal."""
  from app.agent_work_claims import pending_settlement_notices
  from app.goals import update_goal_record

  owner, first, second = _owned_goal_claim(db)
  update_goal_record(
    db, db.get(models.ChatRun, "claim-run-first"),
    db.get(models.ChatGoal, "claim-goal-first"), 1,
    complete="Owner declined the merge; the PR stays open for review.",
  )

  row = _claim_row(db)
  assert row.completed_at is None and row.released_at is not None
  assert row.outcome.startswith(
    "Owning Goal completed without recording this action as done: Owner declined"
  )
  [pending] = pending_settlement_notices(db, first.id)
  assert pending.state == "released"
  taken = claim_work(
    db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
    work_key=KEY, summary="Merge once the owner approves",
  )
  assert (taken["state"], taken["owner_chat_id"]) == ("claimed", second.id)


def test_goal_completion_refuses_to_name_a_claim_it_does_not_own(db):
  from app.goal_plans import GoalPlanError
  from app.goals import update_goal_record

  _owned_goal_claim(db)
  with pytest.raises(GoalPlanError, match="Not an open work claim") as refused:
    update_goal_record(
      db, db.get(models.ChatRun, "claim-run-first"),
      db.get(models.ChatGoal, "claim-goal-first"), 1,
      complete="Merged", finished_claims=[KEY + ":typo"],
    )
  # The refusal shows the key the Goal really holds, so the retry is verbatim.
  assert f"Open claims held here: {KEY}." in str(refused.value)
  db.expire_all()
  assert db.get(models.ChatGoal, "claim-goal-first").status == "open"
  row = _claim_row(db)
  assert row.completed_at is None and row.released_at is None


def test_finishing_a_retyped_key_names_the_exact_keys_this_chat_holds(db):
  """A shortened or suffix-less key is refused, never matched by guesswork.

  The refusal lists only the caller chat's open claims, so the next call can
  settle the real claim verbatim and another chat's claims stay unlisted.
  """
  owner, first, second = _fixture(db)
  held = "github:mobius-os/mobius:pr:1388:14aa63986fb205fb6c32ed91af0512a4:merge"
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
             work_key=held, summary="Merge the reviewed PR")
  claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second",
             work_key="github:mobius-os/mobius:pr:9:abc:update",
             summary="Another chat's action")

  with pytest.raises(ValueError, match="No work claim exists") as refused:
    finish_work(db, owner_id=owner.id, chat_id=first.id,
                work_key="github:mobius-os/mobius:pr:1388:14aa6398",
                outcome="Merged", release=False)
  message = str(refused.value)
  assert f"Open claims held here: {held}." in message
  assert "pr:9:abc" not in message

  db.rollback()
  finished = finish_work(db, owner_id=owner.id, chat_id=first.id,
                         work_key=held, outcome="Merged", release=False)
  assert finished.claim["state"] == "completed"

  with pytest.raises(ValueError, match="No open work claims are held here"):
    finish_work(db, owner_id=owner.id, chat_id=first.id,
                work_key="github:mobius-os/mobius:pr:1:x:merge",
                outcome="Merged", release=False)


def test_work_keys_accept_pr_references_with_hash_and_plus():
  import pytest

  from app.agent_work_claims import clean_work_key

  key = "pr-update:mobius-os/mobius#1365:5d0e+steer"
  assert clean_work_key(key) == key
  with pytest.raises(ValueError, match="# \\+"):
    clean_work_key("Has Spaces")


def test_follow_is_one_dependency_per_goal_and_acquisition(db):
  owner, first, second = _fixture(db)
  won = claim_work(db, owner_id=owner.id, chat_id=first.id,
                   run_id='claim-run-first', work_key=KEY, summary='Do work')
  first_follow = claim_work(db, owner_id=owner.id, chat_id=second.id,
                            run_id='claim-run-second', work_key=KEY, summary='Follow')
  second_follow = claim_work(db, owner_id=owner.id, chat_id=second.id,
                             run_id='claim-run-second', work_key=KEY, summary='Follow')
  assert first_follow['follow_status'] == second_follow['follow_status'] == 'followed'
  [dependency] = db.query(models.GoalDependency).all()
  assert dependency.target_key == f"claim:{won['acquisition_id']}"
  assert dependency.target_acquisition_id == won['acquisition_id']


def test_release_stamps_old_acquisition_before_reclaim(db):
  owner, first, second = _fixture(db)
  won = claim_work(db, owner_id=owner.id, chat_id=first.id,
                   run_id='claim-run-first', work_key=KEY, summary='Do work')
  claim_work(db, owner_id=owner.id, chat_id=second.id,
             run_id='claim-run-second', work_key=KEY, summary='Follow')
  finish_work(db, owner_id=owner.id, chat_id=first.id,
              work_key=KEY, outcome='Not done', release=True)
  [old] = db.query(models.GoalDependency).all()
  assert old.resolved_at is not None
  assert old.outcome_json['kind'] == 'released'
  reclaimed = claim_work(db, owner_id=owner.id, chat_id=second.id,
                         run_id='claim-run-second', work_key=KEY, summary='Retry')
  assert reclaimed['acquisition_id'] != won['acquisition_id']
  assert old.target_acquisition_id == won['acquisition_id']
  assert old.outcome_json['outcome'] == 'Not done'


def test_transfer_keeps_acquisition_and_withdraws_new_owner_self_follow(db):
  owner, first, second = _fixture(db)
  won = claim_work(db, owner_id=owner.id, chat_id=first.id,
                   run_id='claim-run-first', work_key=KEY, summary='Do work')
  claim_work(db, owner_id=owner.id, chat_id=second.id,
             run_id='claim-run-second', work_key=KEY, summary='Follow')
  moved = claim_work(db, owner_id=owner.id, chat_id=second.id,
                     run_id='claim-run-second', work_key=KEY, summary='Take over',
                     takeover_reason='Former owner asked me to take over.',
                     expected_owner_chat_id=first.id)
  assert moved['acquisition_id'] == won['acquisition_id']
  deps = db.query(models.GoalDependency).all()
  assert len(deps) == 2
  assert next(d for d in deps if d.waiting_chat_id == second.id).withdrawn_at
  assert next(d for d in deps if d.waiting_chat_id == first.id).withdrawn_at is None


@pytest.mark.parametrize("ended_status", ["failed", "completed", "parked_notified"])
def test_ended_source_without_handoff_resolves_owner_not_progressing(db, ended_status):
  from app.goal_dependency_sources import stage_reconcile_claim_sources
  owner, first, second = _fixture(db)
  claim_work(db, owner_id=owner.id, chat_id=first.id,
             run_id='claim-run-first', work_key=KEY, summary='Do work')
  claim_work(db, owner_id=owner.id, chat_id=second.id,
             run_id='claim-run-second', work_key=KEY, summary='Follow')
  db.get(models.ChatRun, 'claim-run-first').status = ended_status
  db.commit()
  assert stage_reconcile_claim_sources(db) == 1
  db.commit()
  assert db.query(models.GoalDependency).one().outcome_json['kind'] == 'owner_not_progressing'


def test_failed_source_with_saved_card_remains_pending(db):
  from app.goal_dependency_sources import stage_reconcile_claim_sources
  owner, first, second = _fixture(db)
  claim_work(db, owner_id=owner.id, chat_id=first.id,
             run_id='claim-run-first', work_key=KEY, summary='Do work')
  claim_work(db, owner_id=owner.id, chat_id=second.id,
             run_id='claim-run-second', work_key=KEY, summary='Follow')
  db.get(models.ChatRun, 'claim-run-first').status = 'failed'
  first.pending_question_id = 'source-card'
  db.commit()
  assert stage_reconcile_claim_sources(db) == 0
  assert db.query(models.GoalDependency).one().resolved_at is None


def test_direct_cycle_is_not_registered(db):
  owner, first, second = _fixture(db)
  other_key = 'platform:other-exact-action'
  claim_work(db, owner_id=owner.id, chat_id=first.id,
             run_id='claim-run-first', work_key=KEY, summary='First action')
  claim_work(db, owner_id=owner.id, chat_id=second.id,
             run_id='claim-run-second', work_key=other_key, summary='Second action')
  followed = claim_work(db, owner_id=owner.id, chat_id=second.id,
                        run_id='claim-run-second', work_key=KEY, summary='Follow first')
  blocked = claim_work(db, owner_id=owner.id, chat_id=first.id,
                       run_id='claim-run-first', work_key=other_key, summary='Follow second')
  assert followed['follow_status'] == 'followed'
  assert blocked['follow_status'] == 'direct_cycle'
  assert db.query(models.GoalDependency).count() == 1


def test_goal_less_completed_source_does_not_promise_future_progress(db):
  from app.goal_dependency_sources import stage_reconcile_claim_sources
  owner, first, second = _fixture(db)
  source_run = db.get(models.ChatRun, 'claim-run-first')
  source_run.goal_id = None
  db.commit()
  claim_work(db, owner_id=owner.id, chat_id=first.id,
             run_id='claim-run-first', work_key=KEY, summary='Do work')
  claim_work(db, owner_id=owner.id, chat_id=second.id,
             run_id='claim-run-second', work_key=KEY, summary='Follow')
  source_run.status = 'completed'
  db.commit()
  assert stage_reconcile_claim_sources(db) == 1
  assert db.query(models.GoalDependency).one().outcome_json['kind'] == 'owner_not_progressing'


def _third_executor(db):
  third = create_chat(id="claim-third", title="Third executor", messages=[])
  db.add(third)
  db.add(models.ChatGoal(id="claim-goal-third", chat_id=third.id, objective="Third work"))
  db.add(models.ChatRun(id="claim-run-third", root_run_id="claim-run-third",
    chat_id=third.id, goal_id="claim-goal-third", status="running", provider="codex"))
  db.commit()
  return third


def test_two_transfers_preserve_acquisition_and_all_exact_followers(db):
  owner, first, second = _fixture(db)
  third = _third_executor(db)
  original = claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first",
    work_key=KEY, summary="Action")
  for old, new, run in ((first, second, "claim-run-second"), (second, third, "claim-run-third")):
    moved = claim_work(db, owner_id=owner.id, chat_id=new.id, run_id=run,
      work_key=KEY, summary="Action", expected_owner_chat_id=old.id,
      takeover_reason="Accepted ownership of this exact action")
    assert moved["acquisition_id"] == original["acquisition_id"]
    assert moved["previous_follow_status"] == "followed"
  finish_work(db, owner_id=owner.id, chat_id=third.id, work_key=KEY, outcome="Done", release=False)
  rows = db.query(models.GoalDependency).all()
  assert {row.waiting_goal_id for row in rows} == {"claim-goal-first", "claim-goal-second"}
  assert all(row.outcome_json["kind"] == "completed" and row.outcome_json["source_chat_id"] == third.id for row in rows)


def test_transfer_rejects_cycle_created_for_an_existing_follower(db):
  owner, first, second = _fixture(db)
  third = _third_executor(db)
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first", work_key=KEY, summary="First action")
  other = "platform:second-action"
  claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second", work_key=other, summary="Second action")
  # second -> first; third -> second. Moving first's action to third would
  # create second <-> third although the former owner is first.
  claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second", work_key=KEY, summary="Follow")
  claim_work(db, owner_id=owner.id, chat_id=third.id, run_id="claim-run-third", work_key=other, summary="Follow")
  with pytest.raises(ValueError, match="direct Goal dependency cycle"):
    claim_work(db, owner_id=owner.id, chat_id=third.id, run_id="claim-run-third", work_key=KEY,
      summary="Transfer", expected_owner_chat_id=first.id, takeover_reason="Accepted exact action from original executor")
  assert _claim_row(db).owner_chat_id == first.id


def test_transfer_quota_failure_rolls_back_instead_of_losing_former_goal_follow(db, monkeypatch):
  from app import goal_dependency_sources
  owner, first, second = _fixture(db)
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first", work_key=KEY, summary="Action")
  monkeypatch.setattr(goal_dependency_sources, "MAX_CLAIM_DEPENDENCIES_PER_GOAL", 0)
  with pytest.raises(ValueError, match="former Goal's follow: dependency_limit"):
    claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second", work_key=KEY,
      summary="Transfer", expected_owner_chat_id=first.id, takeover_reason="Accepted exact action from original executor")
  assert _claim_row(db).owner_chat_id == first.id
  assert db.query(models.GoalDependency).count() == 0


def test_transfer_follow_uses_claims_exact_former_goal_even_if_run_rebound(db):
  owner, first, second = _fixture(db)
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first", work_key=KEY, summary="Action")
  db.add(models.ChatGoal(id="successor-goal", chat_id=first.id, objective="Unrelated work"))
  db.get(models.ChatRun, "claim-run-first").goal_id = "successor-goal"
  db.commit()
  claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second", work_key=KEY,
    summary="Transfer", expected_owner_chat_id=first.id, takeover_reason="Accepted exact action from original executor")
  row = db.query(models.GoalDependency).one()
  assert row.waiting_goal_id == "claim-goal-first" and row.created_by_run_id is None


def test_nine_dependencies_do_not_consume_the_eight_monitor_quota(db):
  from app.chat_waits import declare_wait
  owner, first, second = _fixture(db)
  for index in range(8):
    declare_wait(db, chat_id=second.id, description=f"Timer {index}", kind="timer",
      delay_secs=60, created_by_run_id="claim-run-second")
  for index in range(9):
    key = f"platform:action:{index}"
    claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first", work_key=key, summary="Action")
    followed = claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second", work_key=key, summary="Follow")
    assert followed["follow_status"] == "followed"
  assert db.query(models.GoalDependency).count() == 9
  assert db.query(models.ChatWait).count() == 8


@pytest.mark.parametrize("goal_less", [False, True])
def test_source_failure_is_not_hidden_by_another_goal_in_its_chat(db, goal_less):
  from app.goal_dependency_sources import stage_reconcile_claim_sources
  owner, first, second = _fixture(db)
  if goal_less:
    db.get(models.ChatRun, "claim-run-first").goal_id = None
    db.commit()
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first", work_key=KEY, summary="Action")
  claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second", work_key=KEY, summary="Follow")
  db.get(models.ChatRun, "claim-run-first").status = "failed"
  db.add(models.ChatGoal(id="other-source-goal", chat_id=first.id, objective="Other"))
  db.add(models.ChatRun(id="other-source-run", chat_id=first.id, goal_id="other-source-goal", status="running"))
  first.pending_question_id = "other-goals-card"
  db.commit()
  assert stage_reconcile_claim_sources(db) == 1
  db.commit()
  assert db.query(models.GoalDependency).one().outcome_json["kind"] == "owner_not_progressing"


def test_longer_stalled_cycle_is_recovery_not_an_automatic_handoff(db):
  from app.goal_dependency_sources import dependency_source_blocker, stage_reconcile_claim_sources
  owner, first, second = _fixture(db)
  third = _third_executor(db)
  executors = [(first, "claim-run-first"), (second, "claim-run-second"), (third, "claim-run-third")]
  for index, (chat, run_id) in enumerate(executors):
    claim_work(db, owner_id=owner.id, chat_id=chat.id, run_id=run_id,
      work_key=f"platform:cycle:{index}", summary="Exact action")
  for index, (chat, run_id) in enumerate(executors):
    result = claim_work(db, owner_id=owner.id, chat_id=chat.id, run_id=run_id,
      work_key=f"platform:cycle:{(index+1)%3}", summary="Follow next action")
    assert result["follow_status"] == "followed"
    db.get(models.ChatRun, run_id).status = "failed"
  db.commit()
  rows = db.query(models.GoalDependency).all()
  assert all(dependency_source_blocker(db, row) == "owner_not_progressing" for row in rows)
  # Breaking the cycle with one factual result gives its waiting Goal a real
  # automatic next step; reconciliation does not invent takeover authority.
  assert stage_reconcile_claim_sources(db) >= 1
  db.commit()
  assert any(row.outcome_json and row.outcome_json["kind"] == "owner_not_progressing"
             for row in db.query(models.GoalDependency).all())


@pytest.mark.parametrize("ineligible", ["helper", "goalless", "held", "deleted"])
def test_ineligible_follower_and_approval_never_claim_a_follow(db, ineligible):
  from app.routes.owner_approvals import _approval_follower_result
  owner, first, second = _fixture(db)
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first", work_key=KEY, summary="Action")
  if ineligible == "helper":
    db.add(models.Delegation(id="helper-follow", parent_chat_id=first.id,
      parent_root_run_id="claim-run-first", child_chat_id=second.id, task_key="follow",
      provider="codex", scope="write", cwd="/data", prompt_sha256="digest"))
  elif ineligible == "deleted":
    second.deleted_at = now_naive_utc()
  elif ineligible == "goalless":
    db.get(models.ChatRun, "claim-run-second").goal_id = None
  else:
    db.get(models.ChatGoal, "claim-goal-second").status = "stopped"
  db.commit()
  result = claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second", work_key=KEY, summary="Follow")
  assert result["follow_status"] == "no_open_top_level_goal"
  assert db.query(models.GoalDependency).count() == 0
  approval = _approval_follower_result(result)
  assert "No Goal follow was created" in approval["next_action"]
  assert "will receive its result" not in approval["next_action"]


def test_stop_cannot_commit_without_atomic_dependency_outcome(db, monkeypatch):
  from app import agent_work_claims
  from app.chat_writer import PrepareChatStop, get_writer
  owner, first, second = _owned_goal_claim(db, task_status="running")
  def fail_stamp(*args, **kwargs):
    raise RuntimeError("Injected outcome storage failure")
  monkeypatch.setattr(agent_work_claims, "stage_stamp_claim_outcome", fail_stamp)
  with pytest.raises(RuntimeError, match="Injected outcome storage failure"):
    get_writer().submit(PrepareChatStop(chat_id=first.id, actor="owner")).result(timeout=5)
  db.expire_all()
  assert db.get(models.ChatGoal, "claim-goal-first").status == "open"
  assert _claim_row(db).released_at is None
  assert db.query(models.GoalDependency).one().outcome_json is None


@pytest.mark.parametrize("release", [False, True])
def test_settlement_winning_registration_gap_cannot_leave_a_pending_old_follow(db, monkeypatch, release):
  from app import agent_work_claims
  from app.database import SessionLocal
  owner, first, second = _fixture(db)
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first", work_key=KEY, summary="Action")
  original_follow = agent_work_claims._follow
  def settle_before_follow(session, claim, **kwargs):
    # A competing source transaction commits after the collision read but
    # before registration's write fence. Its immutable revision must win.
    with SessionLocal() as other:
      finish_work(other, owner_id=owner.id, chat_id=first.id, work_key=KEY,
        outcome="Concurrent source settlement", release=release)
    return original_follow(session, claim, **kwargs)
  monkeypatch.setattr(agent_work_claims, "_follow", settle_before_follow)
  with pytest.raises(ValueError, match="claim changed while following"):
    claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second", work_key=KEY, summary="Follow")
  db.rollback()
  assert db.query(models.GoalDependency).count() == 0
  row = _claim_row(db)
  assert (row.released_at if release else row.completed_at) is not None


@pytest.mark.parametrize("completed_before_delete", [False, True])
def test_source_hard_purge_preserves_the_exact_acquisitions_immutable_outcome(db, monkeypatch, completed_before_delete):
  from datetime import timedelta
  from app import chat_retention
  owner, first, second = _owned_goal_claim(db)
  if completed_before_delete:
    finish_work(db, owner_id=owner.id, chat_id=first.id, work_key=KEY,
      outcome="Completed before deletion", release=False)
  first.deleted_at = now_naive_utc() - timedelta(days=8)
  db.get(models.ChatRun, "claim-run-first").status = "stopped"
  db.commit()
  # The fixture must never touch a real chat's storage.
  monkeypatch.setattr(chat_retention, "_purge_chat_storage", lambda chat_id: None)
  source_id = first.id
  assert chat_retention.purge_expired_chat_tombstones(db) == [source_id]
  db.expire_all()
  row = db.query(models.GoalDependency).one()
  assert row.outcome_json["kind"] == ("completed" if completed_before_delete else "owner_deleted")
  assert row.outcome_json["source_chat_title"] == "Original integrator"
  assert row.result_disposition is None and row.received_run_id is None


def test_same_chat_goal_ownership_refresh_cannot_create_a_direct_cycle(db):
  owner, first, second = _fixture(db)
  db.add(models.ChatGoal(id="same-chat-new-goal", chat_id=first.id, objective="New work"))
  db.add(models.ChatRun(id="same-chat-new-run", chat_id=first.id, goal_id="same-chat-new-goal", status="running"))
  db.commit()
  other_key = "platform:other-refresh-action"
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first", work_key=KEY, summary="Original action")
  claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second", work_key=other_key, summary="Second action")
  claim_work(db, owner_id=owner.id, chat_id=second.id, run_id="claim-run-second", work_key=KEY, summary="Follow original")
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="same-chat-new-run", work_key=other_key, summary="Follow second")
  with pytest.raises(ValueError, match="refresh would create a direct Goal dependency cycle"):
    claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="same-chat-new-run", work_key=KEY, summary="Refresh under new Goal")
  assert _claim_row(db).owner_goal_id == "claim-goal-first"


def test_self_follow_registration_is_rejected_without_a_dependency(db):
  from app.goal_dependency_sources import stage_follow_claim
  owner, first, _ = _fixture(db)
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first", work_key=KEY, summary="Action")
  assert stage_follow_claim(db, _claim_row(db), chat_id=first.id,
    run_id="claim-run-first") == "self_dependency"
  assert db.query(models.GoalDependency).count() == 0


def test_two_goals_in_one_chat_register_independent_follows_of_one_acquisition(db):
  owner, first, second = _fixture(db)
  db.add(models.ChatGoal(id="second-follower-goal", chat_id=second.id, objective="Other dependent work"))
  db.add(models.ChatRun(id="second-follower-run", chat_id=second.id,
    goal_id="second-follower-goal", status="running"))
  db.commit()
  claim_work(db, owner_id=owner.id, chat_id=first.id, run_id="claim-run-first", work_key=KEY, summary="Action")
  for run_id in ["claim-run-second", "second-follower-run"]:
    followed = claim_work(db, owner_id=owner.id, chat_id=second.id, run_id=run_id, work_key=KEY, summary="Follow")
    assert followed["follow_status"] == "followed"
  rows = db.query(models.GoalDependency).all()
  assert {row.waiting_goal_id for row in rows} == {"claim-goal-second", "second-follower-goal"}
  assert len({row.target_acquisition_id for row in rows}) == 1
