"""Legacy follows are classified without assigning a newer acquisition."""

from datetime import datetime, timedelta

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app import models, schema_migrations
from app.chat_writer import create_chat


def _seed_legacy_follow(eng, *, suffix, goal_status="open", held=False,
                        resolved=False, interest_age=1, claim_live=True):
  now = datetime(2026, 10, 12, 12)
  claim_time = now - timedelta(days=2)
  interest_time = now - timedelta(days=interest_age)
  with Session(eng) as db:
    db.add(create_chat(id=f"chat-{suffix}", messages=[]))
    db.add(models.ChatGoal(
      id=f"goal-{suffix}", chat_id=f"chat-{suffix}", objective="Follow work",
      status=goal_status, hold_json={"reason": "owner"} if held else None,
      created_at=claim_time,
    ))
    db.add(models.AgentWorkClaim(
      id=f"claim-{suffix}", owner_id=1, work_key=f"work-{suffix}",
      summary="Work", owner_chat_id=f"chat-{suffix}",
      owner_run_id=f"run-{suffix}", owner_goal_id=f"goal-{suffix}",
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
  _seed_legacy_follow(eng, suffix="dismissed", goal_status="dismissed")
  _seed_legacy_follow(eng, suffix="terminal", goal_status="completed")
  _seed_legacy_follow(eng, suffix="old", interest_age=3)
  _seed_legacy_follow(eng, suffix="released", claim_live=False)
  _seed_legacy_follow(eng, suffix="resolved", resolved=True, claim_live=False)
  with eng.begin() as conn:
    # Existing acquisitions had no identity. Retrying a partially completed
    # migration must retain whatever ids its first pass already assigned.
    conn.execute(text("UPDATE agent_work_claims SET acquisition_id=NULL"))

  schema_migrations._add_goal_dependencies(eng)
  with eng.connect() as conn:
    first = conn.execute(text(
      "SELECT waiting_goal_id, target_key, target_acquisition_id, "
      "result_disposition, received_run_id, outcome_json "
      "FROM goal_dependencies ORDER BY waiting_goal_id"
    )).all()
    claim_ids = dict(conn.execute(text(
      "SELECT id, acquisition_id FROM agent_work_claims"
    )).all())
  assert {row[0] for row in first} == {"goal-live", "goal-held", "goal-resolved"}
  for row in first[:2]:
    assert row[1] == f"claim:{row[2]}"
    assert row[3:] == (None, None, None)
  resolved = next(row for row in first if row[0] == "goal-resolved")
  assert resolved[1] == "legacy-interest:interest-resolved"
  assert resolved[2] is None
  assert resolved[3] == "legacy_consumed"
  assert resolved[4] is None
  assert "legacy_consumed" in resolved[5]

  schema_migrations._add_goal_dependencies(eng)
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT waiting_goal_id, target_key, target_acquisition_id, "
      "result_disposition, received_run_id, outcome_json "
      "FROM goal_dependencies ORDER BY waiting_goal_id"
    )).all() == first
    assert dict(conn.execute(text(
      "SELECT id, acquisition_id FROM agent_work_claims"
    )).all()) == claim_ids
    assert conn.execute(text("SELECT count(*) FROM agent_work_interests")).scalar_one() == 7
  assert "goal_dependencies" in inspect(eng).get_table_names()


def test_partial_dependency_migration_reuses_existing_acquisition(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
  models.Base.metadata.create_all(eng)
  _seed_legacy_follow(eng, suffix="partial")
  with eng.begin() as conn:
    conn.execute(text("UPDATE agent_work_claims SET acquisition_id='first-generation'"))
    conn.execute(text("DROP TABLE goal_dependencies"))
  schema_migrations._add_goal_dependencies(eng)
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT target_key FROM goal_dependencies"
    )).scalar_one() == "claim:first-generation"
