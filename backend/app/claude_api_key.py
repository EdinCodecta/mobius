"""The owner's Anthropic Console API key for the built-in Claude provider.

An owner without a Claude subscription can run Claude with a pay-per-use API
key instead. The key arrives only through the owner's own Settings form, never
through a chat or a model, and is stored owner-only (0600) in the protected
cli-auth area beside the subscription sign-in it substitutes for, with the
same protection as that sign-in's ``.credentials.json``.

Precedence is fixed and owned by ``providers.claude_auth_method``: a usable
subscription sign-in always wins, so saving a key never silently moves an
owner from a subscription onto per-use billing.

The key never enters a process environment. The Claude CLI passes its
environment to every child (Bash tool commands, MCP servers, package
scripts), so an ``env`` dump would print an injected key into the transcript;
the CLI's own environment scrubbing needs bubblewrap, which the image does
not ship. Instead Claude runs receive an ``apiKeyHelper`` setting
(``providers.claude_cli_settings``): the CLI runs ``helper_command`` itself
and keeps the result in memory. The helper reads this plain file rather than
an encrypted one because it runs without the server secret, which Möbius
withholds from children and which some installations never persist to disk.
"""

import logging
import os
import re
import shlex
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Console keys are ``sk-ant-api03-…``; admin keys (``sk-ant-admin…``) cannot
# run inference, and OAuth tokens (``sk-ant-oat…``) belong to the sign-in flow.
_KEY_SHAPE = re.compile(r"sk-ant-api[0-9]{2}-[A-Za-z0-9_-]{20,500}")


class ClaudeApiKeyUnreadable(RuntimeError):
  """A saved key exists but cannot be read."""


def api_key_path(data_dir: str) -> Path:
  return Path(data_dir) / "cli-auth" / "claude" / "api-key"


def helper_command(data_dir: str) -> str:
  """The CLI ``apiKeyHelper`` command: it prints the saved key on stdout."""
  return f"cat {shlex.quote(str(api_key_path(data_dir)))}"


def normalize_api_key(value: str) -> str:
  """Return the key without surrounding whitespace, or raise ValueError.

  The message never echoes the value: it is shown to the owner and may be
  logged by generic error handling.
  """
  candidate = value.strip() if isinstance(value, str) else ""
  if candidate.startswith("sk-ant-oat"):
    raise ValueError(
      "That is a Claude sign-in token, not an API key. Use Sign in with "
      "Claude instead, or create an API key in the Claude Console."
    )
  if not _KEY_SHAPE.fullmatch(candidate):
    raise ValueError(
      "That does not look like an Anthropic API key. Copy the whole key "
      "(it starts with sk-ant-api) from the Claude Console."
    )
  return candidate


def save_api_key(data_dir: str, key: str) -> None:
  """Atomically replace the saved key; the file is owner-only (0600)."""
  path = api_key_path(data_dir)
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, temporary = tempfile.mkstemp(
    dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
  )
  try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as file:
      file.write(key)
      file.flush()
      os.fsync(file.fileno())
    os.replace(temporary, path)
  except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise


def read_api_key(data_dir: str) -> str | None:
  """The saved key, or None when none is saved.

  Raises ClaudeApiKeyUnreadable when a saved key cannot be read, so a caller
  can tell the owner to enter it again rather than "not connected".
  """
  path = api_key_path(data_dir)
  try:
    key = path.read_text(encoding="ascii").strip()
  except FileNotFoundError:
    return None
  except (OSError, UnicodeError) as exc:
    raise ClaudeApiKeyUnreadable("saved Anthropic API key is unreadable") from exc
  if not _KEY_SHAPE.fullmatch(key):
    raise ClaudeApiKeyUnreadable("saved Anthropic API key is unreadable")
  return key


def remove_api_key(data_dir: str) -> None:
  api_key_path(data_dir).unlink(missing_ok=True)


async def verify_api_key(key: str) -> None:
  """Check the key against Anthropic's free model listing; no inference.

  Raises ValueError when Anthropic rejects the key and ConnectionError when
  Anthropic cannot be reached or answers unexpectedly.
  """
  import httpx

  try:
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
      response = await client.get(
        "https://api.anthropic.com/v1/models?limit=1",
        headers=api_key_headers(key),
      )
  except httpx.HTTPError as exc:
    raise ConnectionError("Anthropic could not be reached") from exc
  if response.status_code in (401, 403):
    raise ValueError(
      "Anthropic rejected this API key. Check that it is active in the "
      "Claude Console, or create a new one."
    )
  # Only a 2xx proves the key: a redirect (say, a captive portal) proves
  # nothing about it.
  if not response.is_success:
    log.warning("Anthropic key check returned HTTP %s", response.status_code)
    raise ConnectionError("Anthropic could not check the key")


def api_key_headers(key: str) -> dict[str, str]:
  return {"x-api-key": key, "anthropic-version": "2023-06-01"}
