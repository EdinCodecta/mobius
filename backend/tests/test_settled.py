"""The shared settle primitive: cancellation waits for the work, then wins."""

import ast
import asyncio
import logging
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import settled
from tests.cancel_helpers import BlockedWorker, cancel_while_worker_blocked


def _under_lock(lock, work):
  async def operation():
    async with lock:
      return await work()
  return operation


def test_double_cancel_holds_the_lock_until_the_worker_exits_then_keeps_its_message():
  async def run():
    lock, worker = asyncio.Lock(), BlockedWorker(lambda: "done")
    outcome = await cancel_while_worker_blocked(
      lock, _under_lock(lock, lambda: settled.run_settled(worker)), worker,
      cancel=lambda task: task.cancel("client gone"),
    )
    assert isinstance(outcome, asyncio.CancelledError)
    assert outcome.args == ("client gone",)

  asyncio.run(run())


def test_plain_to_thread_releases_the_lock_under_a_live_worker():
  """The hazard ``settle`` exists for, kept as a guard on the test itself."""
  async def run():
    lock, worker = asyncio.Lock(), BlockedWorker()
    task = asyncio.create_task(_under_lock(lock, lambda: asyncio.to_thread(worker))())
    assert await asyncio.to_thread(worker.entered.wait, 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not lock.locked() and not worker.exited.is_set()
    worker.release.set()
    assert await asyncio.to_thread(worker.exited.wait, 5)

  asyncio.run(run())


def test_timeout_raises_only_after_the_worker_exits():
  async def run():
    lock, worker = asyncio.Lock(), BlockedWorker()

    async def timed():
      async with lock:
        async with asyncio.timeout(.05):
          await settled.run_settled(worker)

    task = asyncio.create_task(timed())
    assert await asyncio.to_thread(worker.entered.wait, 5)
    await asyncio.sleep(.15)
    assert not task.done() and lock.locked()
    worker.release.set()
    with pytest.raises(TimeoutError):
      await task
    assert worker.exited.is_set() and not lock.locked()

  asyncio.run(run())


def test_failure_after_cancel_is_logged_once_and_the_cancel_is_raised(caplog):
  def fail():
    raise RuntimeError("disk full")

  async def run():
    lock, worker = asyncio.Lock(), BlockedWorker(fail)

    def write_listing():
      return worker()

    return await cancel_while_worker_blocked(
      lock, _under_lock(lock, lambda: settled.run_settled(write_listing)), worker,
    )

  outcome = asyncio.run(run())
  assert isinstance(outcome, asyncio.CancelledError)
  errors = [r for r in caplog.records if r.name == "mobius.settled"]
  assert len(errors) == 1 and errors[0].levelno == logging.ERROR
  # The log names what failed.
  assert "write_listing failed after its caller was cancelled" in caplog.text
  assert "disk full" in caplog.text


def test_a_failing_abandoned_hook_is_logged_and_the_cancel_still_wins(caplog):
  def abandoned(task):
    raise RuntimeError("release failed")

  async def run():
    lock, worker = asyncio.Lock(), BlockedWorker()
    return await cancel_while_worker_blocked(
      lock,
      _under_lock(lock, lambda: settled.settle(
        asyncio.to_thread(worker), abandoned=abandoned,
      )),
      worker,
      cancel=lambda task: task.cancel("client gone"),
    )

  outcome = asyncio.run(run())
  assert isinstance(outcome, asyncio.CancelledError)
  assert outcome.args == ("client gone",)
  errors = [r for r in caplog.records if r.name == "mobius.settled"]
  assert len(errors) == 1 and "release failed" in caplog.text


@pytest.mark.parametrize("fails", [False, True])
def test_abandoned_receives_the_outcome_instead_of_the_log(caplog, fails):
  def outcome():
    if fails:
      raise RuntimeError("refused")
    return "written"

  seen = []

  def abandoned(task):
    seen.append(repr(task.exception()) if task.exception() else task.result())

  async def run():
    lock, worker = asyncio.Lock(), BlockedWorker(outcome)
    return await cancel_while_worker_blocked(
      lock,
      _under_lock(lock, lambda: settled.settle(
        asyncio.to_thread(worker), abandoned=abandoned,
      )),
      worker,
    )

  assert isinstance(asyncio.run(run()), asyncio.CancelledError)
  assert seen == ["RuntimeError('refused')" if fails else "written"]
  assert not [r for r in caplog.records if r.name == "mobius.settled"]


def test_a_refusal_after_cancel_is_logged_at_info_without_a_traceback(caplog):
  def refuse():
    raise HTTPException(409, "The pending update changed.")

  async def run():
    lock, worker = asyncio.Lock(), BlockedWorker(refuse)

    def park_pending_update():
      return worker()

    return await cancel_while_worker_blocked(
      lock, _under_lock(lock, lambda: settled.run_settled(park_pending_update)),
      worker,
    )

  caplog.set_level(logging.INFO, logger="mobius.settled")
  assert isinstance(asyncio.run(run()), asyncio.CancelledError)
  records = [r for r in caplog.records if r.name == "mobius.settled"]
  assert [r.levelno for r in records] == [logging.INFO]
  assert records[0].exc_info is None
  assert "park_pending_update refused after its caller was cancelled" in caplog.text


def test_without_a_cancel_the_result_or_exception_passes_through():
  async def run():
    assert await settled.run_settled(lambda a, *, b: a + b, 1, b=2) == 3

    def fail():
      raise ValueError("bad input")

    with pytest.raises(ValueError, match="bad input"):
      await settled.run_settled(fail)

  asyncio.run(run())


async def _teardown_under_cancel(raise_first, caplog_logger, handled_above=False):
  """A finally whose first step is cancelled twice; return (task, ran)."""
  ran, started, finish = [], asyncio.Event(), asyncio.Event()

  async def first():
    started.set()
    await finish.wait()
    ran.append("first")

  async def second():
    ran.append("second")
    raise RuntimeError("second step broke")

  async def turn():
    try:
      if raise_first:
        raise asyncio.CancelledError("original")
    finally:
      teardown = settled.Teardown(caplog_logger, "chat_id=t")
      await teardown.step(first(), "first step")
      ok = await teardown.step(second(), "second step")
      ran.append(f"second ok={ok}")
      teardown.finish()

  async def turn_while_a_caller_handles_a_cancel():
    try:
      raise asyncio.CancelledError("handled above")
    except asyncio.CancelledError:
      await turn()

  task = asyncio.create_task(
    turn_while_a_caller_handles_a_cancel() if handled_above else turn(),
  )
  await started.wait()
  task.cancel("held one")
  await asyncio.sleep(0)
  task.cancel("held two")
  await asyncio.sleep(0)
  assert not task.done() and ran == []
  finish.set()
  try:
    await task
  except asyncio.CancelledError as exc:
    return exc, ran
  raise AssertionError("teardown swallowed the cancellation")


def test_teardown_holds_a_cancel_until_every_step_ran_then_raises_it(caplog):
  outcome, ran = asyncio.run(
    _teardown_under_cancel(False, logging.getLogger("mobius.test_teardown")),
  )
  assert outcome.args == ("held one",)
  assert ran == ["first", "second", "second ok=False"]
  assert "second step failed during teardown chat_id=t" in caplog.text


def test_teardown_lets_the_original_unwinding_cancel_propagate():
  outcome, ran = asyncio.run(
    _teardown_under_cancel(True, logging.getLogger("mobius.test_teardown")),
  )
  assert outcome.args == ("original",)
  assert ran == ["first", "second", "second ok=False"]


def test_teardown_is_not_fooled_by_a_cancel_handled_further_up_the_stack():
  """Only an exception unwinding the teardown's own block counts as unwinding."""
  outcome, ran = asyncio.run(_teardown_under_cancel(
    False, logging.getLogger("mobius.test_teardown"), handled_above=True,
  ))
  assert outcome.args == ("held one",)
  assert ran == ["first", "second", "second ok=False"]


_READ_ONLY = "# unsettled: read-only"
# Settled by its caller: ``apply_platform_update`` waits out an admitted Apply.
_SETTLED_ELSEWHERE = {("platform_update.py", "_apply_platform_update_sync")}


def _locks(expr) -> bool:
  """Whether an ``async with`` item takes a filesystem, transition or Apply lock."""
  if isinstance(expr, ast.Name):
    return expr.id == "_APPLY_LOCK"
  if not isinstance(expr, ast.Call):
    return False
  func = expr.func
  if isinstance(func, ast.Attribute):
    if isinstance(func.value, ast.Name) and func.value.id == "fs_locks":
      return True
    name = func.attr
  elif isinstance(func, ast.Name):
    name = func.id
  else:
    return False
  # Also the route-local wrappers around them, such as a stack's source locks.
  return name == "get_transition_lock" or name.endswith(("_lock", "_locks"))


def _held(node: ast.AsyncWith) -> bool:
  if any(_locks(item.context_expr) for item in node.items):
    return True
  # An AsyncExitStack that enters such locks one by one.
  return any(
    isinstance(call, ast.Call)
    and isinstance(call.func, ast.Attribute)
    and call.func.attr == "enter_async_context"
    and call.args and _locks(call.args[0])
    for statement in node.body for call in ast.walk(statement)
  )


def _plain_to_thread_calls(body):
  """Yield lexical ``asyncio.to_thread`` calls, not those in nested functions."""
  pending = list(body)
  while pending:
    node = pending.pop()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
      continue
    if (
      isinstance(node, ast.Call)
      and isinstance(node.func, ast.Attribute)
      and node.func.attr == "to_thread"
      and isinstance(node.func.value, ast.Name)
      and node.func.value.id == "asyncio"
    ):
      yield node
    pending.extend(ast.iter_child_nodes(node))


def test_lock_held_thread_work_is_settled_or_marked_read_only():
  """Under a lock, a cancelled caller must not unwind while its thread writes.

  Plain ``asyncio.to_thread`` releases the lock on cancel while the thread
  runs on, so lock-held thread work goes through ``settled``. Read-only work
  may stay plain, marked ``# unsettled: read-only`` on the call's line.
  """
  app_root = Path(__file__).resolve().parents[1] / "app"
  offenders = []
  for path in sorted(app_root.rglob("*.py")):
    source = path.read_text()
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
      if not isinstance(node, ast.AsyncWith) or not _held(node):
        continue
      for call in _plain_to_thread_calls(node.body):
        callee = call.args[0] if call.args else None
        name = getattr(callee, "id", getattr(callee, "attr", ""))
        if (path.name, name) in _SETTLED_ELSEWHERE:
          continue
        if _READ_ONLY not in lines[call.lineno - 1]:
          offenders.append(f"{path.relative_to(app_root)}:{call.lineno}")
  assert not sorted(set(offenders)), (
    "Lock-held asyncio.to_thread must use settled.run_settled, or be marked "
    f"{_READ_ONLY!r} when the thread only reads: {sorted(set(offenders))}"
  )
