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


def _witness(
  repo: Path, head: str, merge: str, paths: list[str], *, number: int = 7,
  repo_slug: str = app_git.PLATFORM_RELEASE_REPO,
) -> str | None:
  """Contribute's record that PR ``number``, carrying ``head``, merged as ``merge``."""
  return app_git.record_merged_contribution(
    repo, number=number, repo_slug=repo_slug, head_sha=head, merge_sha=merge,
    paths=paths,
  )


def _pr_head(repo: Path, files: dict[str, str], start: str = "upstream") -> str:
  """The reviewed head a PR carried: its change on the upstream it was staged on."""
  _git(repo, "checkout", "-q", "-B", "pr-head", start)
  head = _commit(repo, files, "Reviewed change")
  _git(repo, "checkout", "-q", "main")
  return head


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
  _commit(repo, {"greet.py": _DRAFT}, _unit_msg("Draft greet", "greet", disposition))
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
  assert _witness(repo, local, _git(repo, "rev-parse", f"{release}^"), ["greet.py"])

  result = platform_update._final_tree_merge(repo, local, release)

  assert result.status == "conflict" and result.conflict_paths == ["greet.py"]
  assert result.reconciliation.retired_paths == ()


def test_a_path_edited_after_it_was_handed_to_the_pr_stays_with_the_resolver(repo):
  draft = _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  local = _commit(repo, {"greet.py": _DRAFT + "# local tweak\n"}, "tweak")
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])
  assert _witness(repo, draft, release, ["greet.py"])

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
  _commit(repo, {"greet.py": _DRAFT}, "Draft greet")  # untagged
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])
  ref = _witness(
    repo, _pr_head(repo, {"greet.py": _DRAFT}), release, ["greet.py", "missing.py"],
  )

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


def test_a_stray_working_edits_commit_keeps_the_owner_choice_across_updates(repo):
  # A working-edits commit left in committed history takes the file from its
  # owner, so the chain files it under the resolution commit, which must still
  # name the owner's choice for later updates.
  _commit(repo, {"greet.py": _BASE + "# owner tweak\n"},
          _unit_msg("Owner tweak", "owner", "owner-choice:local"))
  local = _commit(repo, {"greet.py": _BASE + "# owner tweak\n# edit\n"},
                  _unit_msg("Working edits", app_git.OVERLAY_WORKING_UNIT))
  release = "upstream"
  for n in (2, 3):
    release = _release(repo, [({"notes.md": f"v{n}\n"}, f"upstream {n}")], start=release)
    merged = app_git.merge_refs(repo, local, release).merged_tree_oid
    local = platform_update._net_overlay_commit(repo, release, merged, local)

    units = {u.slug: u for u in app_git.overlay_units(repo, local, release)}
    assert units["owner"].disposition == "owner-choice:local"
    assert units["owner"].touched == ("greet.py",)
  assert _show(repo, local, "greet.py") == _BASE + "# owner tweak\n# edit\n"
  body = _git(repo, "log", "-1", "--format=%B", local)
  assert "Mobius-Overlay-Also: owner=owner-choice:local" in body


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
  assert _witness(repo, draft, merge, ["greet.py"])
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


# --- Merged-contribution witnesses prove the exact bytes the PR carried ------


@pytest.mark.parametrize("merged_as", ["reviewed", "revised"])
def test_an_earlier_local_only_edit_in_the_contributed_file_is_kept(repo, merged_as):
  """The PR carried only its own change; the owner's older tweak to the same
  file was never handed over, so the file is not replaced."""
  start = _commit(repo, {"greet.py": _HEADER + _BASE}, "header")
  _git(repo, "branch", "-f", "upstream", start)
  _commit(repo, {"greet.py": _TWEAKED + _BASE}, "Owner tweak")  # untagged, local-only
  local = _commit(repo, {"greet.py": _TWEAKED + _DRAFT}, "Draft greet")
  head = _pr_head(repo, {"greet.py": _HEADER + _DRAFT})
  body = _DRAFT if merged_as == "reviewed" else _MERGED
  release = _release(repo, [({"greet.py": _HEADER + body}, "Reviewed greeting (#7)")])
  assert _witness(repo, head, release, ["greet.py"])

  result = platform_update._final_tree_merge(repo, local, release)

  assert result.reconciliation.retired_paths == ()
  assert _show(repo, result.local_oid, "greet.py") == _TWEAKED + _DRAFT
  if merged_as == "reviewed":
    assert result.status == "clean"
    assert _show(repo, result.merged_tree_oid, "greet.py") == _TWEAKED + _DRAFT
  else:
    assert result.status == "conflict" and result.conflict_paths == ["greet.py"]


def test_a_merged_contribution_proof_survives_an_unrelated_unit_chain_update(repo):
  draft = _commit(repo, {"greet.py": _DRAFT}, "Draft greet")  # untagged
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  head = _pr_head(repo, {"greet.py": _DRAFT})
  r1 = _release(repo, [({"notes.md": "v2\n"}, "unrelated upstream work")])
  merged = app_git.merge_refs(repo, local, r1).merged_tree_oid
  carried = platform_update._net_overlay_commit(repo, r1, merged, local)
  assert app_git.ref_is_ancestor(repo, local, carried) is False  # history rewritten
  r2 = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")], start=r1)
  assert _witness(repo, head, r2, ["greet.py"])

  result = platform_update._final_tree_merge(repo, carried, r2)

  assert result.status == "clean"
  assert _show(repo, result.merged_tree_oid, "greet.py") == _MERGED
  assert _show(repo, result.merged_tree_oid, "local.py") == "OWNER = True\n"
  assert [r.as_dict() for r in result.reconciliation.retired_paths] == [{
    "path": "greet.py", "pr": 7, "unit": f"c-{draft[:8]}-draft-greet",
    "proof": "merged-contribution",
  }]


@pytest.mark.parametrize("carries", [(), ("chain",)], ids=["direct", "after-chain"])
@pytest.mark.parametrize("earlier", ["local-only", "owner-choice:local"])
def test_a_shared_file_retires_only_when_the_pr_carried_every_local_change(
  repo, earlier, carries,
):
  """Another unit's edit the reviewed head also carried was handed over with
  it, so only an owner's choice to keep the file local stops the retirement."""
  _commit(repo, {"greet.py": _BASE + "# owner tweak\n"},
          _unit_msg("Owner tweak", "owner", earlier))
  local = _commit(repo, {"greet.py": _DRAFT + "# owner tweak\n"},
                  _unit_msg("Draft greet", "greet"))
  head = _pr_head(repo, {"greet.py": _DRAFT + "# owner tweak\n"})
  release = "upstream"
  for carry in carries:
    release = _release(repo, [({"notes.md": "v2\n"}, "unrelated")], start=release)
    merged = app_git.merge_refs(repo, local, release).merged_tree_oid
    local = platform_update._net_overlay_commit(repo, release, merged, local)
  merging = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")], start=release)
  assert _witness(repo, head, merging, ["greet.py"])

  result = platform_update._final_tree_merge(repo, local, merging)

  if earlier == "owner-choice:local":
    assert result.reconciliation.retired_paths == ()
    assert result.status == "conflict"
  else:
    assert [r.proof for r in result.reconciliation.retired_paths] == ["merged-contribution"]
    assert _show(repo, result.merged_tree_oid, "greet.py") == _MERGED


@pytest.mark.parametrize("mismatch", ["other-repo", "other-merge"])
def test_a_witness_retires_only_through_its_own_repo_and_merge_commit(repo, mismatch):
  """A same-numbered PR elsewhere, or a release commit that only claims the
  number in its title, never stands in for the recorded merge."""
  _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  head = _pr_head(repo, {"greet.py": _DRAFT})
  release = _release(repo, [({"greet.py": _MERGED}, "Someone else's greeting (#7)")])
  fork_merge = _pr_head(repo, {"notes.md": "fork\n"})
  witness = _witness(
    repo, head, release if mismatch == "other-repo" else fork_merge, ["greet.py"],
    repo_slug="someone/mobius-fork" if mismatch == "other-repo"
    else app_git.PLATFORM_RELEASE_REPO,
  )
  assert witness

  result = platform_update._final_tree_merge(repo, local, release)

  assert result.reconciliation.retired_paths == ()
  assert result.status == "conflict"
  app_git.retire_landed_equivalent_changes(repo, release)
  remaining = _git(repo, "for-each-ref", "--format=%(refname)", witness)
  assert remaining == ("" if mismatch == "other-repo" else witness)


def test_a_witness_needs_a_reviewed_head_and_a_merge_commit(repo):
  head = _pr_head(repo, {"greet.py": _DRAFT})
  merge = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])
  assert _witness(repo, head, merge, ["greet.py"])
  assert _witness(repo, "0" * 40, merge, ["greet.py"]) is None
  assert _witness(repo, head, "", ["greet.py"]) is None
  assert _witness(repo, head, merge, ["greet.py"], repo_slug="not a repo") is None


# --- Explicit dispositions on legacy squashes survive their expansion -------


@pytest.mark.parametrize("marked", [None, "outer", "inner"])
def test_owner_choice_noted_on_a_legacy_squash_keeps_its_files(repo, marked):
  """A squash's own ``local-only`` trailer is only the default; a disposition
  noted on it (directly, or on an older squash it expanded through) still
  guards its files from retirement and from superseded-draft removal."""
  start = _commit(repo, {"greet.py": _HEADER + _BASE}, "header")
  _git(repo, "branch", "-f", "upstream", start)
  base = _commit(repo, {"greet.py": _TWEAKED + _BASE}, "Owner tweak")
  draft = _commit(repo, {"greet.py": _TWEAKED + _DRAFT},
                  _unit_msg("Draft greet", "greet", "pending-pr:#7"))
  r1 = _release(repo, [({"notes.md": "v2\n"}, "upstream 1")])
  merged = app_git.merge_refs(repo, draft, r1).merged_tree_oid
  inner = platform_update._single_overlay_commit(repo, r1, merged, draft)
  r2 = _release(repo, [({"notes.md": "v3\n"}, "upstream 2")], start=r1)
  merged = app_git.merge_refs(repo, inner, r2).merged_tree_oid
  outer = platform_update._single_overlay_commit(repo, r2, merged, inner)
  _git(repo, "reset", "-q", "--hard", outer)
  merge = _release(
    repo, [({"greet.py": _HEADER + _MERGED}, "Reviewed greeting (#7)")], start=r2,
  )
  assert app_git.mark_draft_superseded(
    repo, contribution_id="rec-7", base_sha=base, head_sha=draft, upstream_sha=merge,
  )
  if marked:
    app_git.mark_overlay_disposition(
      repo, outer if marked == "outer" else inner, "owner-choice:local",
    )

  units = {u.slug: u for u in app_git.overlay_units(repo, outer, merge)}
  result = platform_update._final_tree_merge(repo, outer, merge)

  assert units["greet"].disposition == "pending-pr:#7"
  if marked is None:
    assert app_git.LEGACY_OVERLAY_UNIT not in units
    assert result.status == "clean"
    return
  assert app_git.LEGACY_OVERLAY_UNIT not in units
  guard = units[f"{app_git.LEGACY_OVERLAY_UNIT}-{(outer if marked == 'outer' else inner)[:12]}"]
  assert guard.disposition == "owner-choice:local"
  assert "greet.py" in guard.touched and guard.paths == ()
  assert result.reconciliation.retired_paths == ()
  assert _show(repo, result.local_oid, "greet.py") == _TWEAKED + _DRAFT
  assert result.status == "conflict" and result.conflict_paths == ["greet.py"]
  # The guard is re-emitted with the chain, so later updates still honour it.
  chained = platform_update._net_overlay_commit(
    repo, r2, app_git.merge_refs(repo, outer, r2).merged_tree_oid, outer,
  )
  body = _git(repo, "log", "-1", "--format=%B", chained)
  assert f"Mobius-Overlay-Also: {guard.slug}=owner-choice:local" in body


def _nested_squashes(repo: Path, *, between: dict | None = None) -> dict[str, str]:
  """A ``pending-pr:#7`` draft of greet.py carried through two legacy squashes.

  ``between`` is committed after the inner squash, so only the outer one
  carries it. Returns the draft, both squashes and the release merging #7.
  """
  start = _commit(repo, {"greet.py": _HEADER + _BASE}, "header")
  _git(repo, "branch", "-f", "upstream", start)
  draft = _commit(repo, {"greet.py": _HEADER + _DRAFT},
                  _unit_msg("Draft greet", "greet", "pending-pr:#7"))
  r1 = _release(repo, [({"notes.md": "v2\n"}, "upstream 1")])
  inner = platform_update._single_overlay_commit(
    repo, r1, app_git.merge_refs(repo, draft, r1).merged_tree_oid, draft,
  )
  _git(repo, "reset", "-q", "--hard", inner)
  before_outer = _commit(repo, between, _unit_msg("Late", "late", "pending-pr:#8")) \
    if between else inner
  r2 = _release(repo, [({"notes.md": "v3\n"}, "upstream 2")], start=r1)
  outer = platform_update._single_overlay_commit(
    repo, r2, app_git.merge_refs(repo, before_outer, r2).merged_tree_oid, before_outer,
  )
  _git(repo, "reset", "-q", "--hard", outer)
  merge = _release(
    repo, [({"greet.py": _HEADER + _MERGED}, "Reviewed greeting (#7)")], start=r2,
  )
  return {"start": start, "draft": draft, "inner": inner, "outer": outer,
          "r2": r2, "merge": merge}


def _prove_merged(repo: Path, proof: str, draft: str, merge: str, base: str) -> None:
  """Give the updater one proof that greet.py's local bytes went into #7."""
  if proof == "witness":
    assert _witness(repo, draft, merge, ["greet.py"])
  elif proof == "draft":
    assert app_git.mark_draft_superseded(
      repo, contribution_id="rec-7", base_sha=base, head_sha=draft, upstream_sha=merge,
    )


@pytest.mark.parametrize("proof", ["pending-pr", "witness", "draft"])
@pytest.mark.parametrize("outer_note, inner_note", [
  (None, None),
  ("owner-choice:local", "pending-pr:#7"),
  ("owner-choice:local", "local-only"),
  ("pending-pr:#7", "owner-choice:local"),
  ("local-only", "owner-choice:local"),
])
def test_an_owner_choice_on_any_nested_squash_outranks_other_squash_notes(
  repo, proof, outer_note, inner_note,
):
  """Nested legacy squashes share one slug; each noted squash still keeps its
  own claim, so no older or newer note on another squash undoes an owner's
  choice to keep the file, under any of the three proofs."""
  at = _nested_squashes(repo)
  if proof != "pending-pr":
    # Only the named proof applies: the draft's unit is no longer pending.
    app_git.mark_overlay_disposition(repo, at["draft"], "local-only")
  _prove_merged(repo, proof, at["draft"], at["merge"], at["start"])
  for squash, note in (("outer", outer_note), ("inner", inner_note)):
    if note:
      app_git.mark_overlay_disposition(repo, at[squash], note)

  units = {u.slug: u for u in app_git.overlay_units(repo, at["outer"], at["merge"])}
  result = platform_update._final_tree_merge(repo, at["outer"], at["merge"])

  if outer_note is None:
    assert result.status == "clean"
    assert _show(repo, result.merged_tree_oid, "greet.py") == _HEADER + _MERGED
    return
  guards = {
    note: units[f"{app_git.LEGACY_OVERLAY_UNIT}-{at[squash][:12]}"].disposition
    for squash, note in (("outer", outer_note), ("inner", inner_note))
  }
  assert guards == {outer_note: outer_note, inner_note: inner_note}
  assert result.reconciliation.retired_paths == ()
  assert _show(repo, result.local_oid, "greet.py") == _HEADER + _DRAFT
  assert result.status == "conflict" and result.conflict_paths == ["greet.py"]


def test_each_noted_squash_guards_only_the_paths_it_carried(repo):
  """An owner's choice noted on the inner squash keeps greet.py; it does not
  stretch to late.py, which only the outer squash carried."""
  at = _nested_squashes(repo, between={"late.py": "L = 1\n"})
  app_git.mark_overlay_disposition(repo, at["inner"], "owner-choice:local")
  app_git.mark_overlay_disposition(repo, at["outer"], "local-only")
  late_head = _pr_head(repo, {"late.py": "L = 1\n"}, start=at["r2"])
  release = _release(
    repo, [({"late.py": "L = 2\n"}, "Reviewed late (#8)")], start=at["merge"],
  )
  assert _witness(repo, late_head, release, ["late.py"], number=8)

  units = {u.slug: u for u in app_git.overlay_units(repo, at["outer"], release)}
  result = platform_update._final_tree_merge(repo, at["outer"], release)

  inner = units[f"{app_git.LEGACY_OVERLAY_UNIT}-{at['inner'][:12]}"]
  outer = units[f"{app_git.LEGACY_OVERLAY_UNIT}-{at['outer'][:12]}"]
  assert (inner.disposition, inner.touched) == ("owner-choice:local", ("greet.py",))
  assert (outer.disposition, outer.touched) == ("local-only", ("greet.py", "late.py"))
  assert [(r.path, r.pr) for r in result.reconciliation.retired_paths] == [("late.py", 8)]
  assert _show(repo, result.local_oid, "greet.py") == _HEADER + _DRAFT
  assert result.status == "conflict" and result.conflict_paths == ["greet.py"]


def test_a_shared_slug_recorded_by_an_older_chain_keeps_an_owner_choice(repo):
  """Chains written before guards had their own slugs may record two
  dispositions for ``platform-local-tree``; the owner's choice wins."""
  _commit(repo, {"greet.py": _DRAFT}, app_git.overlay_message(
    "Draft greet", unit="greet", disposition="pending-pr:#7",
    also=[(app_git.LEGACY_OVERLAY_UNIT, "pending-pr:#7"),
          (app_git.LEGACY_OVERLAY_UNIT, "owner-choice:local")],
  ))
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])

  units = {u.slug: u for u in app_git.overlay_units(repo, local, release)}
  result = platform_update._final_tree_merge(repo, local, release)

  assert units[app_git.LEGACY_OVERLAY_UNIT].disposition == "owner-choice:local"
  assert result.reconciliation.retired_paths == ()
  assert result.status == "conflict"


# --- A merge the same update reverts leaves the local change alone ----------


@pytest.mark.parametrize("proof", ["pending-pr", "witness", "draft"])
def test_a_pr_merged_and_reverted_in_one_update_keeps_the_local_change(repo, proof):
  draft = _commit(repo, {"greet.py": _DRAFT}, _unit_msg(
    "Draft greet", "greet", "pending-pr:#7" if proof == "pending-pr" else "local-only",
  ))
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  merge = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])
  release = _release(repo, [
    ({"greet.py": _BASE}, 'Revert "Reviewed greeting (#7)" (#9)'),
    ({"notes.md": "v2\n"}, "later upstream work"),
  ], start=merge)
  _prove_merged(repo, proof, draft, merge, _git(repo, "rev-parse", "upstream"))

  result = platform_update._final_tree_merge(repo, local, release)

  assert result.reconciliation.retired_paths == ()
  assert result.local_oid == local
  assert result.status == "clean"
  assert _show(repo, result.merged_tree_oid, "greet.py") == _DRAFT


@pytest.mark.parametrize("reverted", [False, True])
def test_a_file_reviewers_dropped_from_the_pr_does_not_keep_its_draft(repo, reverted):
  """The draft changed greet.py and notes.md; the merged PR kept only
  greet.py. Only the paths the merging commit changed are judged for a
  revert, so the draft is still set aside, unless the release reverts
  greet.py itself."""
  start = _commit(repo, {"greet.py": _HEADER + _BASE}, "header")
  _git(repo, "branch", "-f", "upstream", start)
  draft = _commit(
    repo, {"greet.py": _HEADER + _DRAFT, "notes.md": "v1 draft\n"}, "Draft greet",
  )
  merge = _release(repo, [({"greet.py": _HEADER + _MERGED}, "Reviewed greeting (#7)")])
  release = merge if not reverted else _release(repo, [
    ({"greet.py": _HEADER + _BASE}, 'Revert "Reviewed greeting (#7)" (#9)'),
  ], start=merge)
  assert app_git.mark_draft_superseded(
    repo, contribution_id="rec-7", base_sha=start, head_sha=draft, upstream_sha=merge,
  )

  result = platform_update._final_tree_merge(repo, draft, release)

  assert result.status == "clean"
  assert (result.local_oid == draft) is reverted
  if reverted:
    assert _show(repo, result.merged_tree_oid, "greet.py") == _HEADER + _DRAFT
    assert _show(repo, result.merged_tree_oid, "notes.md") == "v1 draft\n"
  else:
    assert _show(repo, result.merged_tree_oid, "greet.py") == _HEADER + _MERGED
    assert _show(repo, result.merged_tree_oid, "notes.md") == "v1\n"


# --- Labels come only from the trailer block ---------------------------------


def test_a_body_line_quoting_a_disposition_labels_nothing(repo):
  _commit(repo, {"greet.py": _DRAFT}, (
    "Draft greet\n\nA reviewer suggested\nMobius-Disposition: pending-pr:#7\n"
    "but this stays local.\n"
  ))
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])

  units = app_git.overlay_units(repo, local, release)
  result = platform_update._final_tree_merge(repo, local, release)

  assert {u.disposition for u in units} == {"local-only"}
  assert result.reconciliation.retired_paths == ()
  assert result.status == "conflict"


def test_superseded_drafts_are_still_set_aside_when_unit_labelling_fails(
  repo, monkeypatch,
):
  start = _commit(repo, {"greet.py": _HEADER + _BASE}, "header")
  _git(repo, "branch", "-f", "upstream", start)
  draft = _commit(repo, {"greet.py": _HEADER + _DRAFT}, "Draft greet")
  merge = _release(repo, [({"greet.py": _HEADER + _MERGED}, "Reviewed greeting")])
  assert app_git.mark_draft_superseded(
    repo, contribution_id="rec-7", base_sha=start, head_sha=draft, upstream_sha=merge,
  )

  def unlabelled(*_args, **_kwargs):
    raise RuntimeError("labelling failed")

  monkeypatch.setattr(app_git, "overlay_units", unlabelled)
  result = platform_update._final_tree_merge(repo, draft, merge)

  assert result.local_oid != draft
  assert result.status == "clean"
  assert _show(repo, result.merged_tree_oid, "greet.py") == _HEADER + _MERGED


# --- Witness formats -----------------------------------------------------------


def _legacy_witness(
  repo: Path, source: str, number: int = 7, merge: str | None = None,
) -> str:
  """A version 1 witness as pre-release Contribute builds wrote it."""
  stored = f'"{merge}"' if merge else "null"
  metadata = (
    f'{{"contribution_id":"rec-{number}","merge_sha":{stored},"number":{number},'
    f'"paths":["greet.py"],"source_sha":"{source}","version":1}}'
  )
  anchor = _git(repo, "commit-tree", f"{source}^{{tree}}", "-p", source, "-m", metadata)
  ref = f"{app_git.MERGED_CONTRIBUTION_PREFIX}/{number}"
  _git(repo, "update-ref", ref, anchor)
  return ref


def test_a_pre_release_witness_retires_nothing_and_is_left_for_the_backfill(repo):
  draft = _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  local = _commit(repo, {"local.py": "OWNER = True\n"}, "unrelated local work")
  release = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])
  ref = _legacy_witness(repo, draft, merge=release)

  result = platform_update._final_tree_merge(repo, local, release)
  app_git.retire_landed_equivalent_changes(repo, release)

  assert result.reconciliation.retired_paths == ()
  assert _git(repo, "for-each-ref", "--format=%(refname)", ref) == ref


def test_recording_the_same_witness_again_keeps_the_overlay_evidence(repo):
  head = _pr_head(repo, {"greet.py": _DRAFT})
  merge = _release(repo, [({"greet.py": _MERGED}, "Reviewed greeting (#7)")])
  ref = _witness(repo, head, merge, ["greet.py"])
  first = _git(repo, "rev-parse", ref)
  evidence = app_git.overlay_evidence_fingerprint(repo)

  assert _witness(repo, head, merge, ["greet.py"]) == ref
  assert _git(repo, "rev-parse", ref) == first
  assert app_git.overlay_evidence_fingerprint(repo) == evidence


# --- Retired copies stay pinned, newest few only -------------------------------


def test_only_the_most_recently_retired_pins_are_kept(repo):
  """Retention follows retirement order, not the served commits' dates: each
  update here retires an older commit than the one before (a rollback)."""
  served = []
  for n in range(5):
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    served.append(subprocess.run(
      ["git", "-C", str(repo), "commit-tree", tree, "-m", f"served {n}"],
      capture_output=True, text=True, check=True,
      env={**app_git._git_env(repo), "GIT_AUTHOR_NAME": "T",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "T",
           "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_DATE": f"2026-01-0{5 - n}T00:00:00Z"},
    ).stdout.strip())
  stamped = f"{platform_update._SET_ASIDE_PREFIX}/20260101T000000000000Z"
  _git(repo, "update-ref", stamped, served[0])

  for n, commit in enumerate(served):
    platform_update._pin_retired_local_copies(repo, f"{n:08d}" + "f" * 32, commit)

  pins = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)",
              platform_update._SET_ASIDE_PREFIX).splitlines()
  kept = {pin.split()[1] for pin in pins if "/retired-" in pin}
  assert kept == {served[n] for n in (2, 3, 4)}
  assert f"{stamped} {served[0]}" in pins
  assert all(
    pin.split()[0].endswith(f"-{n:08d}") for pin in pins for n in range(5)
    if pin.split()[1] == served[n] and "/retired-" in pin
  )


# --- One planner: status counts what Apply sets aside -----------------------


@pytest.mark.parametrize("owner_choice", [False, True])
def test_status_counts_superseded_drafts_the_way_apply_sets_them_aside(repo, owner_choice):
  start = _commit(repo, {"greet.py": _HEADER + _BASE}, "header")
  _git(repo, "branch", "-f", "upstream", start)
  tweak = _commit(repo, {"greet.py": _TWEAKED + _BASE}, "Owner tweak")
  draft = _commit(repo, {"greet.py": _TWEAKED + _DRAFT}, "Draft greet")
  merge = _release(repo, [({"greet.py": _HEADER + _MERGED}, "Reviewed greeting")])
  assert app_git.mark_draft_superseded(
    repo, contribution_id="rec-7", base_sha=tweak, head_sha=draft, upstream_sha=merge,
  )
  if owner_choice:
    app_git.mark_overlay_disposition(repo, tweak, "owner-choice:local")

  def objects():
    return _git(repo, "cat-file", "--batch-all-objects", "--batch-check")

  before = objects()
  summary = platform_update._compute_overlay_summary(
    repo, draft, _git(repo, "rev-parse", "upstream"), merge,
  )
  assert objects() == before  # status is a read: the planner's commits are discarded
  applied = platform_update._final_tree_merge(repo, draft, merge)

  assert summary["retirable"] == {"files": 0 if owner_choice else 1, "prs": []}
  assert (applied.local_oid == draft) is owner_choice


def test_overlay_evidence_binds_the_reviewed_update_plan(repo):
  local = _commit(repo, {"greet.py": _DRAFT}, "Draft greet")
  target = _release(repo, [({"notes.md": "v2\n"}, "upstream")])
  assert app_git.overlay_evidence_fingerprint(repo) == ""
  reviewed = platform_update._update_plan_id(local, target)

  app_git.mark_overlay_disposition(repo, local, "owner-choice:local")

  evidence = app_git.overlay_evidence_fingerprint(repo)
  assert evidence
  assert platform_update._update_plan_id(local, target, evidence=evidence) != reviewed
  with pytest.raises(platform_update.PlatformUpdateError, match="update_plan_invalid"):
    platform_update._validate_update_plan(
      repo, plan_id=reviewed, current_sha=local, target_sha=target,
    )
