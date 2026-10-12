"""Hot paths caught stalling the event loop keep their database work off it.

The stall recorder caught the event loop frozen for seconds inside these
paths' database reads while the disk was saturated. On the loop, one slow read
freezes every request and stream; in a worker thread it only slows its own.
"""

import asyncio

from app import delegations


def _running_on_event_loop() -> bool:
  try:
    asyncio.get_running_loop()
  except RuntimeError:
    return False
  return True


def test_parent_wake_reads_the_parent_off_the_event_loop(monkeypatch):
  seen = []

  def plan(parent_chat_id, source_work_id):
    seen.append(_running_on_event_loop())
    return None

  monkeypatch.setattr(delegations, "_plan_parent_wake", plan)

  assert asyncio.run(
    delegations._deliver_parent_wake_once("parent-chat", "source-run"),
  ) is False
  assert seen == [False]


def test_parent_wake_starts_the_planned_continuation_on_the_loop(monkeypatch):
  import app.chat as chat_mod
  import app.chat_start as chat_start

  plan = delegations._ParentWakePlan(
    root_run_id="root", run_token="token", source_work_id="source-run",
    activity_id="activity",
  )
  monkeypatch.setattr(delegations, "_plan_parent_wake", lambda *_a: plan)
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _chat_id: False)
  started = []

  async def start(**kwargs):
    started.append((_running_on_event_loop(), kwargs))
    return True

  monkeypatch.setattr(chat_start, "start_programmatic_activity_continuation", start)

  assert asyncio.run(
    delegations._deliver_parent_wake_once("parent-chat", "source-run"),
  ) is True
  assert started == [(True, {
    "chat_id": "parent-chat", "root_run_id": "root", "run_token": "token",
    "source_work_id": "source-run", "activity_id": "activity",
    "_transition_lock_held": True,
  })]


def test_parent_wake_skips_the_history_scan_without_a_running_run(
  chat, db, monkeypatch,
):
  # The pre-upgrade carrier it looks for always owns a running ChatRun in the
  # parent; without one, rereading the whole transcript every recovery pass
  # is pure cost that grows with the chat.
  from app import models

  read = []
  real_history = delegations.transcript_rows.history

  def history(parent):
    read.append(parent.id)
    return real_history(parent)

  monkeypatch.setattr(delegations.transcript_rows, "history", history)

  assert delegations._plan_parent_wake(chat.id, "source-run") is None
  assert read == []

  db.add(models.ChatRun(
    id="orphan-run", root_run_id="orphan-run", chat_id=chat.id,
    status="running", provider="claude", provider_execution_admitted=False,
  ))
  db.commit()

  assert delegations._plan_parent_wake(chat.id, "source-run") is None
  assert read == [chat.id]


def test_turn_start_reads_run_off_the_event_loop(monkeypatch):
  # Each of these froze the loop at turn start in the stall recorder: the
  # app-provider declarations, the peer roster and inbox, the helper status
  # hint, the recency transcript scan and the app activity manifests.
  from app import agent_activity_provider, agent_coordination, providers
  import app.chat as chat_mod
  from tests.test_terminal_completion import (
    _patch_claude_runner, _run_real_chat, _seed_chat, _seed_owner_and_creds,
  )

  seen = []

  def record(module, name):
    real = getattr(module, name)

    def wrapper(*args, **kwargs):
      seen.append((name, _running_on_event_loop()))
      return real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)

  record(providers, "_read_app_model_declarations")
  record(agent_coordination, "build_coordination_context_delivery_off_loop")
  record(delegations, "active_parent_context_off_loop")
  record(chat_mod, "_last_user_message_elapsed_off_loop")
  record(agent_activity_provider, "resolve_agent_activity_binding_off_loop")
  monkeypatch.setattr(providers, "_app_provider_sync_at", 0.0)

  _seed_owner_and_creds()
  _seed_chat(
    "turn-start-off-loop",
    messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[], running="running", run_token="rt-turn-start-off-loop",
  )
  _patch_claude_runner(monkeypatch)
  chat_mod.mark_starting("turn-start-off-loop")
  _run_real_chat(
    "turn-start-off-loop", run_token="rt-turn-start-off-loop",
    run_gen=chat_mod.current_run_generation("turn-start-off-loop"),
  )

  # The turn's first provider read is the off-loop refresh; later synchronous
  # get_provider calls reuse it inside the throttle window.
  assert seen[0] == ("_read_app_model_declarations", False)
  assert {name for name, _ in seen} == {
    "_read_app_model_declarations",
    "build_coordination_context_delivery_off_loop",
    "active_parent_context_off_loop",
    "_last_user_message_elapsed_off_loop",
    "resolve_agent_activity_binding_off_loop",
  }
  assert all(
    on_loop is False for name, on_loop in seen
    if name != "_read_app_model_declarations"
  )


def test_off_loop_peer_roster_uses_liveness_captured_on_the_loop(
  db, monkeypatch,
):
  # The runner registry belongs to the loop; the worker gets a snapshot.
  from app import agent_coordination
  from app.runner_registry import registry
  from tests.test_agent_coordination import _network_fixture

  chats, _ = _network_fixture(db)
  alive = frozenset(registry.all_alive_chat_ids())
  expected = agent_coordination.build_coordination_context_delivery(
    db, chats["scout"].id, "scout-run",
  )

  def forbidden(_chat_id):
    raise AssertionError("worker read the loop-owned runner registry")

  monkeypatch.setattr(registry, "is_alive", forbidden)
  delivery = asyncio.run(asyncio.to_thread(
    agent_coordination.build_coordination_context_delivery_off_loop,
    chats["scout"].id, "scout-run", alive,
  ))

  assert delivery == expected
  assert chats["builder"].id in delivery.text


def test_model_pick_during_turn_start_reads_is_not_clobbered(monkeypatch):
  # Turn start now awaits worker reads before its first-send settings
  # snapshot; a picker PATCH that lands meanwhile must win.
  from app import models
  from app.database import SessionLocal
  import app.chat as chat_mod
  from tests.test_terminal_completion import (
    _patch_claude_runner, _run_real_chat, _seed_chat, _seed_owner_and_creds,
  )

  chat_id = "turn-start-pick"
  _seed_owner_and_creds()
  _seed_chat(
    chat_id, messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[], running="running", run_token="rt-turn-start-pick",
  )
  with SessionLocal() as db:
    db.get(models.Chat, chat_id).agent_settings_json = None
    db.commit()
  picked = {"model": "claude-opus-4-8", "effort": "low"}
  real_elapsed = chat_mod._last_user_message_elapsed_off_loop

  def elapsed_with_concurrent_pick(cid):
    with SessionLocal() as db:
      db.get(models.Chat, cid).agent_settings_json = picked
      db.commit()
    return real_elapsed(cid)

  monkeypatch.setattr(
    chat_mod, "_last_user_message_elapsed_off_loop",
    elapsed_with_concurrent_pick,
  )
  _patch_claude_runner(monkeypatch)
  chat_mod.mark_starting(chat_id)
  _run_real_chat(
    chat_id, run_token="rt-turn-start-pick",
    run_gen=chat_mod.current_run_generation(chat_id),
  )

  with SessionLocal() as db:
    assert db.get(models.Chat, chat_id).agent_settings_json == picked


def test_peer_send_persists_off_the_loop_and_hints_on_it(
  client, auth, db, monkeypatch,
):
  # Validation, the write and the recipient inbox prune ran on the loop.
  from app import agent_coordination
  from tests.test_agent_coordination import _delegated_auth, _network_fixture

  chats, _ = _network_fixture(db)
  nested_auth = _delegated_auth(db, chats["nested"].id, "nested-run")
  seen = []
  real_prune = agent_coordination._prune_direct_inboxes
  real_hint = agent_coordination._publish_message_hint

  def prune(*args, **kwargs):
    seen.append(("prune", _running_on_event_loop()))
    return real_prune(*args, **kwargs)

  def hint(*args, **kwargs):
    seen.append(("hint", _running_on_event_loop()))
    return real_hint(*args, **kwargs)

  monkeypatch.setattr(agent_coordination, "_prune_direct_inboxes", prune)
  monkeypatch.setattr(agent_coordination, "_publish_message_hint", hint)

  sent = client.post(
    "/api/agent-coordination/messages",
    headers=nested_auth,
    json={
      "recipients": [chats["outside_helper"].id],
      "kind": "finding",
      "body": "Found off the loop.",
      "send_id": "off-loop-send-1",
    },
  )

  assert sent.status_code == 200, sent.text
  assert sent.json()["recipient_count"] == 1
  assert seen == [("prune", False), ("hint", True)]


def test_child_settle_reads_off_the_loop_and_publishes_on_it(monkeypatch):
  from app import goal_plans

  seen = []
  plan = delegations._ChildSettlePlan(
    delegation_id="delegation", parent_chat_id="parent-chat",
    status="completed", source_work_id=None,
    goal_chat_ids=("parent-chat", "goal-owner"),
    activity_changed=True, wake_source_work_id=None,
  )

  def plan_settle(child_chat_id):
    seen.append(("plan", _running_on_event_loop()))
    return plan

  def publish(name):
    return lambda chat_id: seen.append((name, chat_id, _running_on_event_loop()))

  monkeypatch.setattr(delegations, "_plan_child_settle", plan_settle)
  monkeypatch.setattr(goal_plans, "publish_goal_changed", publish("goal"))
  monkeypatch.setattr(
    delegations, "publish_parent_waiting_changed", publish("waiting"),
  )
  monkeypatch.setattr(
    delegations, "publish_chat_activity_changed", publish("activity"),
  )

  asyncio.run(delegations.wake_parent_after_child_settled("child-chat"))

  assert seen == [
    ("plan", False),
    ("goal", "parent-chat", True),
    ("goal", "goal-owner", True),
    ("waiting", "parent-chat", True),
    ("activity", "parent-chat", True),
  ]


def test_source_work_settle_writes_off_the_loop_and_publishes_on_it(
  monkeypatch,
):
  # Source-attached work records its lifecycle in the worker, then refreshes
  # only its source surfaces on the loop; it never wakes the source agent.
  seen = []
  plan = delegations._ChildSettlePlan(
    delegation_id="delegation", parent_chat_id="parent-chat",
    status="completed", source_work_id="source-work",
    goal_chat_ids=(), activity_changed=True, wake_source_work_id=None,
  )

  def plan_settle(child_chat_id):
    seen.append(("plan", _running_on_event_loop()))
    return plan

  def source_status(**fields):
    seen.append(("source", fields["source_work_id"], _running_on_event_loop()))

  monkeypatch.setattr(delegations, "_plan_child_settle", plan_settle)
  monkeypatch.setattr(delegations, "_publish_source_work_status", source_status)
  monkeypatch.setattr(
    delegations, "publish_chat_activity_changed",
    lambda chat_id: seen.append(("activity", chat_id, _running_on_event_loop())),
  )
  monkeypatch.setattr(
    delegations, "publish_parent_waiting_changed",
    lambda chat_id: seen.append(("waiting", chat_id)),
  )

  asyncio.run(delegations.wake_parent_after_child_settled("child-chat"))

  assert seen == [
    ("plan", False),
    ("activity", "parent-chat", True),
    ("source", "source-work", True),
  ]


def test_parent_settle_finds_owed_results_off_the_loop(monkeypatch):
  seen = []

  def sources(parent_chat_id):
    seen.append(("sources", _running_on_event_loop()))
    return ["source-run"]

  async def deliver(parent_chat_id, source_work_id):
    seen.append(("deliver", source_work_id, _running_on_event_loop()))
    return False

  monkeypatch.setattr(delegations, "_undelivered_result_sources", sources)
  monkeypatch.setattr(delegations, "_deliver_parent_wake", deliver)

  asyncio.run(delegations.deliver_results_after_parent_settled("parent-chat"))

  assert seen == [("sources", False), ("deliver", "source-run", True)]


def test_result_steer_plans_off_the_loop_and_checks_liveness_on_it(
  monkeypatch,
):
  import app.chat as chat_mod
  import app.chat_steering as chat_steering

  seen = []

  def plan_steer(parent_chat_id, source_work_id):
    seen.append(("plan", _running_on_event_loop()))
    return delegations._ResultSteerPlan(
      provider="claude", content="results", cid="cid", source_work_id="src",
    )

  def running(chat_id):
    seen.append(("running", _running_on_event_loop()))
    return True

  def steerable(chat_id, provider):
    seen.append(("steerable", _running_on_event_loop()))
    return False

  monkeypatch.setattr(delegations, "_plan_result_steer", plan_steer)
  monkeypatch.setattr(chat_mod, "is_chat_running", running)
  monkeypatch.setattr(chat_mod, "is_draining", lambda: False)
  monkeypatch.setattr(chat_steering, "has_live_steerable_turn", steerable)

  assert asyncio.run(
    delegations.steer_results_into_running_parent("parent-chat", "src"),
  ) is False
  assert seen == [
    ("running", True), ("plan", False), ("running", True), ("steerable", True),
  ]


def test_parent_wake_plan_never_reads_the_runner_registry(chat, db, monkeypatch):
  # _plan_parent_wake runs in a worker; the registry belongs to the loop,
  # which re-checks is_chat_running itself after planning.
  import app.chat as chat_mod
  from app import models

  def forbidden(_chat_id):
    raise AssertionError("worker read the loop-owned runner registry")

  monkeypatch.setattr(chat_mod, "is_chat_running", forbidden)
  physical = models.ChatRun(
    id="carrier-run", root_run_id="carrier-run", chat_id=chat.id,
    status="running", provider="claude", provider_execution_admitted=False,
  )
  db.add(physical)
  db.commit()
  monkeypatch.setattr(
    delegations, "_committed_parent_wake",
    lambda _db, _chat, _message: (physical, [], ()),
  )
  monkeypatch.setattr(
    delegations, "safe_parent_wake_startup_writer_orphan",
    lambda _db, _chat, _physical: True,
  )

  assert delegations._committed_parent_wake_is_unowned(db, chat, {}) is True


def test_wait_sweep_selects_due_waits_off_the_loop(monkeypatch):
  from app import chat_waits

  seen = []

  def select(now, force_kind):
    seen.append((force_kind, _running_on_event_loop()))
    return [], [], False

  monkeypatch.setattr(chat_waits, "_select_wait_sweep", select)

  assert asyncio.run(chat_waits.sweep_due_waits(force_kind="kind")) == 0
  assert seen == [("kind", False)]


def test_wait_sweep_reads_the_late_edit_hold_off_the_loop(monkeypatch):
  # The hold reads update state and may run git; it belongs in the worker.
  from app import chat_waits, platform_update

  seen = []

  def hold():
    seen.append(_running_on_event_loop())
    return True

  monkeypatch.setattr(platform_update, "late_edits_pending", hold)

  assert asyncio.run(chat_waits.sweep_due_waits()) == 0
  assert seen == [False]


def test_activity_start_checks_its_blockers_off_the_loop(monkeypatch):
  import app.chat as chat_mod
  import app.chat_start as chat_start

  seen = []

  def blocker(db, chat_id, **_kwargs):
    seen.append(_running_on_event_loop())
    return "owner_input"

  monkeypatch.setattr(chat_mod, "programmatic_start_blocker", blocker)
  monkeypatch.setattr(chat_mod, "is_draining", lambda: False)

  assert asyncio.run(chat_start.start_programmatic_activity_continuation(
    chat_id="parent-chat", root_run_id="root", run_token="not-started",
    source_work_id="source", activity_id="activity",
  )) is False
  assert seen == [False]


def test_outbound_access_list_reads_profiles_off_the_loop(
  client, auth, monkeypatch,
):
  # list_profiles reads files under the outbound lock pairing also holds.
  from app import connect_outbound

  seen = []

  def profiles():
    seen.append(_running_on_event_loop())
    return []

  monkeypatch.setattr(connect_outbound, "list_profiles", profiles)

  response = client.get("/api/connect/outbound", headers=auth)
  assert response.status_code == 200, response.text
  assert seen == [False]


def test_activity_start_rechecks_drain_after_its_off_loop_read(monkeypatch):
  # A drain that begins during the worker read owns its runs; the start
  # must stop before touching the database on the loop.
  import app.chat as chat_mod
  import app.chat_start as chat_start
  import app.database as database

  draining = {"value": False}
  sessions = []

  def precheck(chat_id, run_token):
    draining["value"] = True
    return True, False

  monkeypatch.setattr(chat_mod, "is_draining", lambda: draining["value"])
  monkeypatch.setattr(chat_start, "_activity_start_precheck", precheck)
  monkeypatch.setattr(
    database, "SessionLocal", lambda *a, **kw: sessions.append(1),
  )

  assert asyncio.run(chat_start.start_programmatic_activity_continuation(
    chat_id="parent-chat", root_run_id="root", run_token="existing-run",
    source_work_id="source", activity_id="activity",
  )) is False
  assert sessions == []
