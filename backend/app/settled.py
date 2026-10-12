"""Await work to its end even when the awaiting caller is cancelled.

Cancelling an ``asyncio.to_thread`` await does not stop its thread, and
cancelling a coroutine mid-way can skip its rollback. A caller that holds a
lock, or that rolls back once the work fails, must therefore not unwind until
the work has settled. ``settle`` is the one place that rule lives:

* cancellation always wins: a cancelled caller re-raises its own original
  ``CancelledError`` (message included) once the work has ended, whatever the
  work's outcome was;
* the work's outcome then has no taker, so a failure is logged once
  (``log_abandoned_failure``), unless ``abandoned`` is given, in which case it
  receives the finished task instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi import HTTPException

log = logging.getLogger("mobius.settled")

T = TypeVar("T")


async def settle(
  work: Awaitable[T],
  *,
  abandoned: Callable[[asyncio.Future[T]], Any] | None = None,
) -> T:
  """Await ``work`` to its end; a cancelled caller re-raises only once it settles."""
  task = asyncio.ensure_future(work)
  cancel: asyncio.CancelledError | None = None
  while not task.done():
    try:
      await asyncio.wait({task})
    except asyncio.CancelledError as exc:
      cancel = cancel or exc
  if cancel is None:
    return task.result()
  if not task.cancelled():
    if abandoned is None:
      log_abandoned_failure(task)
    else:
      try:
        abandoned(task)
      except Exception:  # The cancellation still wins.
        log.exception("Cleaning up work abandoned by a cancelled caller failed")
  raise cancel


async def run_settled(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
  """Run ``fn`` in a worker thread that finishes before its caller unwinds."""
  what = getattr(fn, "__qualname__", None) or repr(fn)
  return await settle(
    asyncio.to_thread(fn, *args, **kwargs),
    abandoned=lambda task: log_abandoned_failure(task, what),
  )


def log_abandoned_failure(task: asyncio.Future, what: str = "Work") -> None:
  """Log the failure of work whose caller was cancelled, if it failed.

  An ``HTTPException`` is an expected refusal that the work already handled,
  so it goes to info; anything else is an error with its traceback.
  """
  error = task.exception()
  if isinstance(error, HTTPException):
    log.info("%s refused after its caller was cancelled: %s", what, error.detail)
  elif error is not None:
    log.error("%s failed after its caller was cancelled", what, exc_info=error)
