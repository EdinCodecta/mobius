"""Immutable posted evidence and honest public review dispatch outcomes."""
import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app import contribution_review_public as public
from app.routes import contribution_reviews as routes
from tests.test_contribution_review_runs import setup, report, ITEM


def opted_in(setup):
  db, row, _ = setup
  row.mode = "review"
  row.options_json = {"post_review": True}
  db.commit()
  return row


def test_posted_verdict_is_immutable_and_identical_retry_reads_no_remote_state(setup, monkeypatch):
  opted_in(setup)
  calls = []
  monkeypatch.setattr(public, 'post_review', lambda *a: calls.append(a) or public.DispatchResult.confirmed({'id': 3}))
  report(setup)
  monkeypatch.setattr(routes.reviews, 'current_pull', lambda *a: pytest.fail('identical replay is historical'))
  report(setup)
  with pytest.raises(HTTPException) as exc:
    report(setup, state='needs_you', summary='Different verdict')
  assert exc.value.status_code == 409
  assert len(calls) == 1


def test_unknown_post_freezes_evidence_without_replay(setup, monkeypatch):
  row = opted_in(setup)
  monkeypatch.setattr(public, 'post_review', lambda *a: public.DispatchResult.unknown('lost response'))
  report(setup)
  assert row.outcomes_json['example/project#7']['public_review']['state'] == 'unknown'
  monkeypatch.setattr(public, 'post_review', lambda *a: pytest.fail('replayed'))
  report(setup)
  with pytest.raises(HTTPException):
    report(setup, tests='Different evidence')


def test_confirmed_public_fact_survives_stop(setup, monkeypatch):
  db, row, _ = setup
  opted_in(setup)
  def dispatch(*args):
    from app import models
    run = db.get(models.ChatRun, 'physical-run')
    run.status = 'stopped'
    db.commit()
    return public.DispatchResult.confirmed({'id': 42})
  monkeypatch.setattr(public, 'post_review', dispatch)
  report(setup)
  assert row.outcomes_json['example/project#7']['public_review']['id'] == 42


@pytest.mark.parametrize('field', ['summary', 'tests'])
def test_evidence_is_generous_but_bounded(field):
  values = {**ITEM, 'state': 'needs_you', 'summary': 'finding', field: 'x' * 200_000}
  assert len(getattr(routes.ReviewOutcome(**values), field)) == 200_000
  values[field] += 'x'
  with pytest.raises(ValidationError):
    routes.ReviewOutcome(**values)


def test_public_unicode_bound_preserves_verdict_checks_head_and_private_copy():
  original = {'state': 'all_clear', 'summary': '😀' * 200_000, 'tests': '✓' * 200_000, 'head_sha': 'a'*40}
  body = public.review_comment_body(original)
  assert public._body_units(body) <= 65536
  assert 'omitted' in body and '**Checks:**' in body and 'All clear' in body
  assert 'aaaaaaaaaaaa' in body
  assert len(original['summary']) == 200_000


def test_public_payload_uses_stdin_and_typed_confirmed_receipt():
  calls = []
  def gh(*args, **kwargs):
    calls.append((args, kwargs))
    return SimpleNamespace(stdout='{"id": 8, "html_url": "https://github.com/review"}')
  result = public.post_review(gh, None, ITEM, 'evidence')
  assert result.state == 'confirmed' and result.receipt['id'] == 8
  args, kwargs = calls[0]
  assert args[-2:] == ('--input', '-')
  assert json.loads(kwargs['input_text'])['event'] == 'COMMENT'
  assert 'evidence' not in args


def test_known_oversize_is_not_dispatched_and_transport_loss_is_unknown():
  assert public.post_review(lambda *a, **kw: pytest.fail('send'), None, ITEM, 'a'*65537).state == 'not_dispatched'
  def failed(*a, **kw):
    raise OSError('transport lost')
  assert public.post_review(failed, None, ITEM, 'fine').state == 'unknown'
