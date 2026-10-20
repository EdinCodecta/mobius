"""First-open consent for Store-installed apps.

An app *declares* what it needs (its capability contract, derived from the
manifest). The owner *accepts* that once, in one popup the first time it is
opened. The accepted rows are stored on the app; only a request that needs
more than they cover asks again. Acceptance is all or nothing, but the rows are
derived individually, so choosing rows later only needs a subset to be accepted.

Scope of this first slice: Store-installed apps (``manifest_url`` set). Local
and agent-built apps keep their present behaviour.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app import models

ROWS_VERSION = 1

BASELINE = (
  "Every app runs its own code on your server, so only allow apps you trust."
)

# The platform, not the app, decides what is critical: a permission that can
# change the installation or run things with the server's own power. An app
# cannot mark itself harmless because it never supplies this.
CRITICAL = frozenset({
  "filesystem_access", "manage_apps", "manage_skills", "connections_manage",
  "connect_manage", "identity_manage", "railway_manage", "agent_start",
  "project_builders", "workspace.screen-control",
})

_CHAT_TEXT = {
  "summary": (
    "Can read redacted text of your past chats. Tool calls, hidden reasoning, "
    "errors and secret-like values are removed."
  ),
  "summary_with_deleted": (
    "Same redacted text, and also chats you deleted that can still be recovered."
  ),
}
_LEVELS = {"none": 0, "read": 1, "write": 2}
_SHARED_MEMORY_TEXT = {
  "read": "Can read the shared memory area.",
  "write": "Can read and change the shared memory area.",
}
_ACCESS_TEXT = {
  "read": "Can read the private data of apps that opted in to share it.",
  "write": "Can read and change the private data of apps that opted in.",
}
_SHARE_TEXT = {
  "read": "Lets apps that can use other apps' data read this app's private data.",
  "write": "Lets apps that can use other apps' data read and change this app's private data.",
}
# One flag per row; title and text are the single owner-facing wording.
_FLAGS = (
  ("filesystem_api", "filesystem_access", "Use your files",
   "Can use the guarded owner-filesystem API."),
  ("manage_apps", "manage_apps", "Install and remove apps",
   "Can install and uninstall apps on your behalf."),
  ("manage_skills", "manage_skills", "Install and remove agent skills",
   "Can install and remove skills that steer your agents."),
  ("github_access", "github_access", "Use your GitHub account",
   "Can use the connected GitHub account."),
  ("github_connect", "github_connect", "Manage the GitHub connection",
   "Can start, refresh, cancel or disconnect your GitHub connection."),
  ("connections_manage", "connections_manage", "Manage tool connections",
   "Can add, change and remove tool (MCP) connections."),
  ("connect_manage", "connect_manage", "Manage paired computers",
   "Can pair, unpair and manage computers that run work for you."),
  ("identity_manage", "identity_manage", "Manage your sign-in identity",
   "Can manage the mobius.you sign-in link and identity."),
  ("railway_manage", "railway_manage", "Manage Railway deployments",
   "Can manage deployments on your Railway account."),
)


# Every key of the contract's ``data`` block must either produce a row or be
# named here. A test fails when a new permission is added without one, so a
# permission can never silently go missing from the popup.
HANDLED_DATA_KEYS = frozenset(
  {"chat_logs", "shared_memory", "cross_app_access", "share_with_apps",
   "job_secret_read"} | {key for key, *_ in _FLAGS}
)


def _row(row_id: str, title: str, text: str, *, level: str | None = None,
         rank: int = 1) -> dict[str, Any]:
  return {
    "id": row_id, "title": title, "text": text, "level": level,
    "rank": rank, "critical": row_id in CRITICAL,
  }


def rows_from_contract(
  contract: dict[str, Any] | None,
  *,
  project_builders: bool = False,
) -> list[dict[str, Any]]:
  """The rows a contract asks for, most serious first. Pure and deterministic."""
  if not isinstance(contract, dict):
    return []
  data = contract.get("data") or {}
  agent = contract.get("agent") or {}
  rows: list[dict[str, Any]] = []

  logs = (data.get("chat_logs") or {}).get("effective")
  if logs in _CHAT_TEXT:
    rank = 1 if logs == "summary" else 2
    rows.append(_row(
      "chat_log_access", "Read chat history", _CHAT_TEXT[logs],
      level="redacted" if rank == 1 else "redacted+deleted", rank=rank,
    ))
  for key, title, text_by_level, row_id in (
    ("shared_memory", "Use shared memory", _SHARED_MEMORY_TEXT, "shared_memory"),
    ("cross_app_access", "Use other apps' data", _ACCESS_TEXT, "cross_app_access"),
    ("share_with_apps", "Let other apps use its data", _SHARE_TEXT, "share_with_apps"),
  ):
    level = data.get(key)
    if level in ("read", "write"):
      rows.append(_row(
        row_id, title, text_by_level[level], level=level, rank=_LEVELS[level],
      ))
  for key, row_id, title, text in _FLAGS:
    if data.get(key):
      rows.append(_row(row_id, title, text))
  names = data.get("job_secret_read")
  if isinstance(names, list) and names:
    rows.append(_row(
      "job_secret_read", "Read named secrets in its job",
      "Its background job can read the secrets it names: " + ", ".join(names) + ".",
    ))
  if agent.get("embeds_agent"):
    rows.append(_row(
      "agent_start", "Start agent chats on its own",
      "Can start and drive agent chats that run commands as Möbius, with "
      "tool calls approved automatically.",
    ))
  if agent.get("system_prompt"):
    rows.append(_row(
      "agent_prompt", "Add instructions to your agent chats",
      "Adds system instructions to new agent chats while installed.",
    ))
  if agent.get("skills") or agent.get("tools"):
    rows.append(_row(
      "agent_extend", "Add skills and tools to your agents",
      "Installs the skills it ships and lets agents call the tools it declares.",
    ))
  if contract.get("model_provider"):
    rows.append(_row(
      "model_provider", "Add a model provider",
      "Adds a model provider whose requests use a secret saved in the app.",
    ))
  service = contract.get("service") or {}
  public = contract.get("public") or {}
  if service.get("access") in ("apps", "public") or public.get("network") or (
    (public.get("storage") or {}).get("read")
  ):
    rows.append(_row(
      "reach", "Be reachable by other apps or the public",
      "Lets other apps, or anyone without signing in, call its service or "
      "see its published surface.",
    ))
  if project_builders:
    rows.append(_row(
      "project_builders", "Run builder scripts",
      "Adds project types whose builder scripts run with the same access as Möbius itself.",
    ))
  for capability, declaration in sorted((contract.get("runtime") or {}).items()):
    if not isinstance(declaration, dict):
      continue
    rows.append(_row(
      capability, str(declaration.get("title") or capability),
      str(declaration.get("description") or capability),
    ))
  rows.sort(key=lambda row: (not row["critical"], row["title"]))
  return rows


def request_digest(rows: list[dict[str, Any]]) -> str:
  canonical = json.dumps(
    sorted((r["id"], int(r.get("rank") or 1), r.get("level")) for r in rows),
    separators=(",", ":"),
  )
  return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rows_for_manifest(manifest: dict[str, Any], contract: dict[str, Any]) -> list[dict[str, Any]]:
  """The rows an install would ask for, for a manifest not yet installed."""
  return rows_from_contract(
    contract, project_builders=bool(manifest.get("project_templates")),
  )


def gated(app: models.App) -> bool:
  """Only Store-installed apps with a reviewed contract use first-open consent."""
  return app.manifest_url is not None and isinstance(app.capability_contract, dict)


def current_rows(app: models.App) -> list[dict[str, Any]]:
  return rows_from_contract(
    app.capability_contract, project_builders=bool(app.project_templates_json),
  )


def _record(rows: list[dict[str, Any]], via: str) -> dict[str, Any]:
  """What is stored when an app is accepted: the rows themselves.

  Keeping the accepted rows (not the contract they came from) means later
  changes to how rows are derived or worded can never turn an old answer into
  a new question. ``version`` names the row vocabulary for a future migration.
  """
  return {
    "version": ROWS_VERSION,
    "rows": [{"id": r["id"], "rank": int(r.get("rank") or 1)} for r in rows],
    "digest": request_digest(rows),
    "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "via": via,
  }


def accepted_ranks(app: models.App) -> dict[str, int] | None:
  """Row id to accepted rank, or None when the app was never accepted."""
  record = app.accepted_consent
  if not isinstance(record, dict):
    return None
  if isinstance(record.get("rows"), list):
    return {r["id"]: int(r.get("rank") or 1) for r in record["rows"]}
  # Records written by the grandfathering migration hold a contract copy.
  if isinstance(record.get("contract"), dict):
    return {
      r["id"]: int(r.get("rank") or 1)
      for r in rows_from_contract(
        record["contract"], project_builders=bool(record.get("project_builders")),
      )
    }
  return None


def needed_rows(app: models.App) -> list[dict[str, Any]]:
  """Rows the current contract asks for that the accepted answer does not cover."""
  rows = current_rows(app)
  covered = accepted_ranks(app)
  if covered is None:
    return rows
  return [r for r in rows if covered.get(r["id"], 0) < int(r.get("rank") or 1)]


def status(app: models.App) -> str:
  """``not_applicable``, ``required`` (ask first) or ``accepted``."""
  if not gated(app):
    return "not_applicable"
  return "required" if needed_rows(app) or accepted_ranks(app) is None else "accepted"


def never_accepted(app: models.App) -> bool:
  return gated(app) and accepted_ranks(app) is None


def _holders(db: Session, app: models.App) -> dict[str, list[tuple[str, int]]]:
  held: dict[str, list[tuple[str, int]]] = {}
  others = (
    db.query(models.App)
    .filter(models.App.deleted_at.is_(None), models.App.id != app.id)
    .all()
  )
  for other in others:
    ranks = accepted_ranks(other)
    if ranks is None:
      ranks = {r["id"]: int(r.get("rank") or 1) for r in current_rows(other)}
    for row_id, rank in ranks.items():
      held.setdefault(row_id, []).append((other.name, rank))
  return held


def _list(names: list[str]) -> str:
  if len(names) > 4:
    return ", ".join(names[:3]) + f" and {len(names) - 3} more"
  return ", ".join(names)


def _note(row: dict[str, Any], held: dict[str, list[tuple[str, int]]]) -> str:
  holders = held.get(row["id"], [])
  parts: list[str] = []
  if holders:
    parts.append("Also held by " + _list([name for name, _ in holders]) + ".")
    lower = [name for name, rank in holders if rank < int(row.get("rank") or 1)]
    if row["id"] == "chat_log_access" and lower:
      parts.append(_list(lower) + " hold only “redacted”; this asks for more.")
  else:
    parts.append("No installed app holds this yet.")
  if row["id"] == "cross_app_access":
    sharing = [name for name, _ in held.get("share_with_apps", [])]
    parts.append(
      "It could reach only apps that opted in: "
      + (_list(sharing) if sharing else "none today") + "."
    )
  if row["id"] == "share_with_apps":
    readers = [name for name, _ in held.get("cross_app_access", [])]
    parts.append(
      "Today " + (_list(readers) + " could read it." if readers
                  else "no app could read it.")
    )
  return " ".join(parts)


def build_request(db: Session, app: models.App) -> dict[str, Any]:
  """What the popup shows: only the rows not yet accepted, with context."""
  held = _holders(db, app)
  return {
    "app_id": app.id,
    "app_name": app.name,
    "app_description": app.description,
    "digest": request_digest(current_rows(app)),
    "is_update": accepted_ranks(app) is not None,
    "rows": [{**row, "note": _note(row, held)} for row in needed_rows(app)],
    "baseline": BASELINE,
  }


def accept(app: models.App, digest: str, *, via: str = "owner") -> bool:
  """Record acceptance if the digest still names the app's current request."""
  rows = current_rows(app)
  if digest != request_digest(rows):
    return False
  app.accepted_consent = _record(rows, via)
  return True


def revoke(app: models.App) -> None:
  """Make the app dormant again; the next open asks from scratch."""
  app.accepted_consent = None


def platform_acceptance(
  contract: dict[str, Any] | None, *, project_builders: bool = False,
) -> dict[str, Any] | None:
  """Acceptance for apps that ship with the platform (first-boot installs)."""
  if not isinstance(contract, dict):
    return None
  return _record(
    rows_from_contract(contract, project_builders=project_builders), "platform",
  )
