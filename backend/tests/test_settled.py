"""The shared settle primitive: cancellation waits for the work, then wins."""

import asyncio
import logging

import pytest

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
    return await cancel_while_worker_blocked(
      lock, _under_lock(lock, lambda: settled.run_settled(worker)), worker,
    )

  outcome = asyncio.run(run())
  assert isinstance(outcome, asyncio.CancelledError)
  errors = [r for r in caplog.records if r.name == "mobius.settled"]
  assert len(errors) == 1 and errors[0].levelno == logging.ERROR
  assert "disk full" in caplog.text


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


def test_cancel_before_admission_cancels_the_work():
  async def run():
    admitted, ran = asyncio.Event(), []
    lock = asyncio.Lock()

    async def body():
      async with lock:
        admitted.set()
        ran.append(True)

    await lock.acquire()  # Someone else holds the lock: not admitted yet.
    task = asyncio.create_task(settled.settle(body(), admitted=admitted))
    await asyncio.sleep(.02)
    task.cancel("gone")
    with pytest.raises(asyncio.CancelledError, match="gone"):
      await task
    lock.release()
    await asyncio.sleep(.02)
    assert not ran and not admitted.is_set()

  asyncio.run(run())


def test_cancel_after_admission_finishes_the_work_first():
  async def run():
    admitted, worker = asyncio.Event(), BlockedWorker(lambda: "done")
    lock = asyncio.Lock()

    async def body():
      async with lock:
        admitted.set()
        return await asyncio.to_thread(worker)

    async def operation():
      return await settled.settle(body(), admitted=admitted)

    outcome = await cancel_while_worker_blocked(lock, operation, worker)
    assert isinstance(outcome, asyncio.CancelledError)

  asyncio.run(run())


def test_without_a_cancel_the_result_or_exception_passes_through():
  async def run():
    assert await settled.run_settled(lambda a, *, b: a + b, 1, b=2) == 3

    def fail():
      raise ValueError("bad input")

    with pytest.raises(ValueError, match="bad input"):
      await settled.run_settled(fail)

  asyncio.run(run())
