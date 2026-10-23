"""Receipt-aware checkout ownership at Stop and confirmed publication."""
import asyncio
from types import SimpleNamespace

from app import contribution_review_repair_actions as actions
from app import contribution_review_repairs as repairs
from app import contribution_review_runs as domain
from tests.test_contribution_review_runs import setup, ITEM


def test_stopped_editors_cleanup_only_unspent_paths_and_preserve_uncertain_receipts(monkeypatch):
  deleted = []
  monkeypatch.setattr(repairs, 'discard_checkout', deleted.append)
  row = SimpleNamespace(outcomes_json={
    'a': {'checkout': {'checkout': '/unspent'}},
    'b': {'checkout': {'checkout': '/uncertain'}, 'repair_attempts': [{'state': 'push_unknown'}]},
    'c': {'checkout': {'checkout': '/dispatching'}, 'repair_attempts': [{'state': 'pushing'}]},
  })
  asyncio.run(actions.abandon_checkouts(row))
  assert deleted == ['/unspent']


def test_confirmed_reconcile_discards_checkout_after_durable_successor(setup, monkeypatch):
  db, row, _ = setup
  receipt = {'id': 'push-1', 'from_sha': ITEM['head_sha'], 'head_sha': 'c'*40,
             'base_sha': ITEM['base_sha'], 'state': 'push_unknown', 'checkout': '/confirmed'}
  previous = {'state': 'needs_you', 'repair_attempts': [receipt], 'checkout': {'checkout': '/confirmed'}}
  domain.save_outcome(db, row, domain.key(ITEM), previous)
  deleted = []
  def discard(path):
    assert row.outcomes_json[domain.key(ITEM)]['repair_attempts'][0]['state'] == 'pushed'
    deleted.append(path)
  monkeypatch.setattr(repairs, 'discard_checkout', discard)
  assert asyncio.run(actions.reconcile_push(db, row, ITEM, previous, receipt, gh=None, cwd=None))
  assert deleted == ['/confirmed']
  assert 'checkout' not in row.outcomes_json[domain.key(ITEM)]


def test_step_history_is_a_bounded_envelope_not_recursive_evidence(setup):
  db, row, _ = setup
  for index in range(105):
    domain.save_outcome(db, row, domain.key(ITEM), {'state': 'needs_you', 'summary': 'x'*200_000})
  outcome = row.outcomes_json[domain.key(ITEM)]
  assert len(outcome['summary']) == 200_000
  assert len(outcome['steps']) == 100
  assert all('summary' not in step and 'steps' not in step for step in outcome['steps'])
