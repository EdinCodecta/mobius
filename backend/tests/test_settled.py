"""The shared settle primitive: cancellation waits for the work, then wins."""

import asyncio
import logging

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
