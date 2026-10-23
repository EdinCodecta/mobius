#!/usr/bin/env python3
"""Write merged-contribution witnesses for platform PRs merged before they existed.

Contribute now records ``refs/mobius/merged-contributions/<N>`` when a
platform contribution settles merged, and the next update retires local files
still holding exactly the bytes that PR's reviewed head carried. Contributions
settled earlier have no witness. This one-time, local-only pass reads
Contribute's records and writes one for each merged record of the platform
release repository whose source is this checkout, whose merge commit is
recorded, and which the installed release does not carry yet. It never
matches a pull request by number alone. It is idempotent and offline.

Run it again after an update that loads the format-2 witnesses: it rewrites
each format-1 witness an earlier Contribute wrote (staging-source tree, no
merge commit, never used for retirement) from Contribute's records, then
drops the format-1 witnesses no record supports any longer. Re-running it
leaves current witnesses untouched.

  python3 backend/scripts/backfill_merged_contribution_refs.py \\
    --records /data/apps/<contribute app id>/contributions [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_APP_IMPORT_ROOT = str(Path(__file__).resolve().parent.parent)
if _APP_IMPORT_ROOT not in sys.path:
  sys.path.insert(0, _APP_IMPORT_ROOT)

from app import app_git  # noqa: E402

_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _merge_sha(record: dict) -> str | None:
  """The merge commit Contribute recorded, as ``_merged_upstream_sha`` reads it."""
  checks = record.get("checks") if isinstance(record.get("checks"), dict) else {}
  for value in (
    record.get("last_land_head_sha"), record.get("merge_commit_sha"),
    checks.get("merge_commit_sha"),
  ):
    candidate = str(value or "").strip().lower()
    if _GIT_SHA.fullmatch(candidate):
      return candidate
  return None


def _merged_platform_records(records: Path, repo: Path, github_repo: str) -> list[dict]:
  found = []
  for path in sorted(records.glob("*.json")):
    try:
      record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
      continue
    plan = record.get("plan") if isinstance(record, dict) else None
    if not isinstance(plan, dict) or record.get("status") != "merged":
      continue
    source = plan.get("source_repo_path")
    slug = str(plan.get("repo") or record.get("repo") or "")
    if (
      slug.lower() == github_repo.lower()
      and (not source or Path(source).resolve() == repo.resolve())
      and isinstance(record.get("number"), int)
    ):
      found.append(record)
  # A PR restaged by several records keeps the newest capture.
  return sorted(found, key=lambda r: str(r.get("updated_at") or ""))


def backfill(
  records: Path, repo: Path, *, github_repo: str = app_git.PLATFORM_RELEASE_REPO,
  dry_run: bool = False,
) -> dict[str, list[int]]:
  release = app_git.UPSTREAM_BRANCH
  if not app_git.ref_exists(repo, f"{release}^{{commit}}"):
    raise SystemExit(f"{repo} has no installed release ({release})")
  outcome: dict[str, list[int]] = {
    "written": [], "integrated": [], "unavailable": [], "dropped_legacy": [],
  }
  for record in _merged_platform_records(records, repo, github_repo):
    number = record["number"]
    plan = record["plan"]
    merge = _merge_sha(record)
    head = str(plan.get("head_sha") or "")
    files = plan.get("files")
    if merge and app_git.ref_is_ancestor(repo, merge, release) is True:
      outcome["integrated"].append(number)
    elif (
      not merge or not isinstance(files, list) or not files
      or not app_git.ref_exists(repo, f"{head}^{{commit}}")
    ):
      outcome["unavailable"].append(number)
    elif dry_run or app_git.record_merged_contribution(
      repo, number=number, repo_slug=str(plan.get("repo") or record.get("repo")),
      head_sha=head, merge_sha=merge, paths=files,
      contribution_id=str(record.get("id") or ""),
    ):
      outcome["written"].append(number)
    else:
      outcome["unavailable"].append(number)
  if not dry_run:
    outcome["dropped_legacy"] = app_git.drop_legacy_merged_contributions(repo)
  else:
    outcome["dropped_legacy"] = [
      number for number in app_git.drop_legacy_merged_contributions(repo, dry_run=True)
      if number not in outcome["written"]
    ]
  return {key: sorted(set(values)) for key, values in outcome.items()}


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--records", required=True, type=Path)
  parser.add_argument("--repo", default=Path("/data/platform"), type=Path)
  parser.add_argument("--github-repo", default=app_git.PLATFORM_RELEASE_REPO)
  parser.add_argument("--dry-run", action="store_true")
  args = parser.parse_args(argv)
  outcome = backfill(
    args.records, args.repo, github_repo=args.github_repo, dry_run=args.dry_run,
  )
  verb = "would write" if args.dry_run else "wrote"
  print(
    f"{verb} {len(outcome['written'])} witnesses; "
    f"{len(outcome['integrated'])} already in the installed release; "
    f"{len(outcome['unavailable'])} without a reviewed head or merge commit; "
    f"{len(outcome['dropped_legacy'])} unsupported format-1 witnesses "
    f"{'to drop' if args.dry_run else 'dropped'}"
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
