"""Legacy follows are classified without assigning a newer acquisition."""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app import models, schema_migrations
from app.chat_writer import create_chat


def _seed_legacy_follow(eng, *, suffix, goal_status="open", held=False,
                        resolved=False, interest_age=1, claim_live=True,
                        source_status="running", card=False):
  now = datetime(2026, 10, 12, 12)
  claim_time = now - timedelta(days=2)
  interest_time = now - timedelta(days=interest_age)
  with Session(eng) as db:
    db.add(create_chat(id=f"chat-{suffix}", messages=[]))
    source_chat = create_chat(id=f"source-{suffix}", messages=[])
    if card:
      source_chat.pending_question_id = f"question-{suffix}"
    db.add(source_chat)
    db.add(models.ChatGoal(
      id=f"goal-{suffix}", chat_id=f"chat-{suffix}", objective="Follow work",
      status=goal_status, hold_json={"reason": "owner"} if held else None,
      created_at=claim_time,
    ))
    db.add(models.ChatGoal(
      id=f"source-goal-{suffix}", chat_id=f"source-{suffix}",
      objective="Own work", status="open", created_at=claim_time,
    ))
    db.add(models.ChatRun(
      id=f"run-{suffix}", chat_id=f"source-{suffix}",
      goal_id=f"source-goal-{suffix}", status=source_status,
    ))
    db.add(models.AgentWorkClaim(
      id=f"claim-{suffix}", owner_id=1, work_key=f"work-{suffix}",
      summary="Work", owner_chat_id=f"source-{suffix}",
      owner_run_id=f"run-{suffix}", owner_goal_id=f"source-goal-{suffix}",
      claimed_at=claim_time,
      released_at=None if claim_live else now,
    ))
    db.add(models.AgentWorkInterest(
      id=f"interest-{suffix}", claim_id=f"claim-{suffix}",
      chat_id=f"chat-{suffix}", goal_id=f"goal-{suffix}",
      created_at=interest_time, resolved_at=now if resolved else None,
    ))
    db.commit()


def test_legacy_claim_follow_upgrade_is_conservative_and_repeatable(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'follows.db'}")
  models.Base.metadata.create_all(eng)
  _seed_legacy_follow(eng, suffix="live")
  _seed_legacy_follow(eng, suffix="held", goal_status="stopped", held=True)
  _seed_legacy_follow(eng, suffix="card", source_status="completed", card=True)
  _seed_legacy_follow(eng, suffix="alien-card", source_status="completed", card=True)
  _seed_legacy_follow(eng, suffix="stale-completed", source_status="completed")
  _seed_legacy_follow(eng, suffix="stale-failed", source_status="failed")
  _seed_legacy_follow(eng, suffix="dismissed", goal_status="dismissed")
  _seed_legacy_follow(eng, suffix="terminal", goal_status="completed")
  _seed_legacy_follow(eng, suffix="old", interest_age=3)
  _seed_legacy_follow(eng, suffix="released", claim_live=False)
  _seed_legacy_follow(eng, suffix="resolved", resolved=True, claim_live=False)
  _seed_legacy_follow(eng, suffix="source-deleted")
  _seed_legacy_follow(eng, suffix="source-stopped")
  _seed_legacy_follow(eng, suffix="source-missing")
  _seed_legacy_follow(eng, suffix="source-run-missing")
  _seed_legacy_follow(eng, suffix="helper")
  _seed_legacy_follow(eng, suffix="dependent-missing")
  _seed_legacy_follow(eng, suffix="self")
  _seed_legacy_follow(eng, suffix="bad-hold", goal_status="stopped")
  with eng.begin() as conn:
    # Existing acquisitions had no identity. Retrying a partially completed
    # migration must retain whatever ids its first pass already assigned.
    conn.execute(text("UPDATE agent_work_claims SET acquisition_id=NULL"))
    conn.execute(text("UPDATE chats SET deleted_at='2026-10-12 12:00:00' "
                      "WHERE id='source-source-deleted'"))
    conn.execute(text("UPDATE chat_goals SET status='stopped' "
                      "WHERE id='source-goal-source-stopped'"))
    conn.execute(text("DELETE FROM chats WHERE id='source-source-missing'"))
    conn.execute(text("DELETE FROM chat_runs WHERE id='run-source-run-missing'"))
    conn.execute(text("DELETE FROM chats WHERE id='chat-dependent-missing'"))
    conn.execute(text("UPDATE agent_work_claims SET owner_chat_id='chat-self', "
                      "owner_goal_id='goal-self' WHERE id='claim-self'"))
    conn.execute(text("UPDATE chat_goals SET hold_json='not-json' "
                      "WHERE id='goal-bad-hold'"))
    # A marker on the chat is not proof for the claim when its latest run
    # belongs to another Goal.
    conn.execute(text("UPDATE chat_runs SET goal_id='goal-alien-card' "
                      "WHERE id='run-alien-card'"))
  with Session(eng) as db:
    db.add(models.Delegation(id="delegation-helper", parent_chat_id="source-helper",
      parent_root_run_id="run-helper", task_key="task", child_chat_id="chat-helper",
      provider="codex", scope="write", cwd="/data", prompt_sha256="digest"))
    db.commit()


  schema_migrations._add_goal_dependencies(eng)
  schema_migrations._repair_legacy_goal_dependency_classification(eng)
  schema_migrations._narrow_legacy_dependency_wakes_and_add_owner_suppression(eng)
  with eng.connect() as conn:
    first = conn.execute(text(
      "SELECT waiting_goal_id, target_key, target_acquisition_id, "
      "result_disposition, received_run_id, outcome_json "
      "FROM goal_dependencies ORDER BY waiting_goal_id"
    )).all()
    claim_ids = dict(conn.execute(text(
      "SELECT id, acquisition_id FROM agent_work_claims"
    )).all())
  assert {row[0] for row in first} == {"goal-live", "goal-held", "goal-card", "goal-resolved"}
  for row in (row for row in first if row[0] != "goal-resolved"):
    assert row[1] == f"claim:{row[2]}"
    assert row[3:] == (None, None, None)
  with eng.connect() as conn:
    assert dict(conn.execute(text(
      "SELECT waiting_goal_id, target_chat_id FROM goal_dependencies "
      "WHERE resolved_at IS NULL"
    )).all()) == {
      "goal-live": "source-live", "goal-held": "source-held",
      "goal-card": "source-card",
    }
  resolved = next(row for row in first if row[0] == "goal-resolved")
  assert resolved[1] == "legacy-interest:interest-resolved"
  assert resolved[2] is None
  assert resolved[3] == "legacy_consumed"
  assert resolved[4] is None
  assert "legacy_consumed" in resolved[5]

  schema_migrations._add_goal_dependencies(eng)
  schema_migrations._repair_legacy_goal_dependency_classification(eng)
  schema_migrations._narrow_legacy_dependency_wakes_and_add_owner_suppression(eng)
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT waiting_goal_id, target_key, target_acquisition_id, "
      "result_disposition, received_run_id, outcome_json "
      "FROM goal_dependencies ORDER BY waiting_goal_id"
    )).all() == first
    assert dict(conn.execute(text(
      "SELECT id, acquisition_id FROM agent_work_claims"
    )).all()) == claim_ids
    assert conn.execute(text("SELECT count(*) FROM agent_work_interests")).scalar_one() == 19
  assert "goal_dependencies" in inspect(eng).get_table_names()


def test_partial_dependency_migration_reuses_existing_acquisition(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
  models.Base.metadata.create_all(eng)
  _seed_legacy_follow(eng, suffix="partial")
  with eng.begin() as conn:
    conn.execute(text("UPDATE agent_work_claims SET acquisition_id='first-generation'"))
    conn.execute(text("DROP TABLE goal_dependencies"))
  schema_migrations._add_goal_dependencies(eng)
  schema_migrations._repair_legacy_goal_dependency_classification(eng)
  schema_migrations._narrow_legacy_dependency_wakes_and_add_owner_suppression(eng)
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT target_key, target_chat_id FROM goal_dependencies"
    )).one() == ("claim:first-generation", "source-partial")


def test_legacy_direct_cycle_does_not_arm_either_side(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'cycle.db'}")
  models.Base.metadata.create_all(eng)
  _seed_legacy_follow(eng, suffix="a")
  _seed_legacy_follow(eng, suffix="b")
  with eng.begin() as conn:
    for suffix, other in (("a", "b"), ("b", "a")):
      conn.execute(text(
        "UPDATE chat_runs SET chat_id=:chat_id, goal_id=:goal_id WHERE id=:run_id"
      ), {"chat_id": f"chat-{other}", "goal_id": f"goal-{other}",
          "run_id": f"run-{suffix}"})
      conn.execute(text(
        "UPDATE agent_work_claims SET owner_chat_id=:chat_id, "
        "owner_goal_id=:goal_id WHERE id=:claim_id"
      ), {"chat_id": f"chat-{other}", "goal_id": f"goal-{other}",
          "claim_id": f"claim-{suffix}"})
  schema_migrations._add_goal_dependencies(eng)
  schema_migrations._repair_legacy_goal_dependency_classification(eng)
  schema_migrations._narrow_legacy_dependency_wakes_and_add_owner_suppression(eng)
  with eng.connect() as conn:
    assert conn.execute(text("SELECT count(*) FROM goal_dependencies")).scalar_one() == 0
    assert conn.execute(text("SELECT count(*) FROM agent_work_interests")).scalar_one() == 2


def test_upgrade_reconcile_and_pending_admission_never_wake_stale_interests(db):
  from app import chat, goal_dependencies, goal_dependency_sources

  # Use the writer's isolated test database, not just a patched read session:
  # reconciliation and queue admission must inspect this same upgraded state.
  eng = db.get_bind()
  for suffix, status, card in (
    ("running", "running", False),
    ("card", "completed", True),
    ("completed", "completed", False),
    ("failed", "failed", False),
  ):
    _seed_legacy_follow(eng, suffix=suffix, source_status=status, card=card)
  schema_migrations._add_goal_dependencies(eng)
  schema_migrations._repair_legacy_goal_dependency_classification(eng)
  schema_migrations._narrow_legacy_dependency_wakes_and_add_owner_suppression(eng)
  with Session(eng) as db:
    assert goal_dependency_sources.stage_reconcile_claim_sources(db) == 0
    assert {row.waiting_goal_id for row in goal_dependencies.outstanding_dependencies(db)} == {
      "goal-running", "goal-card",
    }
    assert chat._nonempty_pending_queues(db) == []
    db.commit()
  # Exercise the supervisor's real reconciliation/delivery selector before
  # the idle queue admission projection, not merely the migration row count.
  assert asyncio.run(goal_dependencies.deliver_pending_dependencies()) == 0
  with Session(eng) as db:
    assert chat._nonempty_pending_queues(db) == []
    assert db.query(models.AgentWorkInterest).count() == 4
    assert db.query(models.GoalDependency).filter(
      models.GoalDependency.resolved_at.is_not(None)).count() == 0


def test_0092_adds_missing_owner_suppression_field_idempotently(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'field.db'}")
  models.Base.metadata.create_all(eng)
  with eng.begin() as conn:
    conn.execute(text("ALTER TABLE goal_dependencies DROP COLUMN owner_suppressed_at"))
  schema_migrations._narrow_legacy_dependency_wakes_and_add_owner_suppression(eng)
  schema_migrations._narrow_legacy_dependency_wakes_and_add_owner_suppression(eng)
  assert "owner_suppressed_at" in {
    col["name"] for col in inspect(eng).get_columns("goal_dependencies")
  }
