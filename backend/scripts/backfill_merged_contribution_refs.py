#!/usr/bin/env python3
"""Write merged-contribution witnesses for platform PRs merged before they existed.

Contribute now records ``refs/mobius/merged-contributions/<N>`` when a
platform contribution settles merged, and the next update retires local files
still holding exactly the bytes that PR's reviewed head carried. Contributions
settled earlier have no witness. This one-time, local-only pass reads
Contribute's records and writes one for each merged record of the platform
release repository whose source is this checkout, whose reviewed head is in
this repository, and whose merge the installed release does not carry yet.

The merge commit comes from GitHub, as the live settle path resolves it; the
records' own SHA fields are not trusted. When GitHub cannot answer, an
existing witness for the same contribution (a pre-release version 1 one, or
one an earlier run wrote) supplies the merge commit it stored. A pull request the installed release's first-parent history
names as ``(#N)`` is taken as already integrated without asking GitHub.

Pre-release Contribute builds wrote version 1 witnesses (staging-source tree,
often no merge commit) that current updaters ignore. Each one is rewritten
when its record allows; one whose merge the installed release already
carries is dropped, since it could never retire anything; every other one is
kept. Re-running leaves current witnesses untouched.

  python3 backend/scripts/backfill_merged_contribution_refs.py \\
    --records /data/apps/<contribute app id>/contributions [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

_APP_IMPORT_ROOT = str(Path(__file__).resolve().parent.parent)
if _APP_IMPORT_ROOT not in sys.path:
  sys.path.insert(0, _APP_IMPORT_ROOT)

from app import app_git  # noqa: E402

_PRE_RELEASE_VERSION = 1


def _merged_platform_records(records: Path, source: Path, github_repo: str) -> list[dict]:
  """The newest merged record of each platform pull request staged from ``source``."""
  found: dict[int, dict] = {}
  for path in sorted(records.glob("*.json")):
    try:
      record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
      continue
    plan = record.get("plan") if isinstance(record, dict) else None
    if not isinstance(plan, dict) or record.get("status") != "merged":
      continue
    staged_from = plan.get("source_repo_path")
    slug = str(plan.get("repo") or record.get("repo") or "")
    number = record.get("number")
    if (
      slug.lower() == github_repo.lower()
      and (not staged_from or Path(staged_from).resolve() == source.resolve())
      and isinstance(number, int) and number > 0
    ):
      newest = found.get(number)
      if newest is None or str(record.get("updated_at") or "") >= str(
        newest.get("updated_at") or "",
      ):
        found[number] = record
  return [found[number] for number in sorted(found)]


def _stored_witnesses(repo: Path) -> dict[int, dict]:
  """``{N: metadata}`` for each witness ref, with its ``ref``, any version."""
  listing = app_git._run(
    repo, "for-each-ref", "--format=%(refname)%00%(contents)%1e",
    app_git.MERGED_CONTRIBUTION_PREFIX, check=False,
  )
  found = {}
  for entry in listing.stdout.split("\x1e"):
    ref, _, message = entry.strip("\n").partition("\0")
    try:
      metadata = json.loads(message)
    except ValueError:
      continue
    number = metadata.get("number") if isinstance(metadata, dict) else None
    if (
      isinstance(number, int) and ref == f"{app_git.MERGED_CONTRIBUTION_PREFIX}/{number}"
    ):
      found[number] = {**metadata, "ref": ref}
  return found


def _stored_merge(witness: dict | None, record: dict) -> str | None:
  """The merge commit a witness stored, if the witness is this record's."""
  if not witness:
    return None
  owner = str(witness.get("contribution_id") or "")
  merge = str(witness.get("merge_sha") or "").strip().lower()
  if owner and owner != str(record.get("id") or ""):
    return None
  return merge if app_git._HEX_OID.fullmatch(merge) else None


def backfill(
  records: Path, repo: Path, *, github_repo: str = app_git.PLATFORM_RELEASE_REPO,
  dry_run: bool = False, merge_commit: Callable[[dict], str | None] | None = None,
  source: Path | None = None,
) -> dict[str, list[int]]:
  """Write the witnesses; report pull request numbers by outcome.

  ``merge_commit`` resolves a record's merge commit (default: GitHub).
  ``source`` is the checkout the records name as their source (default
  ``repo``; a copy of the live repository is checked against the live path).
  """
  if merge_commit is None:
    from app import github_contributions

    def merge_commit(record: dict) -> str | None:
      return github_contributions.github_merge_commit(record, repo)

  release = app_git.UPSTREAM_BRANCH
  if not app_git.ref_exists(repo, f"{release}^{{commit}}"):
    raise SystemExit(f"{repo} has no installed release ({release})")
  released_prs = app_git._pr_merge_commits(repo, release)
  stored = _stored_witnesses(repo)

  def in_release(merge: str | None) -> bool:
    return bool(merge) and app_git.ref_is_ancestor(repo, merge, release) is True

  outcome: dict[str, list[int]] = {
    "written": [], "integrated": [], "unavailable": [],
    "pre_release_dropped": [], "pre_release_kept": [],
  }
  for record in _merged_platform_records(records, source or repo, github_repo):
    number = record["number"]
    plan = record["plan"]
    head = str(plan.get("head_sha") or "")
    files = plan.get("files")
    if number in released_prs:
      outcome["integrated"].append(number)
      continue
    if (
      not isinstance(files, list) or not files
      or not app_git.ref_exists(repo, f"{head}^{{commit}}")
    ):
      outcome["unavailable"].append(number)
      continue
    merge = merge_commit(record) or _stored_merge(stored.get(number), record)
    if not merge:
      outcome["unavailable"].append(number)
    elif in_release(merge):
      outcome["integrated"].append(number)
    elif dry_run or app_git.record_merged_contribution(
      repo, number=number, repo_slug=str(plan.get("repo") or record.get("repo")),
      head_sha=head, merge_sha=merge, paths=files,
      contribution_id=str(record.get("id") or ""),
    ):
      outcome["written"].append(number)
    else:
      outcome["unavailable"].append(number)

  for number, witness in sorted(stored.items()):
    if witness.get("version") != _PRE_RELEASE_VERSION or number in outcome["written"]:
      continue  # rewritten in place
    if number in outcome["integrated"] or in_release(
      str(witness.get("merge_sha") or "").strip().lower(),
    ):
      if not dry_run:
        app_git._run(repo, "update-ref", "-d", witness["ref"], check=False)
      outcome["pre_release_dropped"].append(number)
    else:
      outcome["pre_release_kept"].append(number)
  return {key: sorted(set(values)) for key, values in outcome.items()}


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--records", required=True, type=Path)
  parser.add_argument("--repo", default=Path("/data/platform"), type=Path)
  parser.add_argument(
    "--source", type=Path, default=None,
    help="checkout the records were staged from (default: --repo)",
  )
  parser.add_argument("--github-repo", default=app_git.PLATFORM_RELEASE_REPO)
  parser.add_argument("--dry-run", action="store_true")
  args = parser.parse_args(argv)
  outcome = backfill(
    args.records, args.repo, github_repo=args.github_repo, dry_run=args.dry_run,
    source=args.source,
  )
  verb = "would write" if args.dry_run else "wrote"
  print(
    f"{verb} {len(outcome['written'])} witnesses {outcome['written']}; "
    f"{len(outcome['integrated'])} already in the installed release; "
    f"{len(outcome['unavailable'])} without a reviewed head or merge commit; "
    f"pre-release witnesses {'to drop' if args.dry_run else 'dropped'} "
    f"{outcome['pre_release_dropped']}, kept {outcome['pre_release_kept']}"
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
