"""Server-side owner of the key vault process and its socket.

The vault itself is ``backend/vault/keyvault.py``: a standalone program that
holds keys encrypted. Only this server talks to it, over one mode-0600 Unix
socket, and it states who each call is for (``app:<id>``, ``agent:<chat>``,
``owner`` or ``platform:<purpose>``) after authenticating the caller with its
own platform-issued token. That attestation is the handshake: an app cannot
name itself, and no app ever receives a value.

This module starts and supervises the vault (from
``RuntimeSupervisors.start_process_services``) and gives routes and the
platform a small client for it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

import httpx

from app.config import get_settings


log = logging.getLogger(__name__)

VAULT_PROGRAM = Path(__file__).resolve().parents[1] / "vault" / "keyvault.py"
SOCKET_NAME = "vault.sock"
_RESTART_BACKOFF_SECS = (1, 2, 5, 10, 30, 60)
# A vault that ran this long before exiting counts as healthy again.
_HEALTHY_RUN_SECS = 120
_STOP_GRACE_SECS = 5
# Matches the vault's own upstream timeout plus room for its answer.
_USE_TIMEOUT_SECS = 75


class VaultUnavailable(RuntimeError):
  """The vault process is not answering on its socket."""


class VaultRefused(RuntimeError):
  """The vault refused; ``reason`` is caller-facing."""

  def __init__(self, status: int, reason: str) -> None:
    super().__init__(reason)
    self.status = status
    self.reason = reason


def vault_dir() -> Path:
  return Path(get_settings().data_dir) / ".vault"


def vault_socket() -> Path:
  return vault_dir() / SOCKET_NAME


def _caller_headers(caller: str | None) -> dict[str, str]:
  return {"X-Vault-Caller": caller} if caller else {}


def _settle(response: httpx.Response) -> dict:
  try:
    body = response.json()
  except ValueError:
    body = {}
  if response.status_code >= 400:
    raise VaultRefused(response.status_code, str(body.get("error") or "The key vault refused."))
  return body


async def vault_call(
  method: str, path: str, payload: dict | None = None, *, caller: str | None = None,
) -> dict:
  """One operation on the vault socket, on behalf of an attested caller."""
  transport = httpx.AsyncHTTPTransport(uds=str(vault_socket()))
  try:
    async with httpx.AsyncClient(
      transport=transport, base_url="http://vault", timeout=10,
    ) as client:
      response = await client.request(
        method, path, json=payload, headers=_caller_headers(caller),
      )
  except (httpx.TransportError, OSError) as exc:
    raise VaultUnavailable("The key vault is not running.") from exc
  return _settle(response)


def vault_call_sync(method: str, path: str, *, caller: str | None = None) -> dict:
  """``vault_call`` for synchronous platform code (model provider launch)."""
  try:
    with httpx.Client(
      transport=httpx.HTTPTransport(uds=str(vault_socket())),
      base_url="http://vault", timeout=10,
    ) as client:
      response = client.request(method, path, headers=_caller_headers(caller))
  except (httpx.TransportError, OSError) as exc:
    raise VaultUnavailable("The key vault is not running.") from exc
  return _settle(response)


async def vault_use(
  method: str, target: str, *, caller: str, headers: list[tuple[str, str]],
  body: bytes | None,
) -> httpx.Response:
  """Relay one use request (``/use/<key>/<host>/<path>?<query>``) as is.

  The vault answers refusals itself (``X-Vault-Denied``), so any response is
  returned to the caller unchanged; only an unreachable vault raises.
  """
  transport = httpx.AsyncHTTPTransport(uds=str(vault_socket()))
  try:
    async with httpx.AsyncClient(
      transport=transport, base_url="http://vault", timeout=_USE_TIMEOUT_SECS,
    ) as client:
      request = client.build_request(
        method, target, headers=[*headers, ("X-Vault-Caller", caller)], content=body,
      )
      # The caller's raw path must reach the vault byte for byte: its path
      # rules refuse encoded slashes and dot segments that normalising here
      # would hide.
      request.url = request.url.copy_with(raw_path=target.encode("ascii"))
      return await client.send(request)
  except (httpx.TransportError, OSError) as exc:
    raise VaultUnavailable("The key vault is not running.") from exc


# -- an app's own key names --------------------------------------------------
#
# Before the vault, each app had its own secret namespace, so two apps could
# both save "api_key". Vault names are shared: an app's name maps to the plain
# name when that is free, otherwise to an app-scoped one.

_VAULT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _vault_safe(name: str) -> str:
  cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", name)
  if not cleaned[:1].isalpha():
    cleaned = f"k{cleaned}"
  return cleaned[:64]


def app_key_names(app_id: int, name: str) -> tuple[str, str]:
  """The plain and the app-scoped vault names for an app's ``name``."""
  plain = _vault_safe(name)
  return plain, f"app{int(app_id)}-{plain}"[:64]


def granted_key_in(keys: list[dict], app_id: int, name: str) -> str | None:
  for candidate in app_key_names(app_id, name):
    if any(k["name"] == candidate and app_id in k["grants"]["apps"] for k in keys):
      return candidate
  return None


def granted_key_sync(app_id: int, name: str) -> str | None:
  """The vault key an app's ``name`` refers to, if the owner granted it."""
  try:
    keys = vault_call_sync("GET", "/keys")["keys"]
  except (VaultUnavailable, VaultRefused):
    return None
  return granted_key_in(keys, app_id, name)


async def granted_key(app_id: int, name: str) -> str | None:
  return granted_key_in((await vault_call("GET", "/keys"))["keys"], app_id, name)


def app_may_use(app_id: int, name: str) -> bool:
  """Whether the owner granted ``app_id`` the key its ``name`` refers to."""
  return granted_key_sync(app_id, name) is not None


def reveal_for_app_platform_use(app_id: int, name: str) -> str | None:
  """A granted key's value for platform code acting for an app.

  The platform itself (not the app's code) sometimes needs the value, for
  example to launch a model runtime an app declared. The app's grant still
  decides, and the vault logs the read as ``platform:app-<id>``.
  """
  key = granted_key_sync(app_id, name)
  if key is None:
    return None
  try:
    return vault_call_sync(
      "GET", f"/keys/{key}/value", caller=f"platform:app-{app_id}",
    )["value"]
  except (VaultUnavailable, VaultRefused):
    return None


async def store_app_key(app_id: int, name: str, value: str) -> str:
  """Save ``value`` as the key an app's ``name`` refers to; return its name.

  An existing key granted to the app is replaced. A new key is granted to the
  app and its owner's agents, sent as a bearer token, with no sites yet: the
  owner adds those before the app can borrow it.
  """
  keys = (await vault_call("GET", "/keys"))["keys"]
  existing = granted_key_in(keys, app_id, name)
  if existing is not None:
    record = next(k for k in keys if k["name"] == existing)
    await vault_call("PUT", f"/keys/{existing}", {
      "value": value, "inject": record["inject"], "grants": record["grants"],
    })
    return existing
  taken = {k["name"] for k in keys}
  plain, scoped = app_key_names(app_id, name)
  target = plain if plain not in taken else scoped
  await vault_call("PUT", f"/keys/{target}", {
    "value": value,
    "inject": {"type": "header", "name": "Authorization", "prefix": "Bearer "},
    "grants": {"apps": [int(app_id)], "agents": True, "sites": []},
  })
  return target


# -- one-time import of the retired per-app secret store ----------------------

_LEGACY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IMPORT_LEDGER = ".vault-import.json"


def _legacy_fernet():
  from cryptography.fernet import Fernet
  material = f"mobius-app-secret-v1:{get_settings().secret_key}".encode()
  return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))


def _write_ledger(path: Path, ledger: dict) -> None:
  fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
  try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
      json.dump(ledger, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)
  except BaseException:
    try:
      os.unlink(tmp)
    except FileNotFoundError:
      pass
    raise


async def import_legacy_app_secrets(live_app_ids: set[int]) -> dict:
  """Move ``/data/app-secrets/<app>/<name>`` values into the vault, once each.

  Each value becomes the key its app's name refers to (``store_app_key``), so
  model providers and the deprecated secrets adapter keep finding it. A ledger
  next to the old files records what moved, so a key the owner later deletes
  is never re-imported; unreadable values are retried on the next start. The
  old ciphertext stays in place for one release as a fallback copy.
  """
  root = Path(get_settings().data_dir) / "app-secrets"
  if not root.is_dir():
    return {}
  ledger_path = root / _IMPORT_LEDGER
  try:
    ledger = json.loads(ledger_path.read_text())
  except (FileNotFoundError, ValueError):
    ledger = {}
  fernet = _legacy_fernet()
  moved = {}
  for app_dir in sorted(root.iterdir()):
    if not app_dir.is_dir() or not app_dir.name.isdigit():
      continue
    app_id = int(app_dir.name)
    if app_id not in live_app_ids:
      continue  # a purged app's id may be reused: never grant it onward
    for secret in sorted(app_dir.iterdir()):
      entry = f"{app_id}/{secret.name}"
      if entry in ledger or not secret.is_file() or not _LEGACY_NAME_RE.fullmatch(secret.name):
        continue
      try:
        value = fernet.decrypt(secret.read_bytes()).decode()
        ledger[entry] = await store_app_key(app_id, secret.name, value)
      except Exception as exc:  # unreadable or refused: retried on the next start
        log.warning("key vault: could not import secret %s: %s", entry, type(exc).__name__)
        continue
      moved[entry] = ledger[entry]
      _write_ledger(ledger_path, ledger)
  if moved:
    log.info("key vault: imported %d app secret(s) into the vault", len(moved))
  return moved


async def forget_app(app_id: int) -> None:
  """Drop a purged app from every grant and request; raise if that fails.

  An instance whose vault never stored anything has nothing to forget, so a
  purge does not depend on a vault that was never used.
  """
  folder = vault_dir()
  if not (folder / "keys.json").exists() and not (folder / "requests.json").exists():
    return
  await vault_call("DELETE", f"/apps/{int(app_id)}")


async def _import_when_ready() -> None:
  """Run the one-time legacy import once the vault answers."""
  for _ in range(60):
    try:
      await vault_call("GET", "/health")
      break
    except VaultUnavailable:
      await asyncio.sleep(1)
  else:
    log.warning("key vault did not start; legacy app secrets stay where they are")
    return
  from app import models
  from app.database import SessionLocal
  with SessionLocal() as db:
    live = {row.id for row in db.query(models.App.id).all()}
  try:
    await import_legacy_app_secrets(live)
  except Exception:
    log.exception("key vault: legacy app-secret import failed; it is retried on the next start")


async def supervise_key_vault() -> None:
  """Keep the vault process running for this server's lifetime.

  The vault exits on its own when this server's pid stops being its parent,
  so a crashed server never leaves an orphan holding the socket. Once per
  server start, values from the retired per-app secret store are imported.
  """
  if not VAULT_PROGRAM.is_file():
    log.warning("key vault program missing at %s; Keys stay unavailable", VAULT_PROGRAM)
    return
  importer = asyncio.create_task(_import_when_ready())
  failures = 0
  log_dir = Path(get_settings().data_dir) / "logs"
  log_dir.mkdir(parents=True, exist_ok=True)
  while True:
    started = asyncio.get_running_loop().time()
    with open(log_dir / "key-vault.log", "ab") as output:
      process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(VAULT_PROGRAM),
        "--data", str(vault_dir()),
        "--parent", str(os.getpid()),
        stdin=asyncio.subprocess.DEVNULL, stdout=output, stderr=output,
        # A minimal environment: the vault needs nothing of the server's.
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
      )
    try:
      code = await process.wait()
    except asyncio.CancelledError:
      importer.cancel()
      await _stop(process)
      raise
    if asyncio.get_running_loop().time() - started >= _HEALTHY_RUN_SECS:
      failures = 0
    delay = _RESTART_BACKOFF_SECS[min(failures, len(_RESTART_BACKOFF_SECS) - 1)]
    failures += 1
    log.warning("key vault exited with %s; restarting in %ss", code, delay)
    await asyncio.sleep(delay)


async def _stop(process: asyncio.subprocess.Process) -> None:
  if process.returncode is not None:
    return
  process.terminate()
  try:
    await asyncio.wait_for(process.wait(), _STOP_GRACE_SECS)
  except asyncio.TimeoutError:
    process.kill()
    await process.wait()
