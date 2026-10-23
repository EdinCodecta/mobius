"""Reviewer regressions for repair receipts, checkout ownership and admission."""
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import agent_work_claims, contribution_review_repair_actions as actions
from app import contribution_review_repairs as repairs, contribution_review_runs as domain, models
from app import chat_queue
from app.chat_writer import create_chat
from app.contribution_review_effects import DispatchResult
from app.deps import Principal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, ITEM
from tests.test_contribution_review_workflows import (BASE, NEW, SHA, checkout_body,
  configure_repair, publish_body, takeover)


@pytest.mark.parametrize("result_state,expected_release", [("not_dispatched", True), ("unknown", False)])
def test_push_result_releases_only_definite_non_effect(setup, monkeypatch, result_state, expected_release):
  db, row, principal = configure_repair(setup, monkeypatch)
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw:
    DispatchResult(result_state, reason="Exact refusal or uncertainty"))
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  claim = db.query(models.AgentWorkClaim).one()
  assert (claim.released_at is not None) is expected_release
  attempt = row.outcomes_json[domain.key(ITEM)].get("repair_attempts", [])
  assert (not attempt) if expected_release else attempt[-1]["state"] == "push_unknown"
  db.add(create_chat(id="fresh-repair-chat", title="Fresh approval"))
  db.add(models.ChatRun(id="fresh-repair-run", chat_id="fresh-repair-chat", status="running"))
  db.flush()
  peer = models.ContributionReviewRun(id="fresh-repair-grant", app_id=row.app_id,
    owner_id=row.owner_id, request_id="fresh-repair-request", mode=row.mode,
    targets_json=row.targets_json, outcomes_json={}, chat_id="fresh-repair-chat",
    github_actor_id=row.github_actor_id, app_nonce=row.app_nonce, options_json=row.options_json)
  db.add(peer)
  db.commit()
  fresh = Principal(owner=principal.owner, app_id=None, chat_id="fresh-repair-chat", run_id="fresh-repair-run")
  target = peer.targets_json[0]
  reservation = domain.arm_repair(db, peer, target, {}, {"id": "fresh", "state": "pushing",
    "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}, fresh)
  if expected_release:
    assert reservation is None
    assert peer.outcomes_json[domain.key(target)]["repair_attempts"][-1]["id"] == "fresh"
  else:
    assert isinstance(reservation, str) and "owning" in reservation
    assert not peer.outcomes_json[domain.key(target)].get("repair_attempts")


def test_confirmed_publish_discards_checkout_after_durable_fact(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  discarded = []
  def discard(path):
    db.refresh(row)
    outcome = row.outcomes_json[domain.key(ITEM)]
    assert outcome["repair_attempts"][-1]["state"] == "pushed"
    assert outcome["successor"]["head_sha"] == NEW
    discarded.append(path)
  monkeypatch.setattr(repairs, "discard_checkout", discard)
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: DispatchResult.confirmed({"head_sha": NEW}))
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert discarded == ["/fake/server-repair"]


def test_missing_recorded_checkout_is_reprepared(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  old = {"checkout": "/fake/missing", "initial_head_sha": SHA, "allowed_files": ["owned.py"]}
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "repairing", "checkout": old})
  monkeypatch.setattr(repairs, "checkout_present", lambda path: False)
  monkeypatch.setattr(repairs, "fetch_target_base", lambda *a: pytest.fail("missing path reused"))
  calls = []
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: calls.append(1) or {**old, "checkout": "/fake/new"})
  result = asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  assert calls == [1] and result["checkout"]["checkout"] == "/fake/new"


def test_gc_protects_active_and_uncertain_but_collects_stopped_and_orphans(setup, monkeypatch, tmp_path):
  db, row, _ = setup
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  paths = {name: tmp_path / f"repair-{name}" for name in ("active", "unknown", "terminal", "orphan")}
  for path in paths.values():
    path.mkdir()
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "repairing",
    "checkout": {"checkout": str(paths["active"])}, "repair_attempts": [{
      "state": "push_unknown", "checkout": str(paths["unknown"])}]})
  db.add(create_chat(id="completed-editor", title="Completed repair editor"))
  other = models.ContributionReviewRun(id="old-gc", app_id=row.app_id, owner_id=row.owner_id,
    request_id="old-gc", mode=row.mode, targets_json=row.targets_json,
    outcomes_json={domain.key(ITEM): {"state": "needs_you", "checkout": {"checkout": str(paths["terminal"])}}},
    chat_id="completed-editor", github_actor_id=row.github_actor_id, app_nonce=row.app_nonce)
  db.add(other)
  db.commit()
  monkeypatch.setattr(repairs, "_last_activity", lambda path: 0)
  repairs.sweep_stale_checkouts(**actions.checkout_gc_scope(db), now=10**12)
  assert paths["active"].exists() and paths["unknown"].exists()
  assert not paths["terminal"].exists() and not paths["orphan"].exists()


def test_stranded_pushing_observation_becomes_visible_unknown(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  outcome = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**outcome, "state": "pushing",
    "repair_attempts": [{"id": "armed", "state": "pushing", "from_sha": SHA,
      "head_sha": NEW, "base_sha": BASE, "checkout": "/fake/server-repair"}]})
  monkeypatch.setattr(actions, "reconcile_push", lambda *a, **kw: asyncio.sleep(0, result=False))
  asyncio.run(routes.observe_review(1, row.id, db, Principal(owner=principal.owner, app_id=None)))
  assert row.outcomes_json[domain.key(ITEM)]["repair_attempts"][-1]["state"] == "push_unknown"


def test_stop_runs_receipt_aware_sweep(setup, monkeypatch):
  db, row, principal = setup
  calls = []
  monkeypatch.setattr(repairs, "sweep_stale_checkouts", lambda **scope: calls.append(scope))
  asyncio.run(routes.stop_review(1, row.id, db, Principal(owner=principal.owner, app_id=None)))
  assert len(calls) == 1 and "protected_paths" in calls[0]


def test_stop_admission_lock_blocks_repair_arm(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pytest.fail("push after Stop"))
  async def race():
    async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
      pending = asyncio.create_task(routes.publish_repair(1, row.id, publish_body(), db, principal))
      await asyncio.sleep(0.05)
      assert not pending.done()
      domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
    try:
      await pending
    except HTTPException as exc:
      assert exc.status_code == 409
    else:
      pytest.fail("repair admitted after Stop")
  asyncio.run(race())
  assert not row.outcomes_json[domain.key(ITEM)].get("repair_attempts")


def test_successor_invariant_refusal_never_dispatches_and_releases(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  monkeypatch.setattr(domain, "confirmed_successor", lambda *a: (_ for _ in ()).throw(HTTPException(409, "bad history")))
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pytest.fail("public push"))
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert db.query(models.AgentWorkClaim).one().released_at is not None


def test_confirmed_remote_push_survives_later_projection_failure(setup, monkeypatch):
  db, row, _ = setup
  pending = {"id": "receipt", "state": "push_unknown", "from_sha": SHA,
    "head_sha": NEW, "base_sha": BASE, "checkout": "/fake/server-repair"}
  previous = {"state": "needs_you", "repair_attempts": [pending]}
  domain.save_outcome(db, row, domain.key(ITEM), previous)
  monkeypatch.setattr(domain, "confirmed_successor", lambda *a: (_ for _ in ()).throw(HTTPException(409, "history changed")))
  deleted = []
  monkeypatch.setattr(repairs, "discard_checkout", deleted.append)
  assert asyncio.run(actions.reconcile_push(db, row, ITEM, previous, pending, gh=None, cwd=None))
  actual = row.outcomes_json[domain.key(ITEM)]
  assert actual["repair_attempts"][-1]["state"] == "pushed"
  assert actual["state"] == "needs_you" and deleted == ["/fake/server-repair"]


def test_reservation_helper_enforces_fence_and_claim_cas(setup, monkeypatch):
  db, row, principal = setup
  called = []
  real_fence = domain.fence_public_transition
  def fence(*args):
    called.append(1)
    return real_fence(*args)
  monkeypatch.setattr(domain, "fence_public_transition", fence)
  domain.reserve_public_work(db, row, principal, "test:reservation", "Test", "Test")
  assert called == [1]
  # A stale claim revision cannot pass the exact CAS even when the row fence did.
  real_claim = agent_work_claims.claim_work
  monkeypatch.setattr(agent_work_claims, "claim_work", lambda *a, **kw: {
    **real_claim(*a, **kw), "revision": -1})
  with pytest.raises(HTTPException):
    domain.reserve_public_work(db, row, principal, "test:stale", "Test", "Test")


def test_canonical_diff_receives_repair_sanitized_environment(setup, monkeypatch):
  from app import app_git
  seen = []
  def canonical(*args, **kwargs):
    seen.append(kwargs.get("env"))
    raise RuntimeError("spy complete")
  monkeypatch.setattr(app_git, "_canonical_diff", canonical)
  monkeypatch.setattr(repairs, "_live_head", lambda *a: ({}, {}))
  monkeypatch.setattr(repairs, "_head_destination", lambda *a: ("fork/repo", 1, "topic"))
  monkeypatch.setattr(repairs, "owned_checkout", lambda *a: Path("/tmp/fake-checkout"))
  monkeypatch.setattr(repairs, "_changed_files", lambda *a: {"owned.py"})
  def git(_path, *args, **kwargs):
    text = {"status": "", "rev-parse": NEW, "diff": "owned.py\0"}.get(args[0], "")
    return type("Result", (), {"stdout": text, "returncode": 0})()
  monkeypatch.setattr(repairs, "_git", git)
  checkout = {"checkout": "/tmp/fake-checkout", "head_repo": "fork/repo", "head_repo_id": 1,
    "head_ref": "topic", "initial_head_sha": SHA, "allowed_files": ["owned.py"]}
  with pytest.raises(RuntimeError, match="spy complete"):
    repairs.validate_repair(None, Path("/tmp"), None,
      {"repo": "base/repo", "head_sha": SHA}, checkout)
  assert len(seen) == 1
  assert seen[0]["GIT_CONFIG_GLOBAL"] == "/dev/null"
  assert seen[0]["GIT_CONFIG_NOSYSTEM"] == "1"
  assert seen[0]["GIT_ASKPASS"] == "/bin/false"
