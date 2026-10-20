"""Local platform work keeps its unit identity, and merged units retire."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import app_git, platform_update
from scripts import platform_overlay

_BASE = "def greet(name):\n  return 'hi ' + name\n"
_DRAFT = "def greet(name):\n  return 'hello ' + name\n"
_MERGED = "def greet(name, *, punct='!'):\n  return 'hello, ' + name + punct\n"


def _git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
     "-C", str(repo), *args],
    capture_output=True, text=True, check=True, env=app_git._git_env(repo),
  ).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], msg: str) -> str:
  for name, text in files.items():
    path = repo / name
    if text is None:
      path.unlink()
    else:
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(text)
  _git(repo, "add", "-A")
  _git(repo, "commit", "-q", "-m", msg)
  return _git(repo, "rev-parse", "HEAD")


def _unit_msg(subject: str, unit: str, disposition: str = "local-only") -> str:
  return app_git.overlay_message(subject, unit=unit, disposition=disposition)


def _show(repo: Path, rev: str, path: str) -> str:
  return _git(repo, "show", f"{rev}:{path}") + "\n"


@pytest.fixture
def repo(tmp_path):
  repo = tmp_path / "platform"
  repo.mkdir()
  _git(repo, "init", "-q", "-b", "main")
  base = _commit(repo, {"greet.py": _BASE, "notes.md": "v1\n"}, "base")
  _git(repo, "branch", "upstream", base)
  return repo


def _release(repo: Path, commits: list[tuple[dict, str]], start: str = "upstream") -> str:
  """Commit ``commits`` on a release branch from ``start``; main stays checked out."""
  _git(repo, "checkout", "-q", "-B", "release", start)
  tip = ""
  for files, msg in commits:
    tip = _commit(repo, files, msg)
  _git(repo, "checkout", "-q", "main")
  return tip


def _three_units(repo: Path) -> tuple[str, ...]:
  """Unit ``alpha`` (two commits), an untagged commit, and the PR draft ``greet``."""
  a1 = _commit(repo, {"alpha.py": "A = 1\n"}, _unit_msg("Add alpha", "alpha"))
  loose = _commit(repo, {"loose.txt": "x\n"}, "Loose local note")
  draft = _commit(
    repo, {"greet.py": _DRAFT}, _unit_msg("Draft greet", "greet", "pending-pr:#7"),
  )
  a2 = _commit(repo, {"alpha.py": "A = 2\n", "shared.txt": "a\n"},
               _unit_msg("Alpha follow-up", "alpha"))
  return a1, loose, draft, a2


def test_overlay_units_group_commits_by_trailer_and_own_paths_by_last_touch(repo):
  a1, loose, draft, a2 = _three_units(repo)
  _commit(repo, {"shared.txt": "b\n"}, "Later shared edit")

  units = {u.slug: u for u in app_git.overlay_units(repo, "main", "upstream")}

  assert units["alpha"].commits == (a1, a2)
  assert units["alpha"].subject == "Add alpha"
  assert units["alpha"].paths == ("alpha.py",)  # a later unit took shared.txt
  assert units["greet"].disposition == "pending-pr:#7" and units["greet"].pr == 7
  assert units[f"c-{loose[:8]}-loose-local-note"].paths == ("loose.txt",)
  later = next(u for slug, u in units.items() if slug.endswith("later-shared-edit"))
  assert later.paths == ("shared.txt",)


def test_note_disposition_overrides_the_commit_trailer(repo):
  _a1, _loose, draft, _a2 = _three_units(repo)
  assert platform_overlay.main(
    ["--repo", str(repo), "mark", draft, "owner-choice:local"],
  ) == 0
  assert platform_overlay.main(["--repo", str(repo), "mark", draft, "wip"]) == 2

  units = {u.slug: u for u in app_git.overlay_units(repo, "main", "upstream")}
  assert units["greet"].disposition == "owner-choice:local"
  assert units["greet"].pr is None


def test_unit_chain_reproduces_the_merged_tree_and_keeps_trailers(repo):
  _three_units(repo)
  release = _release(repo, [({"notes.md": "v2\n"}, "later upstream work")])
  merged = app_git.merge_refs(repo, "main", release).merged_tree_oid

  tip = platform_update._net_overlay_commit(repo, release, merged, _git(repo, "rev-parse", "main"))

  assert _git(repo, "rev-parse", f"{tip}^{{tree}}") == merged
  subjects = _git(repo, "log", "--reverse", "--format=%s", f"{release}..{tip}").splitlines()
  assert subjects == ["Add alpha", "Loose local note", "Draft greet"]
  body = _git(repo, "log", "-1", "--format=%B", f"{tip}")
  assert "Mobius-Overlay-Unit: greet" in body
  assert "Mobius-Disposition: pending-pr:#7" in body
  # The re-emitted chain keeps its identity through the next update.
  again = [u.slug for u in app_git.overlay_units(repo, tip, release)]
  assert again[0] == "alpha" and again[-1] == "greet"


def test_unowned_resolution_paths_go_into_the_resolution_unit(repo):
  _commit(repo, {"alpha.py": "A = 1\n"}, _unit_msg("Add alpha", "alpha"))
  release = _release(repo, [({"notes.md": "v2\n"}, "upstream")])
  merged = app_git.merge_refs(repo, "main", release).merged_tree_oid
  with_answer = _git(repo, "commit-tree", merged, "-m", "x")
  _git(repo, "checkout", "-q", "-B", "answer", with_answer)
  answered = _commit(repo, {"resolver.txt": "added while resolving\n"}, "answer")
  _git(repo, "checkout", "-q", "main")
  tree = _git(repo, "rev-parse", f"{answered}^{{tree}}")

  tip = platform_update._net_overlay_commit(repo, release, tree, _git(repo, "rev-parse", "main"))

  assert _git(repo, "rev-parse", f"{tip}^{{tree}}") == tree
  assert _git(repo, "log", "--reverse", "--format=%s", f"{release}..{tip}").splitlines() == [
    "Add alpha", "Reconcile local platform source with reviewed upstream",
  ]
  assert f"Mobius-Overlay-Unit: update-resolution-{release[:8]}" in _git(
    repo, "log", "-1", "--format=%B", tip,
  )


def test_a_chain_that_would_differ_from_the_merged_tree_falls_back_to_one_commit(
  repo, monkeypatch,
):
  _three_units(repo)
  release = _release(repo, [({"notes.md": "v2\n"}, "upstream")])
  merged = app_git.merge_refs(repo, "main", release).merged_tree_oid
  real = app_git._tree_changes

  def lose_a_path(*args):
    changes = real(*args)
    changes.pop("loose.txt")
    return changes

  monkeypatch.setattr(app_git, "_tree_changes", lose_a_path)
  with pytest.raises(RuntimeError, match="does not reproduce"):
    app_git.unit_chain(
      repo, release, merged, app_git.overlay_units(repo, "main", release),
      resolution=app_git.OverlayUnit("r", "local-only", None, "r", "", (), ()),
    )

  tip = platform_update._net_overlay_commit(repo, release, merged, _git(repo, "rev-parse", "main"))
  assert _git(repo, "rev-parse", f"{tip}^{{tree}}") == merged
  assert _git(repo, "rev-parse", f"{tip}^") == release
  assert "Mobius-Overlay-Unit: platform-local-tree" in _git(repo, "log", "-1", "--format=%B", tip)


def test_legacy_squashes_expand_through_their_previous_sources(repo):
  """Two generations of old-updater squashes regain the units they replaced."""
  _three_units(repo)
  first_local = _git(repo, "rev-parse", "main")
  r1 = _release(repo, [({"notes.md": "v2\n"}, "upstream 1")])
  merged = app_git.merge_refs(repo, "main", r1).merged_tree_oid
  squash1 = platform_update._single_overlay_commit(repo, r1, merged, first_local)
  _git(repo, "reset", "-q", "--hard", squash1)
  late = _commit(repo, {"late.py": "L = 1\n"}, _unit_msg("Late edit", "late"))
  r2 = _release(repo, [({"notes.md": "v3\n"}, "upstream 2")], start=r1)
  merged = app_git.merge_refs(repo, "main", r2).merged_tree_oid
  squash2 = platform_update._single_overlay_commit(repo, r2, merged, late)
  _git(repo, "reset", "-q", "--hard", squash2)

  units = {u.slug: u for u in app_git.overlay_units(repo, "main", r2)}

  assert set(units) >= {"alpha", "greet", "late"}
  assert app_git.LEGACY_OVERLAY_UNIT not in units
  assert units["alpha"].paths == ("alpha.py", "shared.txt")
  assert units["late"].paths == ("late.py",)


def _pr_draft(repo: Path, disposition: str = "pending-pr:#7") -> tuple[str, str]:
  draft = _commit(repo, {"greet.py": _DRAFT}, _unit_msg("Draft greet", "greet", disposition))
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  release = _release(repo, [
    ({"greet.py": _MERGED}, "Reviewed greeting (#7)"),
    ({"notes.md": "v2\n"}, "later upstream work"),
  ])
  return local, release


def test_retires_the_unchanged_local_copy_of_a_pr_the_release_merged(repo):
  local, release = _pr_draft(repo)
  assert app_git.merge_refs(repo, local, release).status == "conflict"

  result = platform_update._final_tree_merge(repo, local, release)

  assert result.status == "clean"
  assert _show(repo, result.merged_tree_oid, "greet.py") == _MERGED
  assert _show(repo, result.merged_tree_oid, "local.py") == "OWNER = True\n"
  assert [r.as_dict() for r in result.reconciliation.retired_paths] == [
    {"path": "greet.py", "pr": 7, "unit": "greet", "proof": "pending-pr"},
  ]
  assert _git(repo, "rev-parse", f"{result.local_oid}^") == local


def test_owner_choice_local_is_never_retired_even_when_its_pr_merged(repo):
  local, release = _pr_draft(repo, "owner-choice:local")
  app_git.record_merged_contribution(
    repo, number=7, source_sha=local, paths=["greet.py"],
  )

  result = platform_update._final_tree_merge(repo, local, release)

  assert result.status == "conflict" and result.conflict_paths == ["greet.py"]
  assert result.reconciliation.retired_paths == ()


def test_a_path_edited_after_it_was_handed_to_the_pr_stays_with_the_resolver(repo):
  draft = _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  app_git.record_merged_contribution(repo, number=7, source_sha=draft, paths=["greet.py"])
  local = _commit(repo, {"greet.py": _DRAFT + "# local tweak\n"}, "tweak")
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])

  result = platform_update._final_tree_merge(repo, local, release)

  assert result.status == "conflict"
  assert result.reconciliation.retired_paths == ()


def test_a_witness_that_did_not_change_the_path_retires_nothing(repo):
  local, _release_tip = _pr_draft(repo)
  release = _release(repo, [({"notes.md": "v2\n"}, "Unrelated fix (#7)"),
                            ({"greet.py": _MERGED}, "Someone else's greeting (#8)")])

  result = platform_update._final_tree_merge(repo, local, release)

  assert result.status == "conflict"
  assert result.reconciliation.retired_paths == ()


def test_a_pr_already_in_the_shared_base_is_not_retired_again(repo):
  local, release = _pr_draft(repo)
  # An earlier update integrated #7, but its resolver kept the draft.
  _git(repo, "checkout", "-q", "-B", "kept", release)
  kept = _commit(repo, {"greet.py": _DRAFT, "local.py": "OWNER = True\n"},
                 _unit_msg("Keep draft", "greet", "pending-pr:#7"))
  newer = _release(repo, [({"notes.md": "v3\n"}, "newer")], start=release)
  _git(repo, "checkout", "-q", "kept")

  tip, retired = app_git.without_retired_units(
    repo, kept, newer, app_git.overlay_units(repo, kept, newer),
  )
  assert (tip, retired) == (kept, ())


def test_merged_contribution_witness_retires_captured_bytes_until_integrated(repo):
  draft = _commit(repo, {"greet.py": _DRAFT}, "Draft greet")  # untagged
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  ref = app_git.record_merged_contribution(
    repo, number=7, source_sha=draft, paths=["greet.py", "missing.py"],
  )
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])

  result = platform_update._final_tree_merge(repo, local, release)
  assert result.status == "clean"
  assert _show(repo, result.merged_tree_oid, "greet.py") == _MERGED
  assert result.reconciliation.retired_paths[0].proof == "merged-contribution"

  app_git.retire_landed_equivalent_changes(repo, "upstream")
  assert _git(repo, "for-each-ref", ref)  # not integrated yet
  app_git.retire_landed_equivalent_changes(repo, release)
  assert _git(repo, "for-each-ref", ref) == ""


@pytest.mark.parametrize("earlier", ["owner-choice:local", "local-only"])
def test_a_file_another_unit_also_changed_is_not_retired_with_the_pr(repo, earlier):
  _commit(repo, {"greet.py": _BASE + "# owner tweak\n"},
          _unit_msg("Owner tweak", "owner", earlier))
  local = _commit(repo, {"greet.py": _DRAFT + "# owner tweak\n"},
                  _unit_msg("Draft greet", "greet", "pending-pr:#7"))
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])

  units = {u.slug: u for u in app_git.overlay_units(repo, local, release)}
  assert units["owner"].paths == () and units["owner"].touched == ("greet.py",)
  assert units["greet"].paths == ("greet.py",)

  tip, retired = app_git.without_retired_units(
    repo, local, release, units.values(),
  )
  assert (tip, retired) == (local, ())


def test_a_file_only_merged_prs_changed_is_retired(repo):
  _commit(repo, {"greet.py": _BASE + "# extra\n"},
          _unit_msg("Extra", "extra", "pending-pr:#5"))
  local = _commit(repo, {"greet.py": _DRAFT + "# extra\n"},
                  _unit_msg("Draft greet", "greet", "pending-pr:#7"))
  release = _release(repo, [
    ({"greet.py": _BASE + "# extra\n"}, "Extra (#5)"),
    ({"greet.py": _MERGED}, "Reviewed greeting (#7)"),
  ])

  _tip, retired = app_git.without_retired_units(
    repo, local, release, app_git.overlay_units(repo, local, release),
  )
  assert [r.path for r in retired] == ["greet.py"]


def _merge_pr_branch(repo: Path, msg: str) -> str:
  _git(repo, "checkout", "-q", "-B", "pr-branch", "upstream")
  _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  _git(repo, "checkout", "-q", "main")
  _git(repo, "merge", "-q", "--no-ff", "-m", msg, "pr-branch")
  return _git(repo, "rev-parse", "HEAD")


def test_a_labelled_integration_merge_is_one_unit_and_can_retire(repo):
  merge = _merge_pr_branch(
    repo, _unit_msg("Integrate PR 7", "pr-7", "pending-pr:#7"),
  )
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])

  units = app_git.overlay_units(repo, merge, release)
  assert [(u.slug, u.commits, u.paths) for u in units] == [
    ("pr-7", (merge,), ("greet.py",)),
  ]
  _tip, retired = app_git.without_retired_units(repo, merge, release, units)
  assert [r.as_dict() for r in retired] == [
    {"path": "greet.py", "pr": 7, "unit": "pr-7", "proof": "pending-pr"},
  ]

  local = _commit(repo, {"greet.py": _DRAFT + "# local\n"}, "Local greet tweak")
  units = {u.slug: u for u in app_git.overlay_units(repo, local, release)}
  assert set(units) == {"pr-7", f"c-{local[:8]}-local-greet-tweak"}
  assert units["pr-7"].touched == ("greet.py",) and units["pr-7"].paths == ()
  result = platform_update._final_tree_merge(repo, local, release)
  assert result.reconciliation.retired_paths == ()
  assert result.status == "conflict"


def test_an_unlabelled_merge_attributes_the_commits_it_imported(repo):
  merge = _merge_pr_branch(repo, "Merge branch 'pr-branch'")

  units = app_git.overlay_units(repo, merge, "upstream")
  assert [(u.subject, u.paths) for u in units] == [("Draft greet", ("greet.py",))]
  assert units[0].commits == (_git(repo, "rev-parse", f"{merge}^2"),)



def _shared_file_carried_by_updates(
  repo: Path, earlier: str, carries: tuple[str, ...],
) -> tuple[str, str]:
  """``owner`` and the ``greet`` PR draft shape one file; updates carry them.

  Each update brings an unrelated release, onto which the local delta is
  re-emitted as a unit chain (``chain``) or, as older updaters did, one
  legacy squash (``squash``). The release after them merges PR #7. Returns
  the carried local tip and that release.
  """
  _commit(repo, {"greet.py": _BASE + "# owner tweak\n"},
          _unit_msg("Owner tweak", "owner", earlier))
  local = _commit(repo, {"greet.py": _DRAFT + "# owner tweak\n"},
                  _unit_msg("Draft greet", "greet", "pending-pr:#7"))
  release = "upstream"
  for n, carry in enumerate(carries, start=2):
    release = _release(repo, [({"notes.md": f"v{n}\n"}, f"upstream {n}")], start=release)
    merged = app_git.merge_refs(repo, local, release).merged_tree_oid
    commit = {
      "chain": platform_update._net_overlay_commit,
      "squash": platform_update._single_overlay_commit,
    }[carry]
    local = commit(repo, release, merged, local)
  _git(repo, "reset", "-q", "--hard", local)
  merging = _release(
    repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")], start=release,
  )
  return local, merging


@pytest.mark.parametrize("carries", [
  ("chain",), ("chain", "chain"), ("squash",), ("squash", "chain"),
], ids="-then-".join)
@pytest.mark.parametrize("earlier", ["owner-choice:local", "local-only"])
def test_a_co_contributor_still_guards_the_file_after_updates_carry_it(
  repo, earlier, carries,
):
  carried, release = _shared_file_carried_by_updates(repo, earlier, carries)

  units = {u.slug: u for u in app_git.overlay_units(repo, carried, release)}
  assert units["greet"].paths == ("greet.py",)
  assert units["owner"].touched == ("greet.py",)
  assert units["owner"].disposition == earlier
  if carries[-1] == "chain":
    body = _git(repo, "log", "-1", "--format=%B", carried)
    assert f"Mobius-Overlay-Also: owner={earlier}" in body

  result = platform_update._final_tree_merge(repo, carried, release)
  assert result.reconciliation.retired_paths == ()
  assert result.status == "conflict" and result.conflict_paths == ["greet.py"]


def test_only_merged_pr_contributors_still_retire_after_a_unit_chain(repo):
  _commit(repo, {"greet.py": _BASE + "# extra\n"},
          _unit_msg("Extra", "extra", "pending-pr:#5"))
  local = _commit(repo, {"greet.py": _DRAFT + "# extra\n"},
                  _unit_msg("Draft greet", "greet", "pending-pr:#7"))
  r1 = _release(repo, [({"notes.md": "v2\n"}, "unrelated upstream work")])
  merged = app_git.merge_refs(repo, local, r1).merged_tree_oid
  carried = platform_update._net_overlay_commit(repo, r1, merged, local)
  r2 = _release(repo, [
    ({"greet.py": _BASE + "# extra\n"}, "Extra (#5)"),
    ({"greet.py": _MERGED}, "Reviewed greeting (#7)"),
  ], start=r1)

  _tip, retired = app_git.without_retired_units(
    repo, carried, r2, app_git.overlay_units(repo, carried, r2),
  )
  assert [r.path for r in retired] == ["greet.py"]


_HEADER = "# greeting helpers\n# kept as is\n# kept as is\n"
_TWEAKED = "# greeting helpers, owner tweak\n# kept as is\n# kept as is\n"


@pytest.mark.parametrize("owner_choice", ["draft", "co-contributor", None])
def test_owner_choice_keeps_a_file_both_witness_types_would_replace(repo, owner_choice):
  """A superseded-draft witness and a merged-contribution witness for one
  draft never override an owner's choice to keep a file it touched."""
  start = _commit(repo, {"greet.py": _HEADER + _BASE}, "header")
  _git(repo, "branch", "-f", "upstream", start)
  tweak = _commit(repo, {"greet.py": _TWEAKED + _BASE}, "Owner tweak")
  draft = _commit(repo, {"greet.py": _TWEAKED + _DRAFT}, "Draft greet")
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  merge = _release(repo, [({"greet.py": _HEADER + _MERGED}, "Reviewed greeting (#7)")])
  release = _release(repo, [({"notes.md": "v2\n"}, "later upstream work")], start=merge)
  assert app_git.mark_draft_superseded(
    repo, contribution_id="rec-7", base_sha=tweak, head_sha=draft, upstream_sha=merge,
  )
  assert app_git.record_merged_contribution(
    repo, number=7, source_sha=draft, paths=["greet.py"],
  )
  if owner_choice:
    marked = draft if owner_choice == "draft" else tweak
    platform_overlay.main(["--repo", str(repo), "mark", marked, "owner-choice:local"])

  result = platform_update._final_tree_merge(repo, local, release)

  if owner_choice is None:
    assert result.status == "clean"
    assert _show(repo, result.merged_tree_oid, "greet.py") == _TWEAKED + _MERGED
    return
  assert result.reconciliation.retired_paths == ()
  assert _show(repo, result.local_oid, "greet.py") == _TWEAKED + _DRAFT
  assert result.status == "conflict" and result.conflict_paths == ["greet.py"]
