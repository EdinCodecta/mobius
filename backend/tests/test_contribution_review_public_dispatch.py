"""Live target proof belongs next to public review dispatch, not only the route."""
from copy import deepcopy
import pytest
from app import contribution_review_runs as domain, contribution_review_public as public
from app import contribution_review_public_actions as actions
from tests.test_contribution_review_runs import setup, report, ITEM, REPO, PULL, REF
from tests.test_contribution_review_public import opted_in

CURRENT_PULL = domain.current_pull
ASSERT_CURRENT_BASE = domain.assert_current_base


@pytest.mark.parametrize('field', ['head', 'base_ref'])
@pytest.mark.parametrize('during', ['actor', 'arm'])
def test_target_moving_after_route_check_never_posts(setup, monkeypatch, field, during):
  db, row, _ = setup
  opted_in(setup)
  pull, ref = deepcopy(PULL), deepcopy(REF)
  posts = []
  armed = []

  def move():
    if field == 'head':
      pull['head']['sha'] = 'c' * 40
    else:
      pull['base']['ref'] = 'other'

  def read(gh, cwd, endpoint):
    if endpoint == 'user':
      if during == 'actor':
        move()
      return {'id': 42}
    if endpoint.endswith('/pulls/7'):
      return deepcopy(pull)
    if '/git/ref/heads/' in endpoint:
      return deepcopy(ref)
    return deepcopy(REPO)

  arm = actions.arm_public_review
  def arm_then_move(*args):
    result = arm(*args)
    armed.append(deepcopy(row.outcomes_json[domain.key(ITEM)]['public_review']))
    if during == 'arm':
      move()
    return result

  monkeypatch.setattr(domain, 'read', read)
  monkeypatch.setattr(domain, 'current_pull', CURRENT_PULL)
  monkeypatch.setattr(domain, 'assert_current_base', ASSERT_CURRENT_BASE)
  monkeypatch.setattr(actions, 'arm_public_review', arm_then_move)
  monkeypatch.setattr(public, 'post_review', lambda *a: posts.append(a) or public.DispatchResult.confirmed({'id': 1}))
  receipt = report(setup, publish=True)['run']['items'][0]['public_review']
  assert posts == []
  assert receipt['state'] == 'not_dispatched'
  assert 'no review was posted' in receipt['summary'].lower()
  if during == 'actor':
    assert armed == []
  else:
    assert receipt['marker'] == armed[0]['marker']


def test_public_comment_on_selected_head_can_post_after_target_base_advances(setup, monkeypatch):
  opted_in(setup)
  posts = []
  def read(gh, cwd, endpoint):
    if endpoint == 'user':
      return {'id': 42}
    if endpoint.endswith('/pulls/7'):
      return deepcopy(PULL)
    if '/git/ref/heads/' in endpoint:
      return {'object': {'sha': 'd' * 40}}
    return deepcopy(REPO)
  monkeypatch.setattr(domain, 'read', read)
  monkeypatch.setattr(domain, 'current_pull', CURRENT_PULL)
  monkeypatch.setattr(domain, 'assert_current_base', ASSERT_CURRENT_BASE)
  monkeypatch.setattr(public, 'post_review', lambda *a: posts.append(a) or public.DispatchResult.confirmed({'id': 1}))
  receipt = report(setup, publish=True)['run']['items'][0]['public_review']
  assert receipt['state'] == 'posted'
  assert len(posts) == 1
  assert posts[0][2]['head_sha'] == ITEM['head_sha']
