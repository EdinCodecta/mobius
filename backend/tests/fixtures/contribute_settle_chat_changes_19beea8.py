#!/usr/bin/env python3
# Copy of mobius-os/app-contribute settle_chat_changes.py (only this header added) at
# 19beea8176bb8ed3642a1eab626bea865026e85a, the shipped caller that the
# retired settle route must keep working for. Do not edit; replace it only
# with another released Contribute version under test.
"""Record reviewed chat edits that intentionally remain local.

The platform owns validation, temporal merging, and projection into Changes.
This helper keeps agents on that one domain path instead of hand-editing app
storage or merely describing exclusions in prose.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DISPOSITIONS = (
  "local-only", "personal", "experimental", "incoming-only", "duplicate",
)


def build_payload(args: argparse.Namespace) -> dict:
  coverage_at = args.through.strip()
  if coverage_at.isdigit():
    coverage_at = int(coverage_at)
  return {
    "coverage_at": coverage_at,
    "items": [
      {
        "path": path,
        "disposition": args.disposition,
        "summary": args.summary,
      }
      for path in dict.fromkeys(args.paths)
    ],
  }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Settle reviewed local-only paths for one Möbius chat.",
  )
  parser.add_argument("paths", nargs="+", help="Absolute /data/platform or /data/apps source paths")
  parser.add_argument("--chat", default=os.environ.get("CHAT_ID", ""))
  parser.add_argument("--app-id", type=int)
  parser.add_argument("--through", required=True, help="Newest chat edit timestamp actually reviewed")
  parser.add_argument("--disposition", choices=DISPOSITIONS, default="local-only")
  parser.add_argument("--summary", default="")
  return parser.parse_args(argv)


def installed_app_id(base_url: str, token: str) -> int:
  request = Request(
    f"{base_url}/api/apps/",
    headers={"Authorization": f"Bearer {token}"},
  )
  with urlopen(request, timeout=30) as response:
    apps = json.load(response)
  match = next(
    (app for app in apps if isinstance(app, dict) and app.get("slug") == "contribute"),
    None,
  )
  if not match or not isinstance(match.get("id"), int):
    raise RuntimeError("Contribute is not installed")
  return match["id"]


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  base_url = os.environ.get("API_BASE_URL", "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN", "")
  if not args.chat or not base_url or not token:
    print("CHAT_ID, API_BASE_URL, and AGENT_TOKEN are required.", file=sys.stderr)
    return 2
  try:
    app_id = args.app_id or installed_app_id(base_url, token)
  except (RuntimeError, HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
    print(str(exc), file=sys.stderr)
    return 2
  body = json.dumps(build_payload(args)).encode("utf-8")
  request = Request(
    f"{base_url}/api/github/contributions/{app_id}/for-chat/{args.chat}/settle",
    data=body,
    method="POST",
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
  )
  try:
    with urlopen(request, timeout=30) as response:
      result = json.load(response)
  except HTTPError as exc:
    detail = exc.read().decode("utf-8", errors="replace")
    print(f"Settlement failed ({exc.code}): {detail}", file=sys.stderr)
    return 1
  except (URLError, TimeoutError, json.JSONDecodeError) as exc:
    print(f"Settlement could not be confirmed: {exc}", file=sys.stderr)
    return 1
  print(json.dumps({
    "updated_at": result.get("updated_at"),
    "settled": len(result.get("settlements") or []),
  }))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
