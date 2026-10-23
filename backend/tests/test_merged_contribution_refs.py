"""Contribute's merged-contribution witnesses, recorded at settle or backfilled."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import app_git, github_contributions as gc, platform_update
from scripts import backfill_merged_contribution_refs as backfill
from tests.test_overlay_units import (  # noqa: F401
  _DRAFT, _MERGED, _commit, _git, _pr_head, _release, _show, repo,
)


@pytest.fixture
def data_dir(repo, monkeypatch):
  """``repo`` is the live platform checkout of this data directory."""
  monkeypatch.setattr(gc, "get_settings", lambda: SimpleNamespace(data_dir=str(repo.parent)))
  return repo.parent


def _merged_record(head: str, **plan) -> dict:
  return {
    "id": "rec-1", "status": "merged", "number": 7,
    "plan": {"repo": app_git.PLATFORM_RELEASE_REPO, "head_sha": head,
             "files": ["greet.py"], **plan},
  }


def test_a_settled_merged_platform_record_records_its_reviewed_head(repo, data_dir):
  head = _pr_head(repo, {"greet.py": _DRAFT})
  merge = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])

  ref = gc._record_merged_contribution(_merged_record(head), repo, merge)

  assert ref == f"{app_git.MERGED_CONTRIBUTION_PREFIX}/7"
  meta = json.loads(_git(repo, "log", "-1", "--format=%B", ref))
  assert meta["head_sha"] == head and meta["paths"] == ["greet.py"]
  assert (meta["repo"], meta["merge_sha"]) == (app_git.PLATFORM_RELEASE_REPO, merge)
  assert _git(repo, "rev-parse", f"{ref}^") == head
  assert _git(repo, "rev-parse", f"{ref}^{{tree}}") == _git(repo, "rev-parse", f"{head}^{{tree}}")
  record = _merged_record(head)
  assert gc._record_merged_contribution({**record, "number": None}, repo, merge) is None
  assert gc._record_merged_contribution(record, repo, None) is None


def test_an_app_contribution_records_no_platform_witness(data_dir):
  app = data_dir / "apps" / "42"
  app.mkdir(parents=True)
  _git(app, "init", "-q", "-b", "main")
  head = _commit(app, {"greet.py": _DRAFT}, "Draft greet")

  assert gc._record_merged_contribution(_merged_record(head), app, head) is None
  assert _git(app, "for-each-ref", app_git.MERGED_CONTRIBUTION_PREFIX) == ""


def _write_records(records: Path, head: str, *entries: dict) -> None:
  records.mkdir(exist_ok=True)
  for entry in entries:
    entry = dict(entry)
    plan = {"repo": app_git.PLATFORM_RELEASE_REPO, "head_sha": head,
            "files": ["greet.py"], **entry.pop("plan", {})}
    (records / f"{entry['id']}.json").write_text(json.dumps(
      {"status": "merged", "plan": plan, **entry},
    ))


def test_backfill_writes_witnesses_only_for_merged_prs_the_release_lacks(repo, tmp_path):
  head = _pr_head(repo, {"greet.py": _DRAFT})
  _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  # The installed release already carries #5; #7 merges in the next release.
  old_merge = _release(repo, [({"old.py": "O = 1\n"}, "Old contribution (#5)")])
  _git(repo, "branch", "-f", "upstream", old_merge)
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])
  records = tmp_path / "contributions"
  _write_records(
    records, head,
    {"id": "a", "number": 7},
    {"id": "a-older", "number": 7, "updated_at": "", "plan": {"head_sha": "0" * 40}},
    {"id": "b", "number": 5},
    {"id": "c", "number": 9, "status": "open"},
    {"id": "d", "number": 11, "plan": {"head_sha": "0" * 40}},
    # The record's own SHA fields are not trusted, as on the live settle path.
    {"id": "e", "number": 12, "merge_commit_sha": release, "last_land_head_sha": release},
    {"id": "f", "number": 13, "plan": {"repo": "someone/mobius-fork"}},
    {"id": "g", "number": 14, "plan": {"source_repo_path": str(tmp_path / "elsewhere")}},
    {"id": "h", "number": 15},  # merged where the installed release has it
  )
  (records / "a.json").write_text(
    (records / "a.json").read_text().replace('"status"', '"updated_at": "2", "status"'),
  )
  github = {7: release, 15: old_merge}
  asked = []

  def merge_commit(record):
    asked.append(record["number"])
    return github.get(record["number"])

  assert backfill.backfill(
    records, repo, dry_run=True, merge_commit=merge_commit,
  )["written"] == [7]
  assert _git(repo, "for-each-ref", app_git.MERGED_CONTRIBUTION_PREFIX) == ""
  asked.clear()
  outcome = backfill.backfill(records, repo, merge_commit=merge_commit)

  assert outcome == {
    "written": [7], "integrated": [5, 15], "unavailable": [11, 12],
    "pre_release_dropped": [], "pre_release_kept": [],
  }
  assert 5 not in asked  # named "(#5)" by the installed release: no lookup
  assert _git(repo, "for-each-ref", "--format=%(refname)",
              app_git.MERGED_CONTRIBUTION_PREFIX) == f"{app_git.MERGED_CONTRIBUTION_PREFIX}/7"
  result = platform_update._final_tree_merge(repo, local, release)
  assert result.status == "clean"
  assert _show(repo, result.merged_tree_oid, "greet.py") == _MERGED


def test_backfill_migrates_pre_release_witnesses_and_drops_only_integrated_ones(
  repo, tmp_path,
):
  """Version 1 witnesses are rewritten from Contribute's records, taking the
  merge commit they stored when GitHub cannot answer. One whose merge the
  installed release already carries is dropped; every other one is kept."""
  from tests.test_overlay_units import _legacy_witness

  draft = _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  head = _pr_head(repo, {"greet.py": _DRAFT})
  installed = _release(repo, [({"old.py": "O = 1\n"}, "Old contribution")])
  _git(repo, "branch", "-f", "upstream", installed)
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])
  _legacy_witness(repo, draft, number=7)  # no merge stored; GitHub knows it
  _legacy_witness(repo, draft, number=8, merge=release)  # GitHub cannot answer
  kept_pending = _legacy_witness(repo, draft, number=9, merge=release)  # no record
  integrated = _legacy_witness(repo, draft, number=10, merge=installed)
  kept_unknown = _legacy_witness(repo, draft, number=11)
  records = tmp_path / "contributions"
  _write_records(
    records, head, {"id": "rec-7", "number": 7}, {"id": "rec-8", "number": 8},
  )
  github = {7: release}
  assert platform_update._final_tree_merge(repo, local, release).status == "conflict"

  def run(**kwargs):
    return backfill.backfill(
      records, repo, merge_commit=lambda record: github.get(record["number"]), **kwargs,
    )

  expected = {
    "written": [7, 8], "integrated": [], "unavailable": [],
    "pre_release_dropped": [10], "pre_release_kept": [9, 11],
  }
  assert run(dry_run=True) == expected
  assert _git(repo, "for-each-ref", integrated) != ""
  assert run() == expected

  for number in (7, 8):
    ref = f"{app_git.MERGED_CONTRIBUTION_PREFIX}/{number}"
    meta = json.loads(_git(repo, "log", "-1", "--format=%B", ref))
    assert (meta["version"], meta["head_sha"], meta["merge_sha"]) == (2, head, release)
  assert _git(repo, "for-each-ref", integrated) == ""
  assert _git(repo, "for-each-ref", "--format=%(refname)", kept_pending) == kept_pending
  assert _git(repo, "for-each-ref", "--format=%(refname)", kept_unknown) == kept_unknown
  result = platform_update._final_tree_merge(repo, local, release)
  assert result.status == "clean"
  assert _show(repo, result.merged_tree_oid, "greet.py") == _MERGED
  rewritten = _git(repo, "rev-parse", f"{app_git.MERGED_CONTRIBUTION_PREFIX}/7")
  assert run() == {**expected, "pre_release_dropped": []}
  assert _git(repo, "rev-parse", f"{app_git.MERGED_CONTRIBUTION_PREFIX}/7") == rewritten
