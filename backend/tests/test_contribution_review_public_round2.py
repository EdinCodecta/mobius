"""Public verdict consent, cross-grant admission and private list projections."""
import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException
from app import models, contribution_review_runs as domain, contribution_review_public as public
from app.database import SessionLocal
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import ITEM, setup, report
from tests.test_contribution_review_public import opted_in


@pytest.mark.parametrize('state', ['posting', 'unknown', 'posted'])
def test_failed_live_read_preserves_concurrently_armed_receipt(setup, monkeypatch, state):
  db, row, _ = setup
  opted_in(setup)
  confirmed = {'state': 'needs_you', 'summary': 'Actual public finding', 'head_sha': ITEM['head_sha'],
    'public_review': {'state': state, 'marker': 'other-worker', 'id': 7}}
  def fail(*args):
    with SessionLocal() as other:
      live = other.get(models.ContributionReviewRun, row.id)
      domain.save_outcome(other, live, domain.key(ITEM), confirmed)
    raise HTTPException(409, 'Live read failed')
  monkeypatch.setattr(domain, 'current_pull', fail)
  report(setup, publish=True)
  db.refresh(row)
  assert row.outcomes_json[domain.key(ITEM)] == confirmed


def test_owner_question_stays_private_until_explicit_public_verdict(setup, monkeypatch):
  db, row, _ = setup
  opted_in(setup)
  sent = []
  monkeypatch.setattr(public, 'post_review', lambda *args: sent.append(args[-1]) or public.DispatchResult.confirmed({'id': 7}))
  result = report(setup, state='needs_you', summary='Can I access the private fixture?', publish=False)
  assert result['run']['state'] == 'needs_you'
  assert not sent and 'public_review' not in row.outcomes_json[domain.key(ITEM)]
  report(setup, state='needs_you', summary='A concrete public correctness finding.', publish=True)
  assert len(sent) == 1 and 'private fixture' not in sent[0]


@pytest.mark.parametrize('state', ['posting', 'unknown', 'posted'])
@pytest.mark.parametrize('app_id', [None, 1])
def test_public_review_fences_another_grant_and_names_holder(setup, monkeypatch, state, app_id):
  db, row, _ = setup
  opted_in(setup)
  holder = models.ContributionReviewRun(id='holding-run', app_id=app_id, owner_id=row.owner_id,
    request_id='another-request', mode='review', targets_json=deepcopy(row.targets_json),
    outcomes_json={domain.key(ITEM): {'state': 'all_clear', 'head_sha': ITEM['head_sha'],
      'public_review': {'state': state, 'marker': 'other-attempt'}}},
    chat_id=row.chat_id, github_actor_id='42', app_nonce=row.app_nonce if app_id else None)
  db.add(holder); db.commit()
  monkeypatch.setattr(public, 'post_review', lambda *args: pytest.fail('other grant already holds this head'))
  with pytest.raises(HTTPException) as error:
    report(setup, publish=True)
  assert error.value.status_code == 409
  assert holder.id in str(error.value.detail) and holder.chat_id in str(error.value.detail)
  assert domain.key(ITEM) not in row.outcomes_json


def test_public_receipt_does_not_duplicate_frozen_evidence(setup, monkeypatch):
  db, row, _ = setup
  opted_in(setup)
  monkeypatch.setattr(public, 'post_review', lambda *args: public.DispatchResult.confirmed({'id': 7}))
  report(setup, publish=True)
  saved = row.outcomes_json[domain.key(ITEM)]
  assert saved['summary'] == 'Full diff reviewed'
  assert 'evidence' not in saved['public_review']
  with pytest.raises(HTTPException):
    report(setup, publish=True, summary='Changed finding')


def test_list_does_not_return_full_evidence_but_detail_keeps_it(setup):
  db, row, principal = setup
  opted_in(setup)
  domain.save_outcome(db, row, domain.key(ITEM), {'state': 'needs_you', 'summary': 's'*200000, 'tests': 't'*200000})
  item = routes.list_reviews(1, db, principal)['runs'][0]['items'][0]
  assert len(item.get('summary', '')) <= 240 and 'tests' not in item
  detail = routes.get_review(1, row.id, db, principal)['run']['items'][0]
  assert detail['summary'] == 's'*200000 and detail['tests'] == 't'*200000


def test_public_instructions_and_label_are_unambiguous():
  from app import contribution_review_presets as presets
  options = presets.resolve_options(None, {'post_review': True}, choice={'provider': 'codex', 'model': 'test', 'effort': None})
  instruction = options['mandatory_instructions']
  assert 'publish' in instruction and 'server' in instruction
  assert 'Never post comments or reviews' not in instruction
  text = public.review_comment_body({'summary': 'Finding', 'tests': 'Reviewer ran pytest', 'head_sha': ITEM['head_sha']})
  assert '**Verification:**' in text and '**Checks:**' not in text


@pytest.mark.parametrize('change', ['head', 'number', 'repo', 'not_dispatched'])
def test_other_head_or_proved_non_dispatch_does_not_fence_public_review(setup, monkeypatch, change):
  db, row, _ = setup
  opted_in(setup)
  target = {**ITEM}
  if change == 'head': target['head_sha'] = 'c' * 40
  if change == 'number': target['number'] += 1
  if change == 'repo': target['repo'] = 'other/repo'
  holder = models.ContributionReviewRun(id='nonblocking', app_id=None, owner_id=row.owner_id,
    request_id='different', mode='review', targets_json=[target],
    outcomes_json={domain.key(target): {'state': 'all_clear', 'head_sha': target['head_sha'],
      'public_review': {'state': 'not_dispatched' if change == 'not_dispatched' else 'unknown'}}},
    chat_id=row.chat_id, github_actor_id='42')
  db.add(holder); db.commit()
  sent = []
  monkeypatch.setattr(public, 'post_review', lambda *a: sent.append(1) or public.DispatchResult.confirmed({'id': 8}))
  report(setup, publish=True)
  assert sent == [1]


def test_public_holder_lookup_occurs_after_database_writer_fence(setup, monkeypatch):
  from app import contribution_review_public_actions as actions
  db, row, _ = setup
  opted_in(setup)
  actual_fence = actions.fence_owner
  def fence(session, owner_id):
    actual_fence(session, owner_id)
    session.add(models.ContributionReviewRun(id='fence-holder', app_id=None, owner_id=owner_id,
      request_id='at-fence', mode='review', targets_json=deepcopy(row.targets_json),
      outcomes_json={domain.key(ITEM): {'state': 'all_clear', 'head_sha': ITEM['head_sha'],
        'public_review': {'state': 'unknown'}}}, chat_id=row.chat_id, github_actor_id='42'))
    session.flush()
  monkeypatch.setattr(actions, 'fence_owner', fence)
  monkeypatch.setattr(public, 'post_review', lambda *a: pytest.fail('holder at write fence was missed'))
  with pytest.raises(HTTPException, match='fence-holder'):
    report(setup, publish=True)
