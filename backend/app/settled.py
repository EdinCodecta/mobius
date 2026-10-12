"""Await work to its end even when the awaiting caller is cancelled.

Cancelling an ``asyncio.to_thread`` await does not stop its thread, and
cancelling a coroutine mid-way can skip its rollback. A caller that holds a
lock, or that rolls back once the work fails, must therefore not unwind until
the work has settled. ``settle`` is the one place that rule lives:

* cancellation always wins: a cancelled caller re-raises its own original
  ``CancelledError`` (message included) once the work has ended, whatever the
  work's outcome was;
* the work's outcome then has no taker, so a failure is logged once, unless
  ``abandoned`` is given, in which case it receives the finished task instead;
* with ``admitted``, a cancellation that lands before the work sets it really
  cancels the work, because nothing has started that needs finishing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

log = logging.getLogger("mobius.settled")

T = TypeVar("T")


async def settle(
  work: Awaitable[T],
  *,
  admitted: asyncio.Event | None = None,
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
      if admitted is not None and not admitted.is_set():
        task.cancel()  # Not admitted yet: nothing to finish, so stop it.
  if cancel is None:
    return task.result()
  if not task.cancelled():
    if abandoned is not None:
      abandoned(task)
    elif task.exception() is not None:
      log.error(
        "Work failed after its caller was cancelled", exc_info=task.exception(),
      )
  raise cancel


async def run_settled(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
  """Run ``fn`` in a worker thread that finishes before its caller unwinds."""
  return await settle(asyncio.to_thread(fn, *args, **kwargs))
