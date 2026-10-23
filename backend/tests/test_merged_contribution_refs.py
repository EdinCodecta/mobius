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


def test_backfill_writes_witnesses_only_for_merged_prs_the_release_lacks(repo, tmp_path):
  head = _pr_head(repo, {"greet.py": _DRAFT})
  _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  # The installed release already carries #5; #7 merges in the next release.
  old_merge = _release(repo, [({"old.py": "O = 1\n"}, "Old contribution (#5)")])
  _git(repo, "branch", "-f", "upstream", old_merge)
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])
  records = tmp_path / "contributions"
  records.mkdir()

  def record(name, merge=None, **fields):
    plan = {"repo": app_git.PLATFORM_RELEASE_REPO, "head_sha": head,
            "files": ["greet.py"], **fields.pop("plan", {})}
    (records / f"{name}.json").write_text(json.dumps(
      {"id": name, "status": "merged", "merge_commit_sha": merge, "plan": plan, **fields},
    ))

  record("a", release, number=7)
  record("b", old_merge, number=5)
  record("c", release, number=9, status="open")
  record("d", release, number=11, plan={"head_sha": "0" * 40})
  record("e", None, number=12)  # no merge commit: never matched by number alone
  record("f", release, number=13, plan={"repo": "someone/mobius-fork"})
  record("g", release, number=14, plan={"source_repo_path": str(tmp_path / "elsewhere")})

  assert backfill.backfill(records, repo, dry_run=True)["written"] == [7]
  assert _git(repo, "for-each-ref", app_git.MERGED_CONTRIBUTION_PREFIX) == ""
  outcome = backfill.backfill(records, repo)

  assert outcome == {"written": [7], "integrated": [5], "unavailable": [11, 12]}
  assert _git(repo, "for-each-ref", "--format=%(refname)",
              app_git.MERGED_CONTRIBUTION_PREFIX) == f"{app_git.MERGED_CONTRIBUTION_PREFIX}/7"
  result = platform_update._final_tree_merge(repo, local, release)
  assert result.status == "clean"
  assert _show(repo, result.merged_tree_oid, "greet.py") == _MERGED
