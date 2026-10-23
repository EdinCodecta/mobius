"""Refusal certification is scoped to the confirmed operation's write phase.

A changed confirmation is always refused before any cleanup, resume, claim or
write. ``not_attempted`` then says whether this operation's write phase was
never attempted. It is derived from the ledger, server-owned attempt artifacts
and the confirmation's digest-bound fields; the confirmation's own lifecycle
fields may withhold that certificate but never grant it.
"""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import github_contributions, models
from app.config import get_settings
from app.database import SessionLocal
from tests.test_github_routes import (
  _all_clear_review,
  _app_token,
  _assert_approval_changed,
  _attempt_paths_exist,
  _forbid_publication_work,
  _frozen,
  _github_state,  # noqa: F401 - autouse credential isolation
  _prepared_existing_pr_update,
  _prepared_existing_pr_update_stack,
  _prepared_new_stack,
  _prepared_standalone_pr,
  _record_bytes,
  _record_file,
  _rewrite_record,
  _write_contribution,
  _write_token,
  github_routes,
)


_SUBMIT_STACK = "submit-stack"
_UPDATE_STACK = "update-stack"
_CREATE_SUBMITTER = "contribute-button"
_UPDATE_SUBMITTER = "contribute-update-button"
_STACK_UPDATE_SUBMITTER = "contribute-stack-update-button"


def _ledger(app_id: int, record_id: str) -> dict:
  return json.loads(_record_file(app_id, record_id).read_text())


def _confirm(app_id: int, record_ids: list[str]) -> list[dict]:
  """Freeze exactly what the ledger shows now, as an honest app displays it."""
  return [_frozen(_ledger(app_id, record_id)) for record_id in record_ids]


def _publish_create(record: dict, number: int) -> None:
  """A settled create publication of this record's reviewed head."""
  record.update({
    "status": "open",
    "publication_stage": "ready",
    "url": f"https://github.com/{record['repo']}/pull/{number}",
    "number": number,
    "head_repository": record["repo"],
    "last_submit_stage": "pushed",
    "last_submit_push_sha": record["plan"]["head_sha"],
  })


def _claimed_by(record: dict, submitter: str) -> None:
  """Leave the exact claim digest one operation writes at its claim."""
  record["submitter"] = submitter
  record["submit_started_at"] = "2026-10-02T00:00:00Z"
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256({
      **record, "status": "submitting", "submitter": submitter,
    })
  )


def _with_status(records: list[dict], index: int, status: str) -> list[dict]:
  forged = [_frozen(record) for record in records]
  forged[index]["status"] = status
  return forged


def _post_stack(client, app_token, app_id, route, record_ids, expected):
  return client.post(
    f"/api/github/contributions/{app_id}/{route}",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids, "expected_records": expected},
  )


def _post_one(client, app_token, app_id, record_id, route, expected):
  suffix = "submit" if route == "submit" else "update-existing"
  return client.post(
    f"/api/github/contributions/{app_id}/{record_id}/{suffix}",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"expected_record": expected},
  )


def _assert_refused_without_effect(
  response, app_id, record_ids, before, calls, *, changed, not_attempted,
  attempts=None,
):
  detail = _assert_approval_changed(
    response, changed=changed, not_attempted=not_attempted,
  )
  assert calls == []
  assert _record_bytes(app_id, record_ids) == before
  assert _attempt_paths_exist(app_id, record_ids) == (
    attempts if attempts is not None else [False] * len(record_ids)
  )
  return detail


@pytest.fixture
def stack_app(client, owner_token):
  _write_token(login="octocat")
  return _app_token(client, owner_token, github_access=True)


# --- the current phase is still unsent -----------------------------------


def test_phased_stack_refusal_is_unsent_beside_its_published_parent(
  client, stack_app, monkeypatch,
):
  """A settled public parent is context; only the private child is the phase."""
  app_id, app_token = stack_app
  record_ids, _ = _prepared_new_stack(app_id, "phase-public-parent")
  _rewrite_record(app_id, record_ids[0], lambda r: _publish_create(r, 81))
  confirmed = _confirm(app_id, record_ids)
  _rewrite_record(
    app_id, record_ids[1],
    lambda r: r["plan"].update({"body_draft": "Unconfirmed child text."}),
  )
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _SUBMIT_STACK, record_ids, confirmed,
  )

  detail = _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=[record_ids[1]], not_attempted=True,
  )
  assert [record["status"] for record in detail["records"]] == [
    "open", "prepared",
  ]


def test_update_stack_refusal_is_unsent_when_a_layer_head_was_already_public(
  client, stack_app, monkeypatch,
):
  """An update whose head predates its claim was not pushed by this phase."""
  app_id, app_token = stack_app
  record_ids, _ = _prepared_existing_pr_update_stack(app_id)
  _rewrite_record(app_id, record_ids[0], lambda r: r.update({
    "last_submit_stage": "pushed",
    "last_submit_push_sha": r["plan"]["head_sha"],
  }))
  confirmed = _confirm(app_id, record_ids)
  _rewrite_record(
    app_id, record_ids[1], lambda r: r["plan"].update({"title": "Changed"}),
  )
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _UPDATE_STACK, record_ids, confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=[record_ids[1]], not_attempted=True,
  )


def test_update_existing_refusal_is_unsent_when_its_head_was_already_public(
  client, stack_app, monkeypatch,
):
  app_id, app_token = stack_app
  record_id = "phase-update-public-head"
  _prepared_existing_pr_update(app_id, record_id)
  _rewrite_record(app_id, record_id, lambda r: r.update({
    "last_submit_stage": "pushed",
    "last_submit_push_sha": r["plan"]["head_sha"],
  }))
  [confirmed] = _confirm(app_id, [record_id])
  _rewrite_record(
    app_id, record_id, lambda r: r["plan"].update({"title": "Changed"}),
  )
  before = _record_bytes(app_id, [record_id])
  calls = _forbid_publication_work(monkeypatch)

  response = _post_one(
    client, app_token, app_id, record_id, "update", confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, [record_id], before, calls,
    changed=[record_id], not_attempted=True,
  )


def _prepared_update_then_deferred_create(app_id: int) -> list[str]:
  """An existing-PR update layer followed by a new private layer."""
  stack_id = "phase-deferred"
  record_ids = [f"{stack_id}-01", f"{stack_id}-02"]
  update_head, create_head = "a" * 40, "c" * 40
  parent_branch = f"stack/{stack_id}/01-parent"
  for position, record_id in enumerate(record_ids, start=1):
    repo_path = Path(get_settings().data_dir) / "contrib" / record_id / "worktree"
    (repo_path / ".git").mkdir(parents=True, exist_ok=True)
    updating = position == 1
    head = update_head if updating else create_head
    branch = parent_branch if updating else f"stack/{stack_id}/02-child"
    diff_text = f"diff --git a/{record_id} b/{record_id}\n+reviewed\n"
    record = {
      "id": record_id,
      "type": "pr",
      "repo": "mobius-os/app-demo",
      "status": "prepared",
      "title": f"Layer {position}",
      "branch": branch,
      "plan": {
        "action": "pr_update" if updating else "pr",
        "repo": "mobius-os/app-demo",
        "title": f"Layer {position}",
        "body_draft": f"Reviewed layer {position}.",
        "branch": branch,
        "repo_path": str(repo_path),
        "base_sha": "b" * 40 if updating else update_head,
        "head_sha": head,
        "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
        "stack": {
          "id": stack_id,
          "position": position,
          "total": 2,
          "parent_record_id": "" if updating else record_ids[0],
          "base_branch": "main" if updating else parent_branch,
        },
      },
      "quality_review": _all_clear_review(head),
    }
    if updating:
      record.update({
        "number": 58,
        "url": "https://github.com/mobius-os/app-demo/pull/58",
        "head_repository": "octocat/app-demo",
      })
      record["plan"]["pr_metadata"] = {
        "old_title": "Layer 1", "old_body": "Reviewed layer 1.",
      }
    _write_contribution(app_id, record_id, record, diff_text)
  return record_ids


def test_update_stack_refusal_leaves_a_deferred_layer_outside_its_phase(
  client, stack_app, monkeypatch,
):
  """A deferred new layer is untouched by an update phase, even if changed."""
  app_id, app_token = stack_app
  record_ids = _prepared_update_then_deferred_create(app_id)
  # An earlier, different publication of the deferred branch is not this
  # update phase's write.
  _rewrite_record(app_id, record_ids[1], lambda r: r.update({
    "last_submit_stage": "pushed",
    "last_submit_push_sha": r["plan"]["head_sha"],
  }))
  confirmed = _confirm(app_id, record_ids)
  _rewrite_record(
    app_id, record_ids[1],
    lambda r: r["plan"].update({"body_draft": "Deferred text changed."}),
  )
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _UPDATE_STACK, record_ids, confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=[record_ids[1]], not_attempted=True,
  )


def test_deferred_layer_attempt_artifact_still_withholds_certification(
  client, stack_app, monkeypatch,
):
  app_id, app_token = stack_app
  record_ids = _prepared_update_then_deferred_create(app_id)
  confirmed = _confirm(app_id, record_ids)
  _rewrite_record(
    app_id, record_ids[1],
    lambda r: r["plan"].update({"body_draft": "Deferred text changed."}),
  )
  artifact = github_routes.contribution_runtime.relay_request_path(
    app_id, record_ids[1], create_parent=True,
  )
  artifact.write_text("{}")
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _UPDATE_STACK, record_ids, confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=[record_ids[1]], not_attempted=False,
  )
  assert artifact.read_text() == "{}"


# --- published parent proof versus uncertain attempts ---------------------


@pytest.mark.parametrize("uncertainty", [
  "personal_receipt", "relay_claim", "relay_request", "submitting",
])
def test_published_parent_with_an_uncertain_attempt_is_never_unsent(
  client, stack_app, monkeypatch, uncertainty,
):
  app_id, app_token = stack_app
  record_ids, _ = _prepared_new_stack(app_id, f"phase-uncertain-{uncertainty}")
  _rewrite_record(app_id, record_ids[0], lambda r: _publish_create(r, 82))
  confirmed = _confirm(app_id, record_ids)
  runtime = github_routes.contribution_runtime
  if uncertainty == "submitting":
    _rewrite_record(
      app_id, record_ids[0], lambda r: r.update({"status": "submitting"}),
    )
  else:
    path_for = {
      "personal_receipt": runtime.personal_attempt_path,
      "relay_claim": runtime.relay_claim_path,
      "relay_request": runtime.relay_request_path,
    }[uncertainty]
    path_for(app_id, record_ids[0], create_parent=True).write_text("{}")
  _rewrite_record(
    app_id, record_ids[1],
    lambda r: r["plan"].update({"title": "Unconfirmed child title"}),
  )
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _SUBMIT_STACK, record_ids, confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=[record_ids[1]], not_attempted=False,
    attempts=[uncertainty == "personal_receipt", False],
  )


@pytest.mark.parametrize("forged_status", [None, "open", "draft", "merged"])
def test_parent_published_after_confirmation_is_never_unsent(
  client, stack_app, monkeypatch, forged_status,
):
  """Neither the honest nor a forged confirmation lifecycle grants certainty."""
  app_id, app_token = stack_app
  record_ids, _ = _prepared_new_stack(
    app_id, f"phase-late-parent-{forged_status or 'honest'}",
  )
  confirmed = _confirm(app_id, record_ids)
  if forged_status is not None:
    confirmed = _with_status(confirmed, 0, forged_status)
  _rewrite_record(app_id, record_ids[0], lambda r: _publish_create(r, 83))
  _rewrite_record(
    app_id, record_ids[1],
    lambda r: r["plan"].update({"title": "Unconfirmed child title"}),
  )
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _SUBMIT_STACK, record_ids, confirmed,
  )

  # The parent's public identity is bound, so its late publication also
  # registers as a change.
  _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=record_ids, not_attempted=False,
  )


def test_published_parent_moved_within_its_bound_pr_stays_context(
  client, stack_app, monkeypatch,
):
  """A bound public PR that later changed stage is still not this phase."""
  app_id, app_token = stack_app
  record_ids, _ = _prepared_new_stack(app_id, "phase-parent-stage")
  _rewrite_record(app_id, record_ids[0], lambda r: _publish_create(r, 84))
  confirmed = _confirm(app_id, record_ids)
  _rewrite_record(app_id, record_ids[0], lambda r: r.update({
    "publication_stage": "draft", "status": "draft",
  }))
  _rewrite_record(
    app_id, record_ids[1],
    lambda r: r["plan"].update({"title": "Unconfirmed child title"}),
  )
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _SUBMIT_STACK, record_ids, confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=record_ids, not_attempted=True,
  )


@pytest.mark.parametrize("identity", ["number", "url"])
def test_published_parent_whose_pr_identity_changed_is_never_unsent(
  client, stack_app, monkeypatch, identity,
):
  app_id, app_token = stack_app
  record_ids, _ = _prepared_new_stack(app_id, f"phase-parent-{identity}")
  _rewrite_record(app_id, record_ids[0], lambda r: _publish_create(r, 84))
  confirmed = _confirm(app_id, record_ids)

  def retarget(record):
    if identity == "number":
      record["number"] = 87
    else:
      record["url"] = "https://github.com/mobius-os/mobius/pull/87"

  _rewrite_record(app_id, record_ids[0], retarget)
  _rewrite_record(
    app_id, record_ids[1],
    lambda r: r["plan"].update({"title": "Unconfirmed child title"}),
  )
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _SUBMIT_STACK, record_ids, confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=record_ids, not_attempted=False,
  )


# --- possibly written current-phase attempts -----------------------------


@pytest.mark.parametrize("forged_status", [None, "open", "draft", "merged"])
def test_update_layer_completed_by_this_operation_is_never_unsent(
  client, stack_app, monkeypatch, forged_status,
):
  """A completed update can leave every bound field unchanged; its claim can't."""
  app_id, app_token = stack_app
  record_ids, _ = _prepared_existing_pr_update_stack(app_id)
  for record_id in record_ids:
    _rewrite_record(
      app_id, record_id, lambda r: r.update({"publication_stage": "ready"}),
    )
  confirmed = _confirm(app_id, record_ids)
  if forged_status is not None:
    confirmed = _with_status(confirmed, 0, forged_status)

  def completed(record):
    _claimed_by(record, _STACK_UPDATE_SUBMITTER)
    record.update({
      "status": "open",
      "last_submit_stage": "pushed",
      "last_submit_push_sha": record["plan"]["head_sha"],
      "last_updated_pr_at": "2026-10-02T00:01:00Z",
    })

  _rewrite_record(app_id, record_ids[0], completed)
  assert github_contributions.publication_approval_sha256(
    _ledger(app_id, record_ids[0]),
  ) == github_contributions.publication_approval_sha256(confirmed[0])
  _rewrite_record(
    app_id, record_ids[1], lambda r: r["plan"].update({"title": "Changed"}),
  )
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _UPDATE_STACK, record_ids, confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=[record_ids[1]], not_attempted=False,
  )


def test_honest_prepared_confirmation_withholds_without_a_claim_digest(
  client, stack_app, monkeypatch,
):
  """A layer confirmed private but now public withholds even with no claim."""
  app_id, app_token = stack_app
  record_ids, _ = _prepared_existing_pr_update_stack(app_id)
  confirmed = _confirm(app_id, record_ids)
  _rewrite_record(app_id, record_ids[0], lambda r: r.update({"status": "open"}))
  _rewrite_record(
    app_id, record_ids[1], lambda r: r["plan"].update({"title": "Changed"}),
  )
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _UPDATE_STACK, record_ids, confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=[record_ids[1]], not_attempted=False,
  )


def test_bound_action_keeps_a_reprepared_row_in_the_confirmed_phase(
  client, stack_app, monkeypatch,
):
  """A row re-prepared for another action still answers for the confirmed one."""
  app_id, app_token = stack_app
  record_id = "phase-reprepared-action"
  _prepared_standalone_pr(app_id, record_id)
  [confirmed] = _confirm(app_id, [record_id])

  def reprepared_after_push(record):
    record["plan"]["action"] = "pr_update"
    record["last_submit_stage"] = "pushed"
    record["last_submit_push_sha"] = record["plan"]["head_sha"]

  _rewrite_record(app_id, record_id, reprepared_after_push)
  before = _record_bytes(app_id, [record_id])
  calls = _forbid_publication_work(monkeypatch)

  response = _post_one(
    client, app_token, app_id, record_id, "submit", confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, [record_id], before, calls,
    changed=[record_id], not_attempted=False,
  )


@pytest.mark.parametrize("forged_status", [None, "open", "submitting"])
def test_pushed_create_branch_is_never_unsent(
  client, stack_app, monkeypatch, forged_status,
):
  """A new PR's head on GitHub can only be this proposal's write."""
  app_id, app_token = stack_app
  record_id = f"phase-pushed-create-{forged_status or 'honest'}"
  _prepared_standalone_pr(app_id, record_id)
  [confirmed] = _confirm(app_id, [record_id])
  _rewrite_record(app_id, record_id, lambda r: r.update({
    "last_submit_stage": "pushed",
    "last_submit_push_sha": r["plan"]["head_sha"],
    "last_submit_error": "GitHub could not open the pull request.",
    "plan": {**r["plan"], "title": "Changed title"},
  }))
  if forged_status is not None:
    # Forge the whole lifecycle to look untouched; bound fields stay honest.
    confirmed = {
      **confirmed, "status": forged_status,
      "last_submit_stage": None, "last_submit_push_sha": None,
    }
  before = _record_bytes(app_id, [record_id])
  calls = _forbid_publication_work(monkeypatch)

  response = _post_one(
    client, app_token, app_id, record_id, "submit", confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, [record_id], before, calls,
    changed=[record_id], not_attempted=False,
  )


@pytest.mark.parametrize("forged_status", [None, "open"])
def test_update_pushed_after_its_own_claim_is_never_unsent(
  client, stack_app, monkeypatch, forged_status,
):
  """A failed update that pushed keeps its claim digest even without a receipt."""
  app_id, app_token = stack_app
  record_id = f"phase-pushed-update-{forged_status or 'honest'}"
  _prepared_existing_pr_update(app_id, record_id)
  [confirmed] = _confirm(app_id, [record_id])
  if forged_status is not None:
    confirmed["status"] = forged_status

  def pushed_then_failed(record):
    _claimed_by(record, _UPDATE_SUBMITTER)
    record.update({
      "status": "prepared",
      "last_submit_stage": "pushed",
      "last_submit_push_sha": record["plan"]["head_sha"],
      "last_submit_error": "GitHub refused the title edit.",
    })
    record["plan"]["body_draft"] += "\n\nA later edit."

  _rewrite_record(app_id, record_id, pushed_then_failed)
  before = _record_bytes(app_id, [record_id])
  calls = _forbid_publication_work(monkeypatch)

  response = _post_one(
    client, app_token, app_id, record_id, "update", confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, [record_id], before, calls,
    changed=[record_id], not_attempted=False,
  )


@pytest.mark.parametrize("stage", ["push_pending", "push_ambiguous"])
def test_update_stack_layer_with_an_unsettled_push_is_never_unsent(
  client, stack_app, monkeypatch, stage,
):
  app_id, app_token = stack_app
  record_ids, _ = _prepared_existing_pr_update_stack(app_id)
  _rewrite_record(app_id, record_ids[0], lambda r: r.update({
    "last_submit_stage": stage,
    "last_submit_push_sha": r["plan"]["head_sha"],
  }))
  confirmed = _confirm(app_id, record_ids)
  _rewrite_record(
    app_id, record_ids[1], lambda r: r["plan"].update({"title": "Changed"}),
  )
  before = _record_bytes(app_id, record_ids)
  calls = _forbid_publication_work(monkeypatch)

  response = _post_stack(
    client, app_token, app_id, _UPDATE_STACK, record_ids, confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, record_ids, before, calls,
    changed=[record_ids[1]], not_attempted=False,
  )


@pytest.mark.parametrize("pushed", ["confirmed_head", "normalized_head"])
def test_normalized_head_is_refused_and_never_folded_into_unsent(
  client, stack_app, monkeypatch, pushed,
):
  """Attribution normalization moves the head; it is a change, never a match."""
  app_id, app_token = stack_app
  record_id = f"phase-normalized-{pushed}"
  _prepared_standalone_pr(app_id, record_id)
  [confirmed] = _confirm(app_id, [record_id])
  confirmed_head = confirmed["plan"]["head_sha"]
  normalized_head = "9" * 40

  def normalized_then_failed(record):
    _claimed_by(record, _CREATE_SUBMITTER)
    record["status"] = "prepared"
    record["plan"] = {**record["plan"], "head_sha": normalized_head}
    record["head_sha"] = normalized_head
    record["last_submit_stage"] = "pushed"
    record["last_submit_push_sha"] = (
      normalized_head if pushed == "normalized_head" else confirmed_head
    )

  _rewrite_record(app_id, record_id, normalized_then_failed)
  before = _record_bytes(app_id, [record_id])
  calls = _forbid_publication_work(monkeypatch)

  response = _post_one(
    client, app_token, app_id, record_id, "submit", confirmed,
  )

  detail = _assert_refused_without_effect(
    response, app_id, [record_id], before, calls,
    changed=[record_id], not_attempted=False,
  )
  assert detail["record"]["plan"]["head_sha"] == normalized_head


def test_settled_prepush_failure_of_this_operation_stays_unsent(
  client, stack_app, monkeypatch,
):
  """A claim alone, settled before any push, is not a possible public write."""
  app_id, app_token = stack_app
  record_id = "phase-prepush-failure"
  _prepared_standalone_pr(app_id, record_id)
  [confirmed] = _confirm(app_id, [record_id])

  def failed_before_push(record):
    _claimed_by(record, _CREATE_SUBMITTER)
    record["last_submit_error"] = "Nothing was pushed."
    record["plan"]["title"] = "Changed title"

  _rewrite_record(app_id, record_id, failed_before_push)
  before = _record_bytes(app_id, [record_id])
  calls = _forbid_publication_work(monkeypatch)

  response = _post_one(
    client, app_token, app_id, record_id, "submit", confirmed,
  )

  _assert_refused_without_effect(
    response, app_id, [record_id], before, calls,
    changed=[record_id], not_attempted=True,
  )


# --- the claim recheck certifies the same phase --------------------------


def _claim_stack(app_id, record_ids, confirmed, **kwargs):
  db = SessionLocal()
  armed = []
  try:
    with pytest.raises(HTTPException) as raised:
      github_contributions._claim_stack_records(
        app_id=app_id, record_ids=record_ids, db=db,
        expected_nonce=db.get(models.App, app_id).token_nonce,
        before_claim_write=lambda *args: armed.append(args),
        confirmed={record["id"]: record for record in confirmed},
        **kwargs,
      )
  finally:
    db.close()
  assert armed == []
  return raised.value


def test_stack_claim_recheck_certifies_its_phase_like_the_route(
  client, owner_token,
):
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_ids, _ = _prepared_new_stack(app_id, "phase-claim-recheck")
  _rewrite_record(app_id, record_ids[0], lambda r: _publish_create(r, 85))
  confirmed = _confirm(app_id, record_ids)
  _rewrite_record(
    app_id, record_ids[1],
    lambda r: r["plan"].update({"title": "Raced child title"}),
  )
  before = _record_bytes(app_id, record_ids)

  raised = _claim_stack(
    app_id, record_ids, confirmed,
    allowed_actions=frozenset({"pr", "pr_update"}),
    prepared_actions=frozenset({"pr"}),
  )

  assert raised.status_code == 409
  assert raised.detail["code"] == "approval_changed"
  assert raised.detail["changed_record_ids"] == [record_ids[1]]
  assert raised.detail["not_attempted"] is True
  assert _record_bytes(app_id, record_ids) == before


def test_stack_claim_recheck_withholds_for_a_late_published_parent(
  client, owner_token,
):
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_ids, _ = _prepared_new_stack(app_id, "phase-claim-late")
  confirmed = _with_status(_confirm(app_id, record_ids), 0, "open")
  _rewrite_record(app_id, record_ids[0], lambda r: _publish_create(r, 86))
  _rewrite_record(
    app_id, record_ids[1],
    lambda r: r["plan"].update({"title": "Raced child title"}),
  )
  before = _record_bytes(app_id, record_ids)

  raised = _claim_stack(
    app_id, record_ids, confirmed,
    allowed_actions=frozenset({"pr", "pr_update"}),
    prepared_actions=frozenset({"pr"}),
  )

  assert raised.status_code == 409
  assert raised.detail["not_attempted"] is False
  assert _record_bytes(app_id, record_ids) == before


# --- wire contract ------------------------------------------------------


def test_status_capability_still_advertises_the_shared_field_constant(
  client, auth,
):
  response = client.get("/api/github/status", headers=auth)
  assert response.status_code == 200, response.text
  assert response.json()["frozen_publication_binding"] == {
    "record_fields": list(github_contributions.PUBLICATION_APPROVAL_FIELDS),
  }
  assert "status" not in github_contributions.PUBLICATION_APPROVAL_FIELDS
  assert "submitter" not in github_contributions.PUBLICATION_APPROVAL_FIELDS


def test_matching_legacy_and_confirmed_sends_are_unaffected_by_certification():
  """Certification runs only on a refusal; matching or legacy calls pass."""
  record = {"id": "legacy", "plan": {"action": "pr"}}
  for expected in (None, {"legacy": _frozen(record)}):
    assert github_contributions._assert_publication_approval(
      app_id=1, records=[record], expected=expected, stack=False,
      phase_actions=frozenset({"pr"}), submitter=_CREATE_SUBMITTER,
    ) is None

