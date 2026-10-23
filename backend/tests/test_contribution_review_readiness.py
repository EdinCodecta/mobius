"""Draft readiness: explicit scope, shared admission and no replay."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import contribution_review_runs as domain, contribution_review_repairs as repairs, models
from app.contribution_review_effects import NotDispatched
from app.deps import Principal
from app.routes import contribution_reviews as routes
from app.chat_writer import create_chat
from tests.test_contribution_review_runs import setup, ITEM, TARGET, SHA, BASE, REPO, PULL, CHECKS, report
from tests.test_contribution_review_workflows import takeover, configure_repair, publish_body, checkout_body, overlapping_grant, NEW


def ready_body(**changes):
  return routes.DraftReady(**{**ITEM, "reviewed_base_sha": BASE,
    "independent_receipt_id": "independent-clear", "summary": "Exact full diff is clear",
    "scope": sorted(routes.SCOPE), "tests": "Hosted checks and focused tests passed",
    "tests_passed": True, **changes})


def ready_takeover(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  row.options_json = {**row.options_json,
    "confirmation_scope": "named_pr_repairs_ready_and_reviewed_successors"}
  db.commit()
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "reviewing",
    "independent_reviews": [{"id": "independent-clear", "head_sha": SHA,
      "base_sha": BASE, "state": "all_clear", "tests_passed": True}]})
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "draft": True}))
  return db, row, principal


def test_draft_readiness_requires_new_frozen_scope_and_preserves_legacy_grant(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("legacy grant widened"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 403
  assert not domain.draft_ready_allowed(row)


def test_new_scope_is_distinct_frozen_capability_and_repair_stays_available(setup, monkeypatch):
  db, row, _ = ready_takeover(setup, monkeypatch)
  assert domain.draft_ready_allowed(row)
  routes._repair_allowed(row)
  assert "POST /api/github/contributions/1/review-runs/batch/ready" in domain.takeover_brief(row)
  row.options_json = {**row.options_json,
    "confirmation_scope": "named_pr_repairs_and_reviewed_successors"}
  db.commit()
  assert not domain.draft_ready_allowed(row)
  assert "does not authorize marking a draft ready" in domain.takeover_brief(row)


def test_new_draft_scope_admits_open_draft_without_widening_old_request_id(setup, monkeypatch):
  db, _, principal = setup
  owner = Principal(owner=principal.owner, app_id=None)
  monkeypatch.setattr(routes, "resolve_round_choice", lambda *a: {"provider": "codex", "model": "gpt-5", "effort": "xhigh"})
  monkeypatch.setattr(domain, "inspect_target", lambda *a: {**TARGET, "head_repo": REPO["full_name"],
    "head_repo_id": 1, "head_ref": "topic"})
  monkeypatch.setattr(domain, "repair_file_scope", lambda *a: ["owned.py"])
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": True}))
  async def admit(**kwargs):
    db.add(models.ChatRun(id="draft-admitted", chat_id=kwargs["chat_id"], status="stopped"))
    db.commit()
    return True
  monkeypatch.setattr(routes, "start_programmatic_chat_turn", admit)
  preview = routes.review_preview(1, routes.ReviewPreview(), db, owner)
  body = routes.StartReviews(request_id="draft-grant-123", mode="review_fix_merge", items=[ITEM],
    confirmation_scope="named_pr_repairs_ready_and_reviewed_successors",
    preview_sha256=preview["preview_sha256"])
  result = asyncio.run(routes.start_reviews(1, body, db, owner))
  assert result["run"]["options"]["confirmation_scope"] == body.confirmation_scope
  assert "POST /api/github/contributions/1/review-runs/" in result["brief"]
  old = body.model_copy(update={"confirmation_scope": "named_pr_repairs_and_reviewed_successors"})
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.start_reviews(1, old, db, owner))
  assert error.value.status_code == 409


def test_mark_ready_uses_github_documented_graphql_mutation_and_validates_exact_head():
  calls = []
  def gh(_cwd, *args):
    calls.append(args)
    return SimpleNamespace(stdout='{"data":{"markPullRequestReadyForReview":{"pullRequest":{"id":"PR_7","isDraft":false,"headRefOid":"' + SHA + '"}}}}')
  assert domain.mark_ready(gh, "/tmp", TARGET)["isDraft"] is False
  assert "markPullRequestReadyForReview" in " ".join(calls[0])
  def changed(_cwd, *args):
    return SimpleNamespace(stdout='{"data":{"markPullRequestReadyForReview":{"pullRequest":{"id":"PR_7","isDraft":false,"headRefOid":"' + NEW + '"}}}}')
  with pytest.raises(HTTPException):
    domain.mark_ready(changed, "/tmp", TARGET)


def test_draft_readiness_is_receipted_once_and_retry_only_observes(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = []
  stage = {"ready": False}
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "draft": not stage["ready"]}))
  def mutate(*args):
    db.refresh(row)
    assert row.outcomes_json[domain.key(ITEM)]["ready_attempt"]["state"] == "attempting"
    calls.append(1)
    stage["ready"] = True
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "mark_ready", mutate)
  first = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert first["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "draft": False}))
  asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert calls == [1]


def test_draft_in_repository_without_checks_can_be_marked_ready(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS,
    "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}})
  stage = {"ready": False}
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "draft": not stage["ready"]}))
  def mutate(*args):
    stage["ready"] = True
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "mark_ready", mutate)
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"


def test_uncertain_draft_readiness_never_replays_and_observe_reconciles(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = []
  def lost(*args):
    calls.append(1)
    raise RuntimeError("lost response")
  monkeypatch.setattr(domain, "mark_ready", lost)
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "unknown"
  retry = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert retry["blocked"] and calls == [1]
  monkeypatch.setattr(domain, "current_pull", lambda *args: (REPO, {**PULL, "draft": False}))
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))
  assert observed["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  assert calls == [1]


def test_uncertain_readiness_blocks_repair_and_merge_until_observed(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: pytest.fail("repair after unknown ready"))
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("merge after unknown ready"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  assert error.value.status_code == 409
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert error.value.status_code == 409
  with pytest.raises(HTTPException) as error:
    report(setup, tests_passed=True, reviewed_base_sha=BASE,
      independent_receipt_id="independent-clear")
  assert error.value.status_code == 409
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": False}))
  assert asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]["items"][0]["ready_attempt"]["state"] == "ready"


def test_stopped_run_can_only_observe_uncertain_readiness_read_only(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  db.get(models.ChatRun, principal.run_id).status = "stopped"
  db.commit()
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": False}))
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("stopped run repeated mutation"))
  with pytest.raises(HTTPException):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  result = asyncio.run(routes.observe_review(1, row.id, db, principal))
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  assert db.get(models.ChatRun, principal.run_id).status == "stopped"


def test_draft_ready_then_exact_outcome_uses_existing_merge_gate(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  stage = {"ready": False}
  calls = []
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": not stage["ready"]}))
  def ready(*a):
    stage["ready"] = True
    calls.append("ready")
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  def merge(*a):
    calls.append("merge")
    return {"merged": True, "sha": "landed"}
  monkeypatch.setattr(domain, "mark_ready", ready)
  monkeypatch.setattr(domain, "perform_merge", merge)
  assert asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  result = report(setup, tests_passed=True, reviewed_base_sha=BASE,
    independent_receipt_id="independent-clear")
  assert result["run"]["items"][0]["state"] == "merged"
  assert calls == ["ready", "merge"]


def test_ready_response_with_changed_base_confirms_exact_readiness(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  stage = {"ready": False}
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": not stage["ready"]}))
  def ready(*a):
    stage["ready"] = True
    monkeypatch.setattr(domain, "assert_current_base", lambda *a: (_ for _ in ()).throw(HTTPException(409, "base drift")))
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "mark_ready", ready)
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"
  assert asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]["items"][0]["ready_attempt"]["state"] == "ready"


@pytest.mark.parametrize("attempt_state, item_state, run_state", [
  ("attempting", "marking_ready", "marking_ready"),
  ("unknown", "ready_unknown", "needs_you"),
])
def test_ready_attempt_projection_keeps_private_review_evidence(setup, monkeypatch,
                                                                  attempt_state, item_state, run_state):
  db, row, _ = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": attempt_state, "head_sha": SHA, "base_sha": BASE}})
  result = domain.view(row)
  assert result["state"] == run_state
  assert result["items"][0]["state"] == item_state
  assert result["items"][0]["independent_reviews"] == prior["independent_reviews"]
  assert row.outcomes_json[domain.key(ITEM)]["state"] == "reviewing"


@pytest.mark.parametrize("changed", ["actor", "model", "rights"])
def test_post_mutation_identity_or_rights_drift_is_unknown_not_replayed(setup, monkeypatch, changed):
  db, row, principal = ready_takeover(setup, monkeypatch)
  stage = {"ready": False}
  calls = []
  def pull(*a):
    repo = {**REPO, "permissions": {"push": False}} if changed == "rights" and stage["ready"] else REPO
    return repo, {**PULL, "draft": not stage["ready"]}
  def actor(*a):
    return {"id": 999 if changed == "actor" and stage["ready"] else 42}
  def mutate(*a):
    stage["ready"] = True
    calls.append(1)
    if changed == "model":
      db.get(models.Chat, row.chat_id).agent_settings_json = {"model": "another-model", "effort": "xhigh"}
      db.commit()
    return {"id": "PR_7", "isDraft": False, "headRefOid": SHA}
  monkeypatch.setattr(domain, "current_pull", pull)
  monkeypatch.setattr(domain, "read", actor)
  monkeypatch.setattr(domain, "mark_ready", mutate)
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  expected = "ready" if changed == "model" else "unknown"
  assert result["run"]["items"][0]["ready_attempt"]["state"] == expected
  if changed == "model":
    # Reconciliation is read-only, even when a changed model cannot call the
    # owning mutation endpoint again.
    assert calls == [1]
  else:
    retry = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
    assert retry["blocked"] and calls == [1]


@pytest.mark.parametrize("invalid", [
  {"tests_passed": False}, {"scope": ["correctness"] * 6},
  {"reviewed_base_sha": "c" * 40}, {"independent_receipt_id": "missing"},
])
def test_draft_readiness_requires_exact_fresh_independent_evidence(setup, monkeypatch, invalid):
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("insufficient evidence"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(**invalid), db, principal))
  assert error.value.status_code in {409, 422}


@pytest.mark.parametrize("case", ["checks", "actor", "stopped", "head"])
def test_draft_readiness_rechecks_checks_actor_stop_and_head(setup, monkeypatch, case):
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("preflight failed"))
  if case == "checks":
    monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS,
      "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "PENDING"}}}]}})
  elif case == "actor":
    monkeypatch.setattr(domain, "read", lambda *a: {"id": 999})
  elif case == "stopped":
    db.get(models.ChatRun, principal.run_id).status = "stopped"
    db.commit()
  else:
    monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL,
      "draft": True, "head": {"sha": "c" * 40}}))
    # The route must not trust a changed head even if a mocked fetch bypasses
    # current_pull's identity guard.
    monkeypatch.setattr(domain, "pull_checks", lambda *a: {**CHECKS, "headRefOid": "c" * 40})
  with pytest.raises(HTTPException):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))


@pytest.mark.parametrize("case", ["actor", "permissions", "stop", "app"])
def test_single_draft_preflight_refuses_revoked_identity_or_rights(setup, monkeypatch, case):
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("no mutation after revocation"))
  reads = {"actor": 0, "pull": 0, "checks": 0}
  def actor(*a):
    reads["actor"] += 1
    return {"id": 999 if case == "actor" and reads["actor"] == 1 else 42}
  def pull(*a):
    reads["pull"] += 1
    repo = {**REPO, "permissions": {"push": False}} if case == "permissions" and reads["pull"] == 1 else REPO
    return repo, {**PULL, "draft": True}
  def checks(*a):
    reads["checks"] += 1
    if reads["checks"] == 1 and case in {"stop", "app"}:
      if case == "stop":
        db.get(models.ChatRun, principal.run_id).status = "stopped"
      else:
        db.get(models.App, row.app_id).github_access = False
      db.commit()
    return CHECKS
  monkeypatch.setattr(domain, "read", actor)
  monkeypatch.setattr(domain, "current_pull", pull)
  monkeypatch.setattr(domain, "pull_checks", checks)
  with pytest.raises(HTTPException):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert reads["checks"] == (0 if case == "actor" else 1)
  assert not row.outcomes_json[domain.key(ITEM)].get("ready_attempt")


@pytest.mark.parametrize("case", ["stop", "app", "model"])
def test_refusal_after_arming_readiness_is_not_attempted_and_can_retry(setup, monkeypatch, case):
  db, row, principal = ready_takeover(setup, monkeypatch)
  calls = []
  monkeypatch.setattr(domain, "mark_ready", lambda *a: calls.append(1))
  arm = domain.arm_ready
  def arm_then_revoke(*args):
    arm(*args)
    if case == "stop":
      db.get(models.ChatRun, principal.run_id).status = "stopped"
    elif case == "app":
      db.get(models.App, row.app_id).github_access = False
    else:
      db.get(models.Chat, row.chat_id).agent_settings_json = {"model": "gpt-5", "effort": "low"}
    db.commit()
  monkeypatch.setattr(domain, "arm_ready", arm_then_revoke)
  with pytest.raises(HTTPException):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  db.refresh(row)
  assert calls == []
  assert "ready_attempt" not in row.outcomes_json[domain.key(ITEM)]
  claim = db.query(models.AgentWorkClaim).filter_by(work_key=domain.ready_work_key(row.targets_json[0])).one()
  assert claim.released_at is not None
  if case == "stop":
    db.get(models.ChatRun, principal.run_id).status = "running"
  elif case == "app":
    db.get(models.App, row.app_id).github_access = True
  else:
    db.get(models.Chat, row.chat_id).agent_settings_json = {"model": "gpt-5", "effort": "xhigh"}
  db.commit()
  monkeypatch.setattr(domain, "arm_ready", arm)
  confirmed = iter([(REPO, {**PULL, "draft": True}), (REPO, {**PULL, "draft": False})])
  monkeypatch.setattr(domain, "current_pull", lambda *a: next(confirmed))
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert calls == [1]
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "ready"


def test_leftover_attempting_readiness_escalates_to_needs_you_on_observe(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "attempting", "head_sha": SHA, "base_sha": BASE}})
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("never replayed"))
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]
  assert observed["state"] == "needs_you"
  assert observed["items"][0]["state"] == "ready_unknown"
  assert observed["items"][0]["ready_attempt"]["state"] == "unknown"


def test_leftover_attempting_readiness_escalates_on_ready_retry(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "attempting", "head_sha": SHA, "base_sha": BASE}})
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("never replayed"))
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert result["blocked"]
  assert result["run"]["state"] == "needs_you"
  assert result["run"]["items"][0]["ready_attempt"]["state"] == "unknown"


def test_observe_actor_read_failure_leaves_readiness_unchanged(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  prior = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**prior,
    "ready_attempt": {"state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": False}))
  def broken(*a):
    raise OSError("gh unavailable")
  monkeypatch.setattr(domain, "read", broken)
  observed = asyncio.run(routes.observe_review(1, row.id, db, principal))["run"]
  assert observed["items"][0]["ready_attempt"]["state"] == "unknown"


@pytest.mark.parametrize("state", ["pushing", "push_unknown"])
@pytest.mark.parametrize("overlap", [False, True])
def test_draft_ready_cannot_cross_pending_repair_with_lagging_head(setup, monkeypatch, state, overlap):
  db, row, principal = ready_takeover(setup, monkeypatch)
  grant = overlapping_grant(db, row) if overlap else row
  previous = grant.outcomes_json.get(domain.key(ITEM), {})
  domain.save_outcome(db, grant, domain.key(ITEM), {**previous, "state": "needs_you",
    "repair_attempts": [{"state": state, "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}]})
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("PUBLIC_READY"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 409
  assert not row.outcomes_json[domain.key(ITEM)].get("ready_attempt")


@pytest.mark.parametrize("state", ["merge_unknown", "queued"])
def test_draft_ready_cannot_cross_overlapping_pending_merge(setup, monkeypatch, state):
  db, row, principal = ready_takeover(setup, monkeypatch)
  peer = overlapping_grant(db, row)
  domain.save_outcome(db, peer, domain.key(ITEM), {"state": state,
    "head_sha": SHA, "merge_attempted": True})
  monkeypatch.setattr(domain, "mark_ready", lambda *a: pytest.fail("PUBLIC_READY"))
  with pytest.raises(HTTPException) as error:
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  assert error.value.status_code == 409
  assert not row.outcomes_json[domain.key(ITEM)].get("ready_attempt")


@pytest.mark.parametrize("other_action", ["merge", "repair"])
@pytest.mark.parametrize("same_item", [False, True])
def test_readiness_and_incompatible_action_share_database_admission_fence(setup, monkeypatch, other_action, same_item):
  from concurrent.futures import ThreadPoolExecutor
  from threading import Barrier
  from app import agent_work_claims
  from app.database import SessionLocal
  db, row, principal = ready_takeover(setup, monkeypatch)
  peer = overlapping_grant(db, row, number=None if same_item else 8)
  target, peer_target = dict(row.targets_json[0]), dict(peer.targets_json[0])
  real_claim = agent_work_claims.claim_work
  barrier = Barrier(2)
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    barrier.wait(timeout=10)
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  def arm(action):
    with SessionLocal() as session:
      current = session.get(models.ContributionReviewRun, row.id if action == "ready" else peer.id)
      try:
        if action == "ready":
          result = domain.arm_ready(session, current, target, {"id": "ready", "state": "attempting",
            "head_sha": SHA, "base_sha": BASE}, principal)
        elif action == "repair":
          result = domain.arm_repair(session, current, peer_target, {}, {"id": "repair", "state": "pushing",
            "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}, principal)
        else:
          result = domain.arm_merge(session, current, peer_target, {"head_sha": SHA}, principal)
        assert result is None
        return "admitted"
      except HTTPException as exc:
        session.rollback()
        assert exc.status_code == 409
        return "blocked"
  with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(arm, ["ready", other_action]))
  assert sorted(results) == ["admitted", "blocked"]
  with SessionLocal() as restarted:
    assert sum(bool(item.get("ready_attempt") or item.get("merge_attempted") or item.get("repair_attempts"))
      for grant in restarted.query(models.ContributionReviewRun).all()
      for item in grant.outcomes_json.values()) == 1


@pytest.mark.parametrize("pending", ["merge", "repair"])
def test_readiness_revalidates_pending_public_actions_after_claim_commit(setup, monkeypatch, pending):
  from app import agent_work_claims
  from app.database import SessionLocal
  db, row, principal = ready_takeover(setup, monkeypatch)
  peer = overlapping_grant(db, row)
  target = dict(row.targets_json[0])
  real_claim = agent_work_claims.claim_work
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    with SessionLocal() as writer:
      saved = writer.get(models.ContributionReviewRun, peer.id)
      outcome = ({"state": "queued", "merge_attempted": True, "head_sha": SHA}
        if pending == "merge" else {"state": "needs_you", "repair_attempts": [
          {"state": "push_unknown", "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}]})
      domain.save_outcome(writer, saved, domain.key(ITEM), outcome)
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  with pytest.raises(HTTPException) as error:
    domain.arm_ready(db, row, target, {"id": "ready", "state": "attempting",
      "head_sha": SHA, "base_sha": BASE}, principal)
  assert error.value.status_code == 409
  db.rollback()
  db.refresh(row)
  assert not row.outcomes_json[domain.key(ITEM)].get("ready_attempt")


@pytest.mark.parametrize("action", ["merge", "repair"])
def test_pending_readiness_in_overlapping_grant_blocks_other_public_actions(setup, monkeypatch, action):
  db, row, principal = configure_repair(setup, monkeypatch)
  peer = overlapping_grant(db, row)
  domain.save_outcome(db, peer, domain.key(ITEM), {"state": "reviewing", "ready_attempt": {
    "state": "unknown", "head_sha": SHA, "base_sha": BASE}})
  target = dict(row.targets_json[0])
  with pytest.raises(HTTPException) as error:
    if action == "merge":
      domain.arm_merge(db, row, target, {"head_sha": SHA}, principal)
    else:
      domain.arm_repair(db, row, target, {}, {"state": "pushing", "from_sha": SHA,
        "head_sha": NEW, "base_sha": BASE}, principal)
  assert error.value.status_code == 409
  db.rollback()
  db.refresh(row)
  assert not row.outcomes_json[domain.key(ITEM)].get("repair_attempts")
  assert not row.outcomes_json[domain.key(ITEM)].get("merge_attempted")


def test_merge_retains_ready_receipt_resolved_during_claim_commit(setup, monkeypatch):
  from app import agent_work_claims
  from app.database import SessionLocal
  db, row, principal = ready_takeover(setup, monkeypatch)
  real_claim = agent_work_claims.claim_work
  receipt = {"id": "confirmed-ready", "state": "ready", "head_sha": SHA, "base_sha": BASE}
  def claim(*args, **kwargs):
    result = real_claim(*args, **kwargs)
    with SessionLocal() as writer:
      current = writer.get(models.ContributionReviewRun, row.id)
      previous = current.outcomes_json[domain.key(ITEM)]
      domain.save_outcome(writer, current, domain.key(ITEM), {**previous, "ready_attempt": receipt})
    return result
  monkeypatch.setattr(agent_work_claims, "claim_work", claim)
  monkeypatch.setattr(domain, "current_pull", lambda *a: (REPO, {**PULL, "draft": False}))
  monkeypatch.setattr(domain, "perform_merge", lambda *a: {"merged": True, "sha": "landed"})
  result = report(setup, tests_passed=True, reviewed_base_sha=BASE,
    independent_receipt_id="independent-clear")
  item = result["run"]["items"][0]
  assert item["state"] == "merged" and item["ready_attempt"] == receipt
  assert item["independent_reviews"][0]["id"] == "independent-clear"


def test_readiness_audit_preserves_admission_and_confirmation(setup, monkeypatch):
  db, row, principal = ready_takeover(setup, monkeypatch)
  confirmed = iter([(REPO, {**PULL, "draft": True}), (REPO, {**PULL, "draft": False})])
  monkeypatch.setattr(domain, "current_pull", lambda *a: next(confirmed))
  monkeypatch.setattr(domain, "mark_ready", lambda *a: {"id": "PR_7", "isDraft": False, "headRefOid": SHA})
  result = asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  attempt = result["run"]["items"][0]["ready_attempt"]
  audit = row.outcomes_json[domain.key(ITEM)]["ready_audit"]
  assert [event["state"] for event in audit] == ["attempting", "ready"]
  assert all(event["id"] == attempt["id"] for event in audit)


def test_known_not_dispatched_readiness_releases_exact_claim(setup, monkeypatch):
  from app.contribution_review_effects import NotDispatched
  db, row, principal = ready_takeover(setup, monkeypatch)
  monkeypatch.setattr(domain, "mark_ready", lambda *a: (_ for _ in ()).throw(NotDispatched()))
  with pytest.raises(HTTPException):
    asyncio.run(routes.mark_draft_ready(1, row.id, ready_body(), db, principal))
  db.refresh(row)
  outcome = row.outcomes_json[domain.key(ITEM)]
  assert "ready_attempt" not in outcome
  assert [event["state"] for event in outcome["ready_audit"]] == ["attempting", "not_dispatched"]


def test_changed_readiness_receipt_cannot_release_a_claim(setup, monkeypatch):
  db, row, _ = ready_takeover(setup, monkeypatch)
  previous = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**previous, 'ready_attempt': {
    'id': 'other-receipt', 'state': 'unknown', 'head_sha': SHA}})
  with pytest.raises(HTTPException):
    domain.disarm_ready(db, row, ITEM, 'my-receipt')
  assert row.outcomes_json[domain.key(ITEM)]['ready_attempt']['state'] == 'unknown'
