"""App-less private review and truthful existing merge settlement."""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text

from app import contribution_review_runs as domain
from app import schema_migrations
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import ITEM, setup, report
from scripts import github_review as cli
from scripts.github_review_selection import review_path, start_body, verify_selection


def test_app_less_routes_do_not_expose_repair_or_posting():
  paths = {route.path for route in routes.router.routes}
  assert "/api/github/review-preview" in paths
  assert "/api/github/review-runs/{run_id}/outcomes" in paths
  assert not any("repair-checkout" in path or path.endswith("/repairs") for path in paths)
  assert routes.REVIEW_CAPABILITIES == {"post_review": True, "review_fix_merge": False}
  with pytest.raises(ValueError):
    routes.StartReviews(mode="review_fix_merge", request_id="request-123", items=[ITEM])


def test_cli_app_adapter_identity_and_body_use_canonical_helpers(monkeypatch):
  monkeypatch.setattr(cli, "inspect_pr", lambda ref: {**ITEM, "title": "Safe change", "url": "https://github.com/example/project/pull/7"})
  snapshot = {"review_prompt": "Review", "fix_prompt": "Fix", "merge_prompt": "Merge",
    "max_rounds": None, "autopilot": False, "provider": "codex", "model": "test-model",
    "reasoning_effort": "high"}
  monkeypatch.setattr(cli, "api", lambda path, **kw: {"options": snapshot, "preview_sha256": routes._fingerprint(snapshot)})
  core = cli.prepare_selection(["example/project#7"], "review", "chat-1")
  app = cli.prepare_selection(["example/project#7"], "review", "chat-1", 9)
  assert core["request_id"] != app["request_id"]
  assert review_path(None, "/review-runs") == "/api/github/review-runs"
  assert review_path(9, "/review-runs") == "/api/github/contributions/9/review-runs"
  frozen = cli.freeze_preview(app)
  verify_selection(frozen)
  assert "app_id" not in start_body(frozen)
  tampered = {**frozen, "app_id": 10}
  with pytest.raises(ValueError):
    verify_selection(tampered)


def test_core_admission_is_null_app_and_app_context_is_read_only(setup, monkeypatch):
  db, existing, principal = setup
  principal.scope = "owner"
  monkeypatch.setattr(domain, "inspect_target", lambda *a: existing.targets_json[0])
  body = routes.StartReviews(request_id="core-request-123", mode="review",
    source_chat_id=principal.chat_id, items=[ITEM],
    chat_approval={"context": "The owner requested this named private review."})
  created = asyncio.run(routes.core_start_reviews(body, db, principal))["run"]
  from app import models
  row = db.get(models.ContributionReviewRun, created["id"])
  assert row.app_id is None and row.app_nonce is None
  assert created["app_id"] is None
  assert any(item["id"] == row.id for item in routes.list_reviews(1, db, principal)["runs"])
  with pytest.raises(HTTPException) as denied:
    routes._row(db, 1, row.id, principal)
  assert denied.value.status_code == 404


def test_private_all_clear_survives_moved_base(setup, monkeypatch):
  db, row, _ = setup
  row.mode = "review"
  db.commit()
  monkeypatch.setattr(domain, "assert_current_base", lambda *a: pytest.fail("private history must not check live base"))
  assert report(setup)["run"]["items"][0]["state"] == "all_clear"
  assert asyncio.run(routes.observe_review(1, row.id, db, Principal(owner=row_owner(db, row), app_id=None)))["run"]["items"][0]["state"] == "all_clear"


def row_owner(db, row):
  from app import models
  return db.get(models.Owner, row.owner_id)


def test_proved_predispatch_stop_releases_exact_merge_claim(setup, monkeypatch):
  db, row, _ = setup
  calls = 0
  original = routes._parent

  def stop_after_arm(*args):
    nonlocal calls
    calls += 1
    if calls >= 3:
      raise HTTPException(409, "Stopped before public dispatch")
    return original(*args)

  monkeypatch.setattr(routes, "_parent", stop_after_arm)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("must not dispatch"))
  result = report(setup)["run"]["items"][0]
  assert result["state"] == "needs_you"
  assert result["merge_attempted"] is False
  from app import models
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.merge_work_key(ITEM)).one()
  assert claim.released_at is not None


def test_stop_after_dispatch_cannot_erase_confirmed_merge(setup, monkeypatch):
  db, row, _ = setup

  def merge_then_stop(*args):
    from app import models
    from app.database import SessionLocal
    other = SessionLocal()
    try:
      live = other.get(models.ContributionReviewRun, row.id)
      domain.save_outcome(other, live, routes.REVIEW_STOP_KEY, {"state": "requested"})
    finally:
      other.close()
    return {"merged": True, "sha": "confirmed-merge-sha"}

  monkeypatch.setattr(domain, "perform_merge", merge_then_stop)
  result = report(setup)["run"]["items"][0]
  assert result["state"] == "merged"
  assert result["merge_sha"] == "confirmed-merge-sha"
  db.refresh(row)
  assert row.outcomes_json[routes.REVIEW_STOP_KEY]["state"] == "requested"


def test_core_migration_adopts_prior_0079_variant_without_rewriting_it(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'old-review.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE schema_migrations (version VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMP)"))
    conn.execute(text("INSERT INTO schema_migrations VALUES ('0079_core_contribution_review_runs', CURRENT_TIMESTAMP)"))
    conn.execute(text("CREATE TABLE contribution_review_runs (id VARCHAR(64) PRIMARY KEY, app_id INTEGER NOT NULL, app_nonce VARCHAR(64) NOT NULL, request_id VARCHAR(64), owner_id INTEGER)"))
    conn.execute(text("INSERT INTO contribution_review_runs VALUES ('old-run', 2, 'old-nonce', 'old-request', 1)"))
  schema_migrations._allow_core_contribution_review_runs(eng)
  schema_migrations._allow_core_contribution_review_runs(eng)
  cols = {col["name"]: col for col in inspect(eng).get_columns("contribution_review_runs")}
  assert cols["app_id"]["nullable"] and cols["app_nonce"]["nullable"]
  assert "options_json" in cols
  with eng.connect() as conn:
    assert conn.execute(text("SELECT id, app_id, app_nonce FROM contribution_review_runs")).one() == ("old-run", 2, "old-nonce")
    assert conn.execute(text("SELECT version FROM schema_migrations")).scalar_one() == "0079_core_contribution_review_runs"
