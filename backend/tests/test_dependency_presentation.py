"""Dependency context is exact, quiet and truthful across owner-facing surfaces."""
import pytest
from app import models
from app.chat_writer import create_chat
from app.goal_dependencies import (source_dependency_backlinks,
  terminal_dependency_summaries_by_message_index, goal_dependency_history)
from app.timeutil import now_naive_utc
from tests.test_decision_dependencies import _setup, _follow
from tests.test_goal_dependency_delivery import _run, _dependency


def test_card_backlinks_are_exact_deduplicated_and_read_only(db, chat, client, auth):
  source, _ = _setup(db, chat)
  chat.title = 'Finish blocked update'
  row = _follow(db, chat)
  other = create_chat(id='other', title='Another dependent')
  db.add(other)
  db.add(models.ChatGoal(id='other-goal', chat_id='other', objective='Other work'))
  db.add(models.ChatGoal(id='second-goal', chat_id=chat.id, objective='Second work'))
  db.flush()
  for id, waiting_chat, goal in [('second', chat.id, 'second-goal'), ('other-link', 'other', 'other-goal')]:
    db.add(models.GoalDependency(id=id, waiting_chat_id=waiting_chat, waiting_goal_id=goal,
      created_by_run_id='dependent-run', target_kind='owner_card', target_key='card:source:choice',
      target_chat_id='source', target_question_id='choice'))
  db.commit()
  expected = [{'chat_id': 'other', 'title': 'Another dependent'}, {'chat_id': chat.id, 'title': 'Finish blocked update'}]
  assert source_dependency_backlinks(db, source.id, question_ids=['choice', 'unrelated']) == {'choice': expected}
  detail = client.get('/api/chats/source', headers=auth).json()
  cards = [b for m in detail['messages'] for b in m.get('blocks', []) if b.get('type') == 'question']
  assert cards[0]['needed_by'] == expected
  assert source_dependency_backlinks(db, chat.id, question_ids=['choice']) == {}
  assert row.resolved_at is None and row.received_run_id is None
  assert db.get(models.Chat, source.id).pending_question_id == 'choice'
  db.get(models.ChatGoal, 'other-goal').status = 'completed'
  db.get(models.ChatGoal, 'dependent-goal').status = 'stopped'
  db.commit()
  assert source_dependency_backlinks(db, source.id, question_ids=['choice']) == {'choice': expected[1:]}
  chat.deleted_at = now_naive_utc()
  db.commit()
  assert source_dependency_backlinks(db, source.id, question_ids=['choice']) == {}


def test_claim_backlink_moves_with_exact_acquisition_owner_not_another_goal(db, chat, auth):
  owner = db.query(models.Owner).one()
  run = _run(db, chat, 'follower', 'dependent')
  row = _dependency(db, chat, run, settled=False)
  source = create_chat(id='source', title='Repair')
  next_source = create_chat(id='next-source', title='New executor')
  db.add_all([source, next_source])
  db.flush()
  claim = models.AgentWorkClaim(id='claim', acquisition_id='acquisition', owner_id=owner.id,
    work_key='repair', summary='Repair host', owner_chat_id='source', owner_goal_id='source-goal', owner_run_id='source-run')
  db.add(claim)
  db.commit()
  assert source_dependency_backlinks(db, 'source', goal_id='source-goal')['source-goal'][0]['chat_id'] == chat.id
  assert source_dependency_backlinks(db, 'source', goal_id='unrelated') == {}
  claim.owner_chat_id, claim.owner_goal_id = 'next-source', 'next-goal'
  db.commit()
  assert source_dependency_backlinks(db, 'source', goal_id='source-goal') == {}
  assert source_dependency_backlinks(db, 'next-source', goal_id='next-goal')['next-goal']
  claim.acquisition_id = 'new-acquisition'
  db.commit()
  assert source_dependency_backlinks(db, 'next-source', goal_id='next-goal') == {}
  assert row.target_acquisition_id == 'acquisition'


@pytest.mark.parametrize('state', ['automatic', 'held', 'failed', 'local-input'])
def test_drawer_runtime_and_detail_agree_on_dependency_continuation(client, auth, db, chat, state):
  from app.goals import _new_hold
  from tests.test_chat_deferred_handoff import read
  _setup(db, chat)
  _follow(db, chat)
  run = db.get(models.ChatRun, 'dependent-run')
  run.goal_objective = 'Finish update'
  goal = db.get(models.ChatGoal, 'dependent-goal')
  if state == 'held':
    goal.status = 'stopped'
    goal.hold_json = _new_hold(cause='deferred', actor='agent', source_id=run.id, run_id=run.id, reason='Later')
  if state == 'failed':
    row = db.query(models.GoalDependency).one()
    row.target_kind, row.target_claim_id = 'claim_acquisition', 'missing'
  if state == 'local-input':
    chat.pending_question_id = 'own-card'
  db.commit()
  listing, runtime = read(client, auth, chat)
  expected = {'automatic': 'automatic', 'held': 'on_hold', 'failed': 'recovery', 'local-input': 'owner_input'}[state]
  assert listing['handoff']['kind'] == expected
  assert listing['waiting'] == (expected == 'automatic')
  if state in {'held', 'failed'}:
    assert not runtime['goal']['dependencies'][0]['automatic']


@pytest.mark.parametrize('disposition', [None, 'delivered', 'withdrawn', 'retired_by_goal_outcome'])
def test_typed_history_belongs_to_exact_receipt_or_declaration_never_newest_goal(db, chat, disposition):
  run = _run(db, chat, 'declare', 'goal')
  row = _dependency(db, chat, run)
  row.result_disposition = disposition
  row.received_run_id = 'receive' if disposition == 'delivered' else None
  db.commit()
  messages = [{'role': 'assistant', 'id': id} for id in ['declare', 'unrelated', 'receive']]
  history = terminal_dependency_summaries_by_message_index(db, chat.id, messages)
  index = 2 if disposition == 'delivered' else 0
  assert list(history) == [index]
  assert history[index][0]['outcome']['kind'] == 'released'
  assert history[index][0]['condition'] == 'Host repair'
  assert history[index][0]['delivery_pending'] == (disposition is None)
  assert terminal_dependency_summaries_by_message_index(db, chat.id, [messages[1]]) == {}
  assert bool(goal_dependency_history(db, db.get(models.ChatGoal, 'goal'))) == (disposition is not None)


def test_dependency_trace_skips_hidden_segments_of_both_exact_runs(db, chat):
  run = _run(db, chat, 'declare', 'goal')
  row = _dependency(db, chat, run)
  row.result_disposition, row.received_run_id = 'delivered', 'receive'
  db.commit()
  messages = [{'role': 'assistant', 'id': 'receive', 'hidden': True},
    {'role': 'assistant', 'id': 'declare', 'hidden': True},
    {'role': 'assistant', 'id': 'unrelated'},
    {'role': 'assistant', 'id': 'declare:assistant:1'},
    {'role': 'assistant', 'id': 'receive:assistant:1'}]
  assert list(terminal_dependency_summaries_by_message_index(db, chat.id, messages)) == [4]
  assert list(terminal_dependency_summaries_by_message_index(db, chat.id, messages[:-1])) == [3]


def test_retired_pending_claim_keeps_source_context_after_reacquisition(db, chat, auth):
  from app.goal_dependencies import stage_retire_goal_dependencies
  run = _run(db, chat, 'declare', 'goal')
  row = _dependency(db, chat, run, settled=False)
  source = create_chat(id='source', title='Original repair')
  db.add(source)
  db.flush()
  claim = models.AgentWorkClaim(id='claim', acquisition_id='acquisition', owner_id=db.query(models.Owner).one().id,
    work_key='repair', summary='Repair host before release', owner_chat_id='source', owner_run_id='source-run')
  db.add(claim)
  db.commit()
  assert stage_retire_goal_dependencies(db, chat.id, 'goal') == 1
  claim.acquisition_id, claim.summary = 'new-acquisition', 'Unrelated later operation'
  db.commit(); db.expire_all()
  history = goal_dependency_history(db, db.get(models.ChatGoal, 'goal'))[0]
  assert history['condition'] == 'Repair host before release'
  assert history['outcome'].get('kind') is None
  assert row.resolved_at is None and row.received_run_id is None
  assert history['disposition'] == 'retired_by_goal_outcome'
