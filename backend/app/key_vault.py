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
import logging
import os
import sys
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


def app_may_use(app_id: int, name: str) -> bool:
  """Whether ``name`` exists and the owner granted it to ``app_id``."""
  try:
    keys = vault_call_sync("GET", "/keys")["keys"]
  except (VaultUnavailable, VaultRefused):
    return False
  return any(k["name"] == name and app_id in k["grants"]["apps"] for k in keys)


def reveal_for_app_platform_use(app_id: int, name: str) -> str | None:
  """A granted key's value for platform code acting for an app.

  The platform itself (not the app's code) sometimes needs the value, for
  example to launch a model runtime an app declared. The app's grant still
  decides, and the vault logs the read as ``platform:app-<id>``.
  """
  if not app_may_use(app_id, name):
    return None
  try:
    return vault_call_sync(
      "GET", f"/keys/{name}/value", caller=f"platform:app-{app_id}",
    )["value"]
  except (VaultUnavailable, VaultRefused):
    return None


async def forget_app(app_id: int) -> None:
  """Drop a purged app from every grant and request; raise if that fails.

  An instance whose vault never stored anything has nothing to forget, so a
  purge does not depend on a vault that was never used.
  """
  folder = vault_dir()
  if not (folder / "keys.json").exists() and not (folder / "requests.json").exists():
    return
  await vault_call("DELETE", f"/apps/{int(app_id)}")


async def supervise_key_vault() -> None:
  """Keep the vault process running for this server's lifetime.

  The vault exits on its own when this server's pid stops being its parent,
  so a crashed server never leaves an orphan holding the socket.
  """
  if not VAULT_PROGRAM.is_file():
    log.warning("key vault program missing at %s; Keys stay unavailable", VAULT_PROGRAM)
    return
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
