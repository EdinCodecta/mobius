#!/usr/bin/env python3
"""Mark a local platform commit's overlay disposition without rewriting it.

Platform updates carry local commits as labelled units. A unit marked
``pending-pr:#N`` is retired automatically by the first update whose release
merged pull request N, if its files are still the bytes the unit wrote;
``owner-choice:local`` is never retired. The mark is a local git note
(``refs/notes/mobius-overlay``), never pushed, and applies to the marked
commit's own unit.

A unit whose every path a later unit took over has no commit of its own after
an update re-emits the overlay: it survives only as a
``Mobius-Overlay-Also: <unit>=<disposition>`` trailer on the commits it
shaped, so it cannot be marked directly. Mark one of those commits instead;
``owner-choice:local`` on it guards the same files, since a file retires only
when no unit that shaped it chose to keep it local.

  python3 backend/scripts/platform_overlay.py mark <commit> pending-pr:#1234
  python3 backend/scripts/platform_overlay.py mark <commit> owner-choice:local
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_APP_IMPORT_ROOT = str(Path(__file__).resolve().parent.parent)
if _APP_IMPORT_ROOT not in sys.path:
  sys.path.insert(0, _APP_IMPORT_ROOT)

from app import app_git  # noqa: E402


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  commands = parser.add_subparsers(dest="command", required=True)
  mark = commands.add_parser("mark", help="set one commit's unit disposition")
  mark.add_argument("commit")
  mark.add_argument("disposition")
  parser.add_argument("--repo", default="/data/platform")
  args = parser.parse_args(argv)
  try:
    sha = app_git.mark_overlay_disposition(args.repo, args.commit, args.disposition)
  except ValueError as exc:
    print(f"platform-overlay: {exc}", file=sys.stderr)
    return 2
  print(f"{sha[:12]} {args.disposition}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
