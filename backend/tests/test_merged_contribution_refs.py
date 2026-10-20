"""Contribute's merged-contribution witnesses, recorded at settle or backfilled."""
from __future__ import annotations

import json
from pathlib import Path

from app import app_git, github_contributions as gc, platform_update
from scripts import backfill_merged_contribution_refs as backfill
from tests.test_overlay_units import _DRAFT, _MERGED, _commit, _git, _release, _show, repo  # noqa: F401


def test_a_settled_merged_record_records_its_captured_source(repo):
  draft = _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  record = {"id": "rec-1", "status": "merged", "number": 7,
            "plan": {"source_sha": draft, "files": ["greet.py"]}}

  ref = gc._record_merged_contribution(record, repo, None)

  assert ref == f"{app_git.MERGED_CONTRIBUTION_PREFIX}/7"
  meta = json.loads(_git(repo, "log", "-1", "--format=%B", ref))
  assert meta["source_sha"] == draft and meta["paths"] == ["greet.py"]
  assert _git(repo, "rev-parse", f"{ref}^") == draft
  assert gc._record_merged_contribution({**record, "number": None}, repo, None) is None


def test_backfill_writes_witnesses_only_for_merged_prs_the_release_lacks(repo, tmp_path):
  draft = _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  old = _commit(repo, {"old.py": "O = 1\n"}, "Old contribution")
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  # The installed release already carries #5; #7 merges in the next release.
  _git(repo, "branch", "-f", "upstream",
       _release(repo, [({"old.py": "O = 1\n"}, "Old contribution (#5)")]))
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])
  records = tmp_path / "contributions"
  records.mkdir()

  def record(name, **fields):
    (records / f"{name}.json").write_text(json.dumps(fields))

  plan = {"source_repo_path": str(repo)}
  record("a", id="a", status="merged", number=7,
         plan={**plan, "source_sha": draft, "files": ["greet.py"]})
  record("b", id="b", status="merged", number=5,
         plan={**plan, "source_sha": old, "files": ["old.py"]})
  record("c", id="c", status="open", number=9,
         plan={**plan, "source_sha": draft, "files": ["greet.py"]})
  record("d", id="d", status="merged", number=11,
         plan={**plan, "source_sha": "0" * 40, "files": ["x.py"]})
  record("e", id="e", status="merged", number=12,
         plan={"source_repo_path": str(tmp_path / "elsewhere"), "source_sha": draft,
               "files": ["greet.py"]})

  assert backfill.backfill(records, repo, dry_run=True)["written"] == [7]
  assert _git(repo, "for-each-ref", app_git.MERGED_CONTRIBUTION_PREFIX) == ""
  outcome = backfill.backfill(records, repo)

  assert outcome == {"written": [7], "integrated": [5], "unavailable": [11]}
  assert _git(repo, "for-each-ref", "--format=%(refname)",
              app_git.MERGED_CONTRIBUTION_PREFIX) == f"{app_git.MERGED_CONTRIBUTION_PREFIX}/7"
  result = platform_update._final_tree_merge(repo, local, release)
  assert result.status == "clean"
  assert _show(repo, result.merged_tree_oid, "greet.py") == _MERGED
