#!/usr/bin/env python3
"""Write merged-contribution witnesses for platform PRs merged before they existed.

Contribute now records ``refs/mobius/merged-contributions/<N>`` when a
platform contribution settles merged, and the next update retires local files
still holding the bytes that PR started from. Contributions settled earlier
have no witness. This one-time, local-only pass reads Contribute's records and
writes one for each merged record whose source is this repository and whose
pull request the installed release does not carry yet. It is idempotent and
offline.

  python3 backend/scripts/backfill_merged_contribution_refs.py \\
    --records /data/apps/<contribute app id>/contributions [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_APP_IMPORT_ROOT = str(Path(__file__).resolve().parent.parent)
if _APP_IMPORT_ROOT not in sys.path:
  sys.path.insert(0, _APP_IMPORT_ROOT)

from app import app_git  # noqa: E402

_MERGE_SHA_KEYS = ("last_land_head_sha", "merge_commit_sha")


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
    ours = (
      Path(source).resolve() == repo.resolve() if source
      else plan.get("repo") == github_repo
    )
    if ours and isinstance(record.get("number"), int):
      found.append(record)
  # A PR restaged by several records keeps the newest capture.
  return sorted(found, key=lambda r: str(r.get("updated_at") or ""))


def backfill(
  records: Path, repo: Path, *, github_repo: str = "mobius-os/mobius",
  dry_run: bool = False,
) -> dict[str, list[int]]:
  release = app_git._resolve_commit(repo, app_git.UPSTREAM_BRANCH)
  if release is None:
    raise SystemExit(f"{repo} has no installed release ({app_git.UPSTREAM_BRANCH})")
  integrated = app_git._pr_merge_commits(repo, release)
  outcome: dict[str, list[int]] = {"written": [], "integrated": [], "unavailable": []}
  for record in _merged_platform_records(records, repo, github_repo):
    number = record["number"]
    plan = record["plan"]
    merge = next((
      str(record.get(key) or "").lower() for key in _MERGE_SHA_KEYS
      if app_git._HEX_OID.fullmatch(str(record.get(key) or "").lower())
    ), None)
    if number in integrated or (
      merge and app_git.ref_is_ancestor(repo, merge, release) is True
    ):
      outcome["integrated"].append(number)
      continue
    source = app_git._resolve_commit(repo, str(plan.get("source_sha") or ""))
    files = plan.get("files")
    if source is None or not isinstance(files, list) or not files:
      outcome["unavailable"].append(number)
      continue
    if not dry_run:
      app_git.record_merged_contribution(
        repo, number=number, source_sha=source, paths=files, merge_sha=merge,
        contribution_id=str(record.get("id") or ""),
      )
    outcome["written"].append(number)
  return {key: sorted(set(values)) for key, values in outcome.items()}


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--records", required=True, type=Path)
  parser.add_argument("--repo", default=Path("/data/platform"), type=Path)
  parser.add_argument("--github-repo", default="mobius-os/mobius")
  parser.add_argument("--dry-run", action="store_true")
  args = parser.parse_args(argv)
  outcome = backfill(
    args.records, args.repo, github_repo=args.github_repo, dry_run=args.dry_run,
  )
  verb = "would write" if args.dry_run else "wrote"
  print(
    f"{verb} {len(outcome['written'])} witnesses; "
    f"{len(outcome['integrated'])} already in the installed release; "
    f"{len(outcome['unavailable'])} without captured source"
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
