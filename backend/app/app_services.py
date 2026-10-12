"""Bounded request/response execution for app-owned server policy.

The platform owns authentication, accepted-runtime identity, process limits,
and revocation. The app owns the request paths and domain behavior behind one
small JSON protocol. This is deliberately not a framework or an import hook:
every request runs in its own process from one reviewed Python entrypoint and
receives one JSON reply. An entry that declares ``MOBIUS_PRELOAD = True`` has
its module setup run once and each request forked from it (``service_preload``);
every other entry, and any request no preloaded host can take, is spawned.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import contextlib
import io
import itertools
import json
import logging
import os
import re
import signal
import sys
import weakref
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException

from app import app_python_env, auth, models, service_preload
from app.applied_app_runtime import AppliedRuntimeUnavailable, hold_runtime, runtime_root
from app.browser_access import BrowserLineage, require_live
from app.config import get_settings
from app.manifest_contract import (
  SERVICE_REQUEST_MAX_BYTES,
  accepted_service_max_bytes, accepted_service_route_limits, service_route_key,
)


log = logging.getLogger(__name__)
MAX_REQUEST_BYTES = SERVICE_REQUEST_MAX_BYTES
MAX_RESPONSE_BYTES = SERVICE_REQUEST_MAX_BYTES
MAX_ERROR_BYTES = 16 * 1024
SERVICE_TIMEOUT_SECONDS = 15
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}$")
# The only headers an app service may add. A service answers on the shell
# origin, so anything else (Set-Cookie, Clear-Site-Data, NEL/Report-To,
# Service-Worker-Allowed, framing, transport) would act on the owner's whole
# session or on the platform's own response contract, and is dropped. The
# platform sets the content type from `media_type`.
_SERVICE_RESPONSE_HEADERS = frozenset({
  "cache-control", "content-disposition", "content-language", "etag",
  "last-modified", "vary",
})
# Cache-Control directives that let a shared cache store a response to an
# authorized request (RFC 9111 section 3.5), unless private/no-store also apply.
_SHARED_CACHE_DIRECTIVES = frozenset({
  "public", "s-maxage", "must-revalidate", "proxy-revalidate",
})
_global_slots = {
  "private": asyncio.Semaphore(8),
  "public": asyncio.Semaphore(8),
  "tools": asyncio.Semaphore(8),
}
_app_slots: weakref.WeakValueDictionary[tuple[int, str], asyncio.Semaphore] = (
  weakref.WeakValueDictionary()
)


# This is an exchange allocation budget, not another transfer grant. It is
# shared by every lane and held through the execution queue and response
# handoff. Exhaustion never buffers another body byte and never starts a process.
#
# Structural marks ('"', '{', '[', ',', ':' outside strings) bound the work of
# the stdlib parser and renderer, which run as single uninterruptible C calls,
# so larger transfer grants buy media bytes, not denser JSON. Depth stays well
# inside every consumer: FastAPI's response validation of a tool result fails
# near 255 levels, and the pure-Python request encoder needs one frame per level.
MAX_JSON_STRUCTURE = 1 << 19
# Parsing and rendering a number take time in proportion to its text: a
# 17-digit float costs well over ten marks. Charging characters outside
# strings this fraction of a mark makes the costliest float element
# ("-1.2345678901234567e-300,") two marks, no denser than the predecessor's
# 262,144-mark bound allowed.
_JSON_NUMBER_CHARS_PER_MARK = 24
MAX_JSON_DEPTH = 128
MAX_JSON_DECODE_COST = 320 * 1024 * 1024
MAX_ADMITTED_EXCHANGE_COST = 512 * 1024 * 1024
MAX_PUBLIC_INGRESS_COST = 192 * 1024 * 1024
_admitted_exchange_cost = 0
_public_ingress_cost = 0
_JSON_RESOURCE_MARKS = re.compile(r'["\\{}\[\],:]')
# Disjoint alternatives consume string content in native, bounded chunks,
# including escaped quotes/backslashes. This only finds the end of a string;
# stdlib JSON still validates escapes and syntax. Never loop per escape in Python.
_JSON_STRING_CONTENT = re.compile(r'(?:[^"\\]+|\\[\s\S])*')
_JSON_WIDE_TEXT = re.compile(r'[^\x00-\xff]')
_JSON_ASTRAL_TEXT = re.compile(r'[^\x00-\uffff]')
_JSON_BMP_TEXT = re.compile(r'[\u0100-\uffff]')
# Upper bounds on what stdlib JSON allocates, for 64-bit CPython 3.12. Every
# block may waste 23 bytes (pymalloc rounds to 16; malloc adds a chunk header).
# A phase's allocations are summed, including transients it frees itself.
# One document's fixed parser and decoder state (scanner, memo dict, buffer
# headers), measured under 1.6 KiB.
_JSON_DOCUMENT_COST = 4096
# A list object (56 + GC rounding, 64) plus the constant part of an append
# reallocation (old and new item arrays coexist; 6 spare slots; two blocks).
_JSON_LIST_COST = 64 + 8 * 5 + 2 * 23
# One element ('[' or ','): its pointer in the old array and in the 9/8 larger
# new array, plus a bare number object (int up to 30 bits or float, 32).
_JSON_ELEMENT_COST = 8 + 9 + 32
# A dict object (64) plus old and new key tables' headers and blocks.
_JSON_DICT_COST = 64 + 2 * 32 + 2 * 23
# One member (':'). A resize from u members coexists the full old table
# (1.5u four-byte indices, u 16-byte entries: 22u) with a new table of fewer
# than 6u indices and 4u entries (88u). The parser's key memo dict grows the
# same way per distinct key. Plus a bare number value (32).
_JSON_MEMBER_COST = 2 * (22 + 88) + 32
# A str object header (56 non-ASCII), terminator (up to 4) and block waste.
_JSON_STRING_COST = 56 + 4 + 23
# A character outside strings: a big int stores under half a byte per digit,
# and parsing a number briefly holds a copy of its text.
_JSON_BARE_CHAR_COST = 2
# Each object costs at most sys.getsizeof plus its blocks' waste: one block, or
# two for a container's separately allocated items/keys.
_RETAINED_BLOCK_SLACK = 23
# A cooperative yield after this many encoded parts or visited values keeps a
# Python walk of a dense document from monopolizing the event loop.
_COOPERATIVE_STEP = 16 * 1024
_EXHAUSTED = object()


def _json_text_width(text: str, start: int = 0, end: int | None = None) -> int:
  end = len(text) if end is None else end
  return (
    4 if _JSON_ASTRAL_TEXT.search(text, start, end) else
    2 if _JSON_WIDE_TEXT.search(text, start, end) else 1
  )


class ServiceExchangeAdmission:
  """Own request/response materialization, retained backlog and response handoff.

  The lexical scan measures resources only; stdlib JSON remains the sole
  syntax/value parser. Incremental stdlib text decoding and fixed-size scan
  chunks avoid constructing either the JSON tree or a full Unicode copy first.
  Estimates deliberately overcount duplicate keys, escapes and shared values.

  The reservation is the sum of three parts, each charged by the phase that
  owns it and never substituted for another: transfer, request and response.
  Transfer charges three copies of serialized bytes (raw, pipe/buffer and
  encoder): of the bytes actually received while a body is read
  (``admit_received``), nothing while the decoded envelope waits for an
  execution slot (``retain``), the whole transfer ceiling from inside that slot
  before serialization or any process starts (``admit_execution``), and the
  actual stdout once the process has answered (``admit_output``). Request and
  response decoded costs are added, never substituted. After request
  materialization its transient Unicode decode copy is gone; retain() charges
  the surviving envelope. The response's decode/encode estimate stays held
  through HTTP rendering or tool handoff.
  """

  def __init__(self, max_bytes: int, *, public_ingress: bool = False):
    self.max_bytes = max_bytes
    self.public_ingress = public_ingress
    self.cost = 0
    self.transfer_cost = 0
    self.request_cost = 0
    self.response_cost = 0

  def __enter__(self):
    return self

  def __exit__(self, *_):
    global _admitted_exchange_cost
    self.finish_ingress()
    _admitted_exchange_cost -= self.cost
    self.cost = 0
    self.transfer_cost = 0
    self.request_cost = 0
    self.response_cost = 0

  def finish_ingress(self):
    """Transfer a completed public read without releasing its total reservation."""
    global _public_ingress_cost
    if self.public_ingress:
      _public_ingress_cost -= self.cost
      self.public_ingress = False

  def _reserve(self, cost: int, *, direction: str = "request"):
    global _admitted_exchange_cost, _public_ingress_cost
    # No await between checking and reserving: all callers run on the owning
    # event loop, including HTTP, policy and agent-tool invocations.
    if _admitted_exchange_cost - self.cost + cost > MAX_ADMITTED_EXCHANGE_COST:
      self._reject(direction, "admission budget is exhausted")
    if self.public_ingress:
      if _public_ingress_cost - self.cost + cost > MAX_PUBLIC_INGRESS_COST:
        self._reject(direction, "public ingress budget is exhausted")
      _public_ingress_cost += cost - self.cost
    _admitted_exchange_cost += cost - self.cost
    self.cost = cost

  def _reject(self, direction: str, detail: str):
    if direction == "execution":
      # Nothing was serialized or started; the same request can succeed once
      # another exchange releases its transfer allowance.
      raise HTTPException(503, "App service is busy; retry shortly.", headers={"Retry-After": "1"})
    raise HTTPException(502 if direction == "response" else 413, f"App service {direction} {detail}.")

  def _hold(
    self, direction: str, *, transfer: int | None = None,
    request: int | None = None, response: int | None = None,
  ):
    transfer = self.transfer_cost if transfer is None else transfer
    request = self.request_cost if request is None else request
    response = self.response_cost if response is None else response
    self._reserve(transfer + request + response, direction=direction)
    self.transfer_cost = transfer
    self.request_cost = request
    self.response_cost = response

  def admit_received(self, size: int):
    """Charge a body read for ``size`` bytes before its buffer grows to them.

    Three copies cover the growing buffer's slack, a reallocation's old and
    new buffers, and the final immutable copy (``read_capped_body``).
    """
    self._hold("request", transfer=3 * size)

  def admit_execution(self, max_bytes: int):
    """Reserve the whole transfer allowance from inside an execution slot.

    Runs before the request is serialized or a process is started, so an
    exhausted budget answers a retryable 503 with no side effect.
    """
    self._hold("execution", transfer=3 * max_bytes)

  def admit_output(self, stdout: bytes):
    """Replace the transfer allowance by the stdout the process produced.

    The serialized request is gone; the response's raw, decoded and rendered
    copies are bounded by three copies of its actual stdout. A finished
    exchange awaiting response handoff therefore no longer holds a ceiling
    sized for output it did not produce.
    """
    self._hold("response", transfer=3 * len(stdout))

  def _decoded_cost(self, cost: int, *, direction: str = "request"):
    if cost > MAX_JSON_DECODE_COST:
      self._reject(direction, "decoded resource limit exceeded")
    self._hold(direction, **{direction: cost})

  async def decode(self, raw: bytes):
    return await self._decode(raw, direction="request")

  async def decode_response(self, raw: bytes):
    return await self._decode(raw, direction="response")

  async def _decode(self, raw: bytes, *, direction: str):
    if not raw and direction == "request":
      return None
    decoder = codecs.getincrementaldecoder(json.detect_encoding(raw))("surrogatepass")
    in_string = False
    pending_escape = False
    string_length = 0
    string_width = 1
    string_escaped = False
    string_content = 0
    string_text = 0
    string_chars = 0
    text_length = 0
    text_width = 1
    value_width = 1
    strings = lists = dicts = commas = members = 0
    text_bmp = False
    depth = 0
    cost = _JSON_DOCUMENT_COST
    for start in range(0, len(raw), 64 * 1024):
      # Bound uninterrupted scan work, not the media allowance. This yields
      # only while the raw body/stdio remains owned by the exchange lease.
      # It also makes scans cancellable without an unkillable worker thread.
      await asyncio.sleep(0)
      chunk = decoder.decode(raw[start:start + 64 * 1024], final=start + 64 * 1024 >= len(raw))
      text_length += len(chunk)
      # Measure Unicode width in C over bounded spans, not one Python
      # iteration per scalar character. A Unicode caption must not inflate
      # the separate ASCII base64 string sharing its final scan chunk.
      text_width = max(text_width, _json_text_width(chunk))
      last = 0
      while last < len(chunk):
        if in_string:
          # A final backslash can escape the first character of the next
          # incremental text chunk (also for UTF-16/32 inputs).
          if pending_escape:
            if chunk[last] == "u":
              string_width = 4
            string_length += 1
            last += 1
            pending_escape = False
            if last == len(chunk):
              break
          end = _JSON_STRING_CONTENT.match(chunk, last).end()
          string_length += end - last
          string_width = max(string_width, _json_text_width(chunk, last, end))
          if chunk.find("\\", last, end) != -1:
            string_escaped = True
            if chunk.find("\\u", last, end) != -1:
              # A \u escape may decode to any scalar, astral included.
              string_width = 4
          if end == len(chunk):
            break
          if chunk[end] == "\\":
            # Only an unpaired terminal backslash stops the content matcher.
            pending_escape = True
            string_escaped = True
            string_length += 1
          else:
            in_string = False
            string_content += _json_string_content(string_length, string_width, string_escaped)
            string_text += string_length * string_width
            string_chars += string_length + 2
            value_width = max(value_width, string_width)
          last = end + 1
          continue
        mark = _JSON_RESOURCE_MARKS.search(chunk, last)
        if mark is None:
          break
        char = mark.group()
        if char == '"':
          in_string = True
          string_length = 0
          string_width = 1
          string_escaped = False
          strings += 1
        elif char == "[" or char == "{":
          depth += 1
          if depth > MAX_JSON_DEPTH:
            self._reject(direction, "structural resource limit exceeded")
          if char == "[":
            lists += 1
          else:
            dicts += 1
        elif char == ",":
          commas += 1
        elif char == ":":
          members += 1
        else:
          depth -= 1
        last = mark.end()
      bare = text_length - string_chars - (string_length + 1 if in_string else 0)
      # Marks only bound work done after the scan, so checking them per
      # bounded chunk still rejects before the stdlib parser runs.
      marks = strings + lists + dicts + commas + members
      if marks + bare // _JSON_NUMBER_CHARS_PER_MARK > MAX_JSON_STRUCTURE:
        self._reject(direction, "structural resource limit exceeded")
      tree = (
        strings * _JSON_STRING_COST
        + lists * _JSON_LIST_COST + dicts * _JSON_DICT_COST
        + (lists + commas) * _JSON_ELEMENT_COST + members * _JSON_MEMBER_COST
        + bare * _JSON_BARE_CHAR_COST
      )
      building, built, widest = string_content, string_text, value_width
      if in_string:
        building += _json_string_content(string_length, string_width, string_escaped)
        built += string_length * string_width
        widest = max(widest, string_width)
      # Phases free their transients before the next starts. json.loads first
      # decodes the whole body into a buffer sized by its byte count; each
      # widening (ASCII -> UCS1 -> UCS2 -> UCS4) copies it into a wider one.
      # Parsing then builds strings beside that text; rendering a response
      # holds only the built strings.
      text_bmp = text_bmp or text_width > 1 and _JSON_BMP_TEXT.search(chunk) is not None
      narrower = 2 if text_width == 4 and text_bmp else 1
      phases = [len(raw) * (text_width + narrower), text_length * text_width + tree + building]
      if direction == "response":
        phases.append(tree + built + _json_render_excess(text_length, bare, widest, len(raw)))
      cost = _JSON_DOCUMENT_COST + max(phases)
      if cost > MAX_JSON_DECODE_COST:
        self._reject(direction, "decoded resource limit exceeded")
    self._decoded_cost(cost, direction=direction)
    # The parse is one uninterruptible call; never run it back to back with
    # the last scan chunk or the caller's next call (a render or retain walk).
    await asyncio.sleep(0)
    value = json.loads(raw, parse_constant=_reject_json_constant)
    await asyncio.sleep(0)
    return value

  async def retain(self, envelope):
    """Charge the decoded envelope as the only request copy awaiting execution.

    This applies the same resource boundary to already-materialized
    tool/policy JSON. An HTTP body's raw bytes are released by then, so a
    queued exchange holds only its actual bounded envelope, not a transfer
    allowance. The walk counts the structural marks of the envelope's compact
    JSON text, the unit the lexical scan bounds, and the envelope itself is
    depth 0, so a body nests exactly as it was sent. It is iterative and
    yields cooperatively, like the scan. A bounded scalar check needs no
    copy, so an oversize string fails before it waits; serialization itself
    runs only after execution admission.
    """
    marks = 0
    cost = _JSON_DOCUMENT_COST
    visited = 0
    pending = [iter((envelope,))]
    while pending:
      item = next(pending[-1], _EXHAUSTED)
      if item is _EXHAUSTED:
        pending.pop()
        continue
      visited += 1
      if visited % _COOPERATIVE_STEP == 0:
        await asyncio.sleep(0)
      children = None
      if isinstance(item, dict):
        marks += max(2 * len(item), 1)
        children = itertools.chain.from_iterable(item.items())
      elif isinstance(item, (list, tuple)):
        marks += max(len(item), 1)
        children = iter(item)
      elif isinstance(item, str):
        marks += 1
        _reject_oversize_json_string(item, self.max_bytes)
      cost += sys.getsizeof(item) + _RETAINED_BLOCK_SLACK * (1 if children is None else 2)
      depth = len(pending) - 1
      if marks > MAX_JSON_STRUCTURE or (children is not None and depth > MAX_JSON_DEPTH):
        self._reject("request", "structural resource limit exceeded")
      if cost > MAX_JSON_DECODE_COST:
        self._reject("request", "decoded resource limit exceeded")
      if children is not None:
        pending.append(children)
    self._hold("request", transfer=0, request=cost)


def _json_render_excess(text_length: int, bare: int, width: int, raw_bytes: int) -> int:
  """What rendering a decoded response can hold beyond its three stdout copies.

  Rendering never adds characters except to floats, up to 3.5 per input
  character ("1e15" -> "1000000000000000.0"); escapes and whitespace only
  shrink. Its text has the widest decoded string's width: the encoder's
  writer overallocates a quarter beside each escaped string copy (2.25x),
  then the finished text coexists with its UTF-8 bytes (up to width + 1).
  """
  rendered = (text_length + bare * 7 // 2) * max(width * 9 // 4, 2 * width + 1)
  return max(0, rendered - 3 * raw_bytes)


def _json_string_content(length: int, width: int, escaped: bool) -> int:
  """Bytes one decoded string can hold, beyond its counted header.

  An unescaped string is an exact substring. An escaped one is built by a
  writer that overallocates a quarter and coexists with its final or widened
  copy (2.25 times, plus a second header).
  """
  if escaped:
    return _JSON_STRING_COST + length * width * 9 // 4
  return length * width


def _reject_json_constant(value: str):
  raise ValueError(f"invalid JSON constant: {value}")


def service_contract(app, *, access: str) -> dict:
  contract = app.capability_contract if isinstance(app.capability_contract, dict) else {}
  service = contract.get("service")
  if not isinstance(service, dict) or service.get("protocol") != "json-v1":
    raise HTTPException(404, "App service not found.")
  accepted_access = service.get("access", "self")
  if access == "public" and accepted_access != "public":
    raise HTTPException(404, "Public app service not found.")
  if access == "apps" and accepted_access not in {"apps", "public"}:
    raise HTTPException(403, "This app service is private.")
  entry = service.get("entry")
  if not isinstance(entry, str) or not entry.endswith(".py"):
    raise HTTPException(503, "Accepted app service declaration is invalid.")
  _accepted_transfer_limits(service)
  return service


def service_max_bytes(service: dict) -> int:
  """The one reviewed serialized request/response ceiling, including old grants."""
  ceiling = accepted_service_max_bytes(service)
  if ceiling is None:
    raise HTTPException(503, "Accepted app service transfer limit is invalid.")
  return ceiling


def _accepted_transfer_limits(service: dict) -> tuple[int, dict[tuple[str, str], int]]:
  """The accepted whole ceiling and its per-route ceilings by (method, canonical path).

  An accepted contract is not trusted to be well formed: one the manifest
  contract could not have produced disables the service rather than silently
  falling back to its wider whole grant.
  """
  ceiling = service_max_bytes(service)
  routes = accepted_service_route_limits(service.get("route_limits", []), ceiling)
  if routes is None:
    raise HTTPException(503, "Accepted app service route limits are invalid.")
  return ceiling, routes


def exchange_max_bytes(service: dict, method, path) -> int:
  """The serialized per-direction ceiling for one exchange with an accepted service.

  The single route-selection seam for HTTP, tool and policy exchanges. An
  exact reviewed (method, canonical path) ceiling narrows the service's whole
  grant; any other exchange, including one whose envelope names no route,
  keeps the whole accepted ceiling. There is no prefix matching and no further
  decoding: `service_route_key` is the only normalization.
  """
  ceiling, routes = _accepted_transfer_limits(service)
  if not isinstance(method, str) or not isinstance(path, str):
    return ceiling
  return routes.get((method, service_route_key(path)), ceiling)


def _reject_oversize_json_string(value: str, max_bytes: int) -> None:
  """Reject a scalar before JSONEncoder can materialize its entire escaped form.

  Structural overhead and the sum of small scalars are still counted by the
  streaming encoder. Six UTF-8 bytes per character is a safe upper bound for
  one JSON string character with ensure_ascii=False.
  """
  if len(value) > max_bytes:
    raise HTTPException(413, "App service request is too large.")
  if len(value) * 6 + 2 <= max_bytes:
    return
  size = 2  # JSON quotes
  for start in range(0, len(value), 64 * 1024):
    # The standard encoder's escaping, applied in bounded slices, has the
    # same result for a string regardless of where it is split.
    part = json.encoder.encode_basestring(value[start:start + 64 * 1024])
    size += len(part[1:-1].encode("utf-8"))
    if size > max_bytes:
      raise HTTPException(413, "App service request is too large.")
  if size > max_bytes:
    raise HTTPException(413, "App service request is too large.")


async def _serialize_request(request_envelope: dict, max_bytes: int) -> bytes:
  """Encode the stdin request without building more than ``max_bytes`` of it.

  Tool and policy calls bypass HTTP body reads, so this bounds their
  serialization too rather than building an arbitrarily large request first.
  The pure-Python encoder yields cooperatively between bounded runs of parts.
  """
  try:
    buffer = io.BytesIO()
    size = 0
    parts = json.JSONEncoder(
      ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).iterencode(request_envelope)
    for count, part in enumerate(parts, 1):
      if count % _COOPERATIVE_STEP == 0:
        await asyncio.sleep(0)
      encoded = part.encode("utf-8")
      size += len(encoded)
      if size > max_bytes:
        raise HTTPException(413, "App service request is too large.")
      buffer.write(encoded)
    return buffer.getvalue()
  except (TypeError, ValueError, RecursionError) as exc:
    raise HTTPException(400, "App service request contains invalid JSON data.") from exc


def request_actor(db, principal, caller=None) -> dict:
  """Who is calling an app's service, as the request's `actor` states it.

  Trusted helpers get "write"; an orphaned or retired delegated identity gets
  "read" defensively so app services do not mistake it for an active helper.
  """
  access = "write"
  if principal.delegation_id is not None:
    delegation = db.get(models.Delegation, principal.delegation_id)
    access = (
      "write" if delegation is not None
      and delegation.scope == "write"
      and delegation.interrupted_at is None
      else "read"
    )
  actor = {
    "scope": principal.scope,
    "app_id": principal.app_id,
    "app_slug": caller.slug if caller is not None else None,
    "delegated": principal.delegation_id is not None,
    "access": access,
  }
  if principal.browser is not None:
    actor.update(browser_grant_id=principal.browser.grant_id,
                 browser_session_id=principal.browser.session_id)
  return actor


def service_entry(app, service: dict) -> Path:
  try:
    root = runtime_root(app)
  except AppliedRuntimeUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  entry = root / service["entry"]
  try:
    if entry.is_symlink() or not entry.is_file() or entry.parent != root:
      raise HTTPException(503, "Accepted app service entry is unavailable.")
  except OSError as exc:
    raise HTTPException(503, "Accepted app service entry is unavailable.") from exc
  return entry


def service_python_env(app, entry: Path) -> Path | None:
  """The accepted service's own Python env, None when it declares none."""
  try:
    return app_python_env.resolve_env(get_settings().data_dir, app.id, entry.parent)
  except app_python_env.PythonEnvUnavailable as exc:
    log.warning("App service %s cannot start: %s", app.slug, exc)
    raise HTTPException(503, str(exc)) from exc


def service_environment(app, owner, service: dict, *, public: bool, browser: BrowserLineage | None = None) -> dict[str, str]:
  """The environment of one invocation; its APP_TOKEN's authority follows the caller.

  A public invocation acts for an anonymous visitor, so its token has the narrow
  public-service scope (auth.create_app_token), which cannot start or drive the
  owner's agents. This bounds what the platform does on the owner's behalf, not
  file access: a service is owner-installed reviewed code.
  """
  allowed = {"PATH", "LANG", "LC_ALL", "TZ", "HOME"}
  if service.get("access", "self") != "public":
    # A service reached only through an authenticated caller may run a provider
    # CLI (Memory's recall navigator does), so it receives the same credential
    # locations as its scheduled job.
    allowed |= {"DATA_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME"}
  env = {key: value for key, value in os.environ.items() if key in allowed}
  settings = get_settings()
  env.update({
    "APP_ID": str(app.id),
    "APP_SLUG": str(app.slug),
    "APP_STORAGE_DIR": str(Path(settings.data_dir) / "apps" / str(app.id)),
    "API_BASE_URL": settings.api_base_url,
    "INSTANCE_DOMAIN": settings.domain,
    "INSTANCE_ORIGIN": settings.frontend_origin.rstrip("/"),
    "APP_TOKEN": auth.create_app_token(
      app.id,
      owner.username,
      owner.token_epoch,
      app_nonce=app.token_nonce,
      expires_delta=timedelta(minutes=5),
      service="public" if public else "private",
      browser=browser,
    ),
  })
  return env


_read_bounded = service_preload.read_bounded


async def _write_request(stream, body: bytes) -> None:
  stream.write(body)
  await stream.drain()
  stream.close()
  await stream.wait_closed()


async def _stop_process(process, *tasks) -> None:
  try:
    os.killpg(process.pid, signal.SIGKILL)
  except ProcessLookupError:
    pass
  if not tasks:
    # Admission cancellation has not installed pipe readers yet. Drain the
    # now-stopped child as well as reaping it; wait() alone can block on a full
    # asyncio pipe buffer even after SIGKILL.
    await process.communicate()
    return
  await process.wait()
  for task in tasks:
    task.cancel()
  await asyncio.gather(*tasks, return_exceptions=True)


def _response_headers(value, *, public: bool) -> dict[str, str]:
  if value is None:
    return {}
  if not isinstance(value, dict) or len(value) > 16:
    raise ValueError("response headers must be a bounded object")
  headers: dict[str, str] = {}
  for name, raw in value.items():
    lower = name.lower() if isinstance(name, str) else ""
    if lower not in _SERVICE_RESPONSE_HEADERS:
      continue
    if not isinstance(raw, str) or len(raw) > 4096 or "\r" in raw or "\n" in raw:
      raise ValueError("response contains an invalid header")
    if lower == "cache-control" and not public:
      directives = {part.split("=", 1)[0].strip().lower() for part in raw.split(",")}
      if directives & _SHARED_CACHE_DIRECTIVES and not directives & {"private", "no-store"}:
        # An owner-authenticated response must never be stored by a shared cache.
        continue
    headers[name] = raw
  return headers


async def _run_spawned(
  python: str, entry: Path, environment: dict[str, str], request_bytes: bytes,
  timeout_seconds: float, max_stdout: int = MAX_RESPONSE_BYTES,
) -> tuple[bytes, bytes, int]:
  """Run one request in a fresh interpreter; return (stdout, stderr, exit code)."""
  try:
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
      python,
      str(entry),
      stdin=asyncio.subprocess.PIPE,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
      cwd=str(entry.parent),
      env=environment,
      start_new_session=True,
    ))
    try:
      process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
      # Admission can be cancelled after the OS child exists but before
      # asyncio returns its handle. Finish recovery before releasing the
      # runtime pin, even if shutdown cancels this request again.

      async def recover_spawn() -> None:
        try:
          process = await asyncio.shield(spawn)
        except OSError:
          pass
        else:
          await _stop_process(process)

      recovery = asyncio.create_task(recover_spawn())
      while not recovery.done():
        try:
          await asyncio.shield(recovery)
        except asyncio.CancelledError:
          continue
      raise
  except OSError as exc:
    raise HTTPException(502, "App service could not start.") from exc
  assert process.stdin is not None
  assert process.stdout is not None
  assert process.stderr is not None
  stdout_task = asyncio.create_task(_read_bounded(process.stdout, max_stdout))
  stderr_task = asyncio.create_task(_read_bounded(process.stderr, MAX_ERROR_BYTES))
  write_task = asyncio.create_task(_write_request(process.stdin, request_bytes))
  try:
    _written, stdout, stderr, returncode = await asyncio.wait_for(
      asyncio.gather(write_task, stdout_task, stderr_task, process.wait()),
      timeout=timeout_seconds,
    )
  except (TimeoutError, ValueError) as exc:
    await _stop_process(process, write_task, stdout_task, stderr_task)
    raise HTTPException(503, "App service exceeded its execution limits.")
  except OSError as exc:
    await _stop_process(process, write_task, stdout_task, stderr_task)
    raise HTTPException(502, "App service failed before accepting its request.") from exc
  except asyncio.CancelledError:
    await _stop_process(process, write_task, stdout_task, stderr_task)
    raise
  try:
    os.killpg(process.pid, signal.SIGKILL)
  except ProcessLookupError:
    pass
  return stdout, stderr, returncode


# Invocation tasks, not a second job queue: existing subprocess cancellation owns
# process cleanup. A revoked grant cancels only its attributed in-flight calls.
_browser_calls: dict[str, set[asyncio.Task]] = {}


def _actor_browser(actor: dict) -> BrowserLineage | None:
  return BrowserLineage.of(actor.get("browser_grant_id"), actor.get("browser_session_id"))


def _validate_browser_call(owner, browser: BrowserLineage | None):
  if browser is not None:
    from app.database import SessionLocal
    with SessionLocal() as db:
      require_live(db, browser, owner.id)


def browser_grant_has_active_calls(grant_id: str) -> bool:
  """Whether attributed invocation cleanup is still outstanding."""
  return any(not task.done() for task in _browser_calls.get(grant_id, ()))


async def cancel_browser_grant_calls(grant_id: str) -> None:
  tasks = tuple(_browser_calls.get(grant_id, ()))
  for task in tasks:
    if task is not asyncio.current_task():
      task.cancel()
  await asyncio.gather(*(task for task in tasks if task is not asyncio.current_task()), return_exceptions=True)


async def invoke_service(
  app, owner, request_envelope: dict, *,
  timeout_seconds: float = SERVICE_TIMEOUT_SECONDS,
  lane: str | None = None,
  admission: ServiceExchangeAdmission | None = None,
) -> tuple[int, object, dict[str, str], str | None]:
  service = service_contract(app, access="public" if request_envelope.get("public") is True else "self")
  # HTTP callers admit their body read under the same route ceiling.
  boundary = (
    contextlib.nullcontext(admission) if admission is not None
    else ServiceExchangeAdmission(exchange_max_bytes(
      service, request_envelope.get("method"), request_envelope.get("path"),
    ))
  )
  browser = _actor_browser(request_envelope.get("actor") or {})
  grant_id = browser.grant_id if browser is not None else None
  try:
    with boundary as admitted:
      # The envelope, with its metadata, is the only request copy retained
      # while queued; non-HTTP callers get the same structural boundary.
      await admitted.retain(request_envelope)
      return await _invoke_admitted_service(
        app, owner, request_envelope, timeout_seconds=timeout_seconds, lane=lane,
        admission=admitted,
      )
  finally:
    # Attribution includes cooperative response scanning, not just execution.
    # Revocation must still cancel a result that has not been handed off.
    if grant_id is not None:
      calls = _browser_calls.get(grant_id)
      if calls is not None:
        calls.discard(asyncio.current_task())
        if not calls:
          _browser_calls.pop(grant_id, None)


async def _invoke_admitted_service(
  app, owner, request_envelope: dict, *,
  timeout_seconds: float = SERVICE_TIMEOUT_SECONDS,
  lane: str | None = None,
  admission: ServiceExchangeAdmission,
) -> tuple[int, object, dict[str, str], str | None]:
  public = request_envelope.get("public") is True
  service = service_contract(app, access="public" if public else "self")
  # One ceiling bounds both directions: request serialization, the stdout of
  # either execution backend, and the decoded binary response. It never
  # exceeds what admission reserved for.
  max_bytes = min(admission.max_bytes, exchange_max_bytes(
    service, request_envelope.get("method"), request_envelope.get("path"),
  ))
  # A service owns its persistence semantics. Serialize one app's private
  # requests so simple file-backed services do not need a platform-specific
  # lock API, and serialize its public requests on a separate lane. A private
  # federation write may synchronously cause the peer to call this instance's
  # public service (for example, to verify the sender's identity). Sharing one
  # lane for both directions deadlocks that callback behind the write waiting
  # for it. Public services still own their own file/SQLite locking where the
  # two lanes can touch the same state.
  #
  # Agent tool calls use a third lane that is not serialized per app: one
  # agent's long tool call (a Memory search can take minutes) must not stall
  # the app's own screen or another chat's call. A tool owns its concurrency,
  # as a public service already must.
  if lane is None:
    lane = "public" if public else "private"
  slot = (
    contextlib.nullcontext() if lane == "tools"
    else _app_slots.setdefault((app.id, lane), asyncio.Semaphore(1))
  )
  # Backlog for one app must not reserve all platform execution capacity while
  # waiting for that app's serialized request. Count only executable requests.
  # Queued requests already own an accepted revision. Pin before admission so
  # pruning or a migration drain cannot overlook a request waiting to run.
  browser = _actor_browser(request_envelope.get("actor") or {})
  _validate_browser_call(owner, browser)
  grant_id = browser.grant_id if browser is not None else None
  task = asyncio.current_task()
  pin = hold_runtime(app.id)
  if grant_id is not None:
    _browser_calls.setdefault(grant_id, set()).add(task)
  try:
    async with slot, _global_slots[lane]:
      _validate_browser_call(owner, browser)
      # Waiting held only the retained envelope. The whole transfer allowance
      # is reserved here, before serialization or any process start, so
      # exhaustion is a side-effect-free retryable 503.
      admission.admit_execution(max_bytes)
      request_bytes = await _serialize_request(request_envelope, max_bytes)
      entry = service_entry(app, service)
      python_env = service_python_env(app, entry)
      python = app_python_env.python_for(python_env)
      # The preload host and its request children inherit this PATH too.
      environment = app_python_env.activated_environment(
        service_environment(
          app, owner, service, public=public,
          browser=browser,
        ), python_env,
      )
      outcome = None
      host = service_preload.ready_host(app, python, entry, environment)
      if host is not None:
        try:
          outcome = await service_preload.run(
            host, environment, request_bytes, timeout_seconds=timeout_seconds,
            max_stdout=max_bytes, max_stderr=MAX_ERROR_BYTES,
          )
        except service_preload.PreloadUnavailable:
          pass
        except (TimeoutError, ValueError) as exc:
          raise HTTPException(503, "App service exceeded its execution limits.") from exc
        except OSError as exc:
          raise HTTPException(502, "App service failed before accepting its request.") from exc
      if outcome is None:
        outcome = await _run_spawned(
          python, entry, environment, request_bytes, timeout_seconds, max_bytes,
        )
      del request_bytes
      stdout, stderr, returncode = outcome
      admission.admit_output(stdout)
      if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
        log.warning("App service %s failed: %s", app.slug, detail or "no diagnostics")
        raise HTTPException(502, "App service failed.")
  finally:
    pin.close()
  try:
    response = await admission.decode_response(stdout)
  except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
    raise HTTPException(502, "App service returned invalid JSON.") from exc
  _validate_browser_call(owner, browser)
  if not isinstance(response, dict) or set(response) - {
    "status", "body", "body_base64", "headers", "media_type",
  }:
    raise HTTPException(502, "App service returned an invalid response envelope.")
  status = response.get("status", 200)
  if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
    raise HTTPException(502, "App service returned an invalid status.")
  if status >= 500 and stderr:
    detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
    if detail:
      # A handled app failure still exits its adapter successfully, so preserve
      # its bounded diagnostics without exposing them in the HTTP response.
      log.warning("App service %s returned %d: %s", app.slug, status, detail)
  try:
    headers = _response_headers(
      response.get("headers"), public=bool(request_envelope.get("public")),
    )
  except ValueError as exc:
    raise HTTPException(502, str(exc)) from exc
  encoded = response.get("body_base64")
  media_type = response.get("media_type")
  if encoded is not None:
    if response.get("body") is not None or not isinstance(encoded, str):
      raise HTTPException(502, "App service returned an invalid binary response.")
    if not isinstance(media_type, str) or _MEDIA_TYPE.fullmatch(media_type) is None:
      raise HTTPException(502, "App service returned an invalid media type.")
    try:
      body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
      raise HTTPException(502, "App service returned invalid binary data.") from exc
    if len(body) > max_bytes:
      raise HTTPException(502, "App service returned too much binary data.")
    return status, body, headers, media_type
  if media_type is not None:
    raise HTTPException(502, "App service returned an invalid media type.")
  return status, response.get("body"), headers, None


async def invoke_policy(app, owner, path: str, body: dict) -> dict:
  """Invoke one authenticated app-owned policy and normalize its error shape."""
  status, result, _headers, media_type = await invoke_service(app, owner, {
    "schema": 1,
    "method": "POST",
    "path": path,
    "query": {},
    "headers": {},
    "body": body,
    "public": False,
    "actor": {"scope": "platform"},
  })
  if media_type is not None:
    raise HTTPException(502, "App policy returned binary data.")
  if status >= 400:
    detail = result.get("detail") if isinstance(result, dict) else None
    raise HTTPException(status, detail or "App policy rejected the request.")
  if not isinstance(result, dict):
    raise HTTPException(502, "App policy returned an invalid result.")
  return result
