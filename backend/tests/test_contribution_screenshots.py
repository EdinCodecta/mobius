"""A prepared PR's screenshots are approved with its text and hosted at Send.

Staging uses the real stage route and repositories from the staging suite.
Publication runs real Git against a local bare repository standing in for the
owner's fork; only the anonymous raw-URL read is faked.
"""

import hashlib
import subprocess
import zlib
from pathlib import Path

import pytest

from app import contribution_screenshots as shots
from app.config import get_settings
from app.contribution_errors import ContributionSubmitError
from app.github_contributions import (
  _publish_reviewed_screenshots,
  publication_approval_sha256,
)
from app.routes import contribution_relay
from tests.test_contribution_staging import staging  # noqa: F401  (fixture)


def _png(seed: int) -> bytes:
  """A tiny valid PNG whose pixel differs per seed."""
  def chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return (
      len(data).to_bytes(4, "big") + body
      + zlib.crc32(body).to_bytes(4, "big")
    )
  header = (1).to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0])
  pixel = zlib.compress(bytes([0, seed % 256, 0, 0]))
  return (
    b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")
  )


def _write(tmp_path: Path, name: str, data: bytes) -> str:
  path = tmp_path / name
  path.write_bytes(data)
  return str(path)


BODY = (
  "Fixes the header.\n\n| Before | After |\n|---|---|\n"
  "| ![Old header](screenshot:before.png) | ![New header](screenshot:after.png) |\n"
)


def _items(tmp_path: Path, before=1, after=2) -> list[dict]:
  return [
    {"name": "before.png", "path": _write(tmp_path, "b.png", _png(before)),
     "alt": "Old header"},
    {"name": "after.png", "path": _write(tmp_path, "a.png", _png(after)),
     "alt": "New header"},
  ]


def _store(app_id: int, record_id: str = "greet") -> Path:
  return shots.blob_dir(
    Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
    / f"{record_id}.json"
  )


def test_staged_screenshots_are_listed_in_the_approved_plan(staging, tmp_path, client, owner_token):
  response = staging["new_record"](body_draft=BODY, screenshots=_items(tmp_path))
  assert response.status_code == 200, response.text
  entries = response.json()["record"]["plan"]["screenshots"]
  assert [e["name"] for e in entries] == ["before.png", "after.png"]
  assert entries[0]["sha256"] == hashlib.sha256(_png(1)).hexdigest()
  stored = sorted(p.name for p in _store(staging["app_id"]).iterdir())
  assert stored == sorted(f"{e['sha256']}.png" for e in entries)

  served = client.get(
    f"/api/github/contributions/{staging['app_id']}/greet/screenshots/after.png",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert served.status_code == 200
  assert served.headers["content-type"] == "image/png"
  assert served.content == _png(2)


def test_the_approval_digest_changes_when_a_screenshot_changes():
  record = {"id": "r", "plan": {"screenshots": [{"name": "a.png", "sha256": "1" * 64}]}}
  other = {"id": "r", "plan": {"screenshots": [{"name": "a.png", "sha256": "2" * 64}]}}
  assert publication_approval_sha256(record) != publication_approval_sha256(other)


@pytest.mark.parametrize(("body", "expected"), [
  ("Only ![Old header](screenshot:before.png).", "Not shown: after.png"),
  (BODY + "![Extra](screenshot:extra.png)", "not attached: extra.png"),
])
def test_stage_refuses_screenshots_and_references_that_do_not_match(staging, tmp_path, body, expected):
  response = staging["new_record"](body_draft=body, screenshots=_items(tmp_path))
  assert response.status_code == 422
  assert expected in response.text


def test_stage_refuses_a_reference_without_any_attachment(staging):
  response = staging["new_record"](body_draft="See ![x](screenshot:x.png)")
  assert response.status_code == 422
  assert "not attached: x.png" in response.text


def test_stage_refuses_a_file_that_is_not_a_png(staging, tmp_path):
  items = _items(tmp_path)
  items[0]["path"] = _write(tmp_path, "fake.png", b"GIF89a not a png")
  response = staging["new_record"](body_draft=BODY, screenshots=items)
  assert response.status_code == 422
  assert "is not a PNG image" in response.text


def test_changing_a_screenshot_drops_the_verdict_and_prunes_the_old_image(staging, tmp_path):
  record = staging["new_record"](body_draft=BODY, screenshots=_items(tmp_path)).json()["record"]
  assert staging["review"](record).status_code == 200
  kept = staging["stage"](body_draft=BODY).json()["record"]
  assert kept["quality_review"]["state"] == "all_clear"

  changed = staging["stage"](screenshots=_items(tmp_path, after=3)).json()["record"]
  assert "quality_review" not in changed
  stored = {p.name for p in _store(staging["app_id"]).iterdir()}
  assert stored == {f"{e['sha256']}.png" for e in changed["plan"]["screenshots"]}

  cleared = staging["stage"](body_draft="No images.", screenshots=[]).json()["record"]
  assert "screenshots" not in cleared["plan"]
  assert not _store(staging["app_id"]).exists()


def test_the_bot_relay_refuses_screenshots_it_cannot_host():
  with pytest.raises(ContributionSubmitError) as raised:
    contribution_relay._reviewed_public_metadata({
      "plan": {"title": "T", "body_draft": BODY, "screenshots": [{"name": "a.png"}]},
    })
  assert raised.value.code == "relay_screenshots_unsupported"


# --- Publication -------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
  ).stdout.strip()


@pytest.fixture
def fork(tmp_path, monkeypatch):
  """A bare 'fork', a local checkout, and a record store with two images."""
  monkeypatch.setattr(shots, "_PROOF_BACKOFF_SECONDS", 0)
  remote = tmp_path / "fork.git"
  _git(tmp_path, "init", "-q", "--bare", str(remote))
  repo = tmp_path / "checkout"
  repo.mkdir()
  _git(repo, "init", "-q")
  record_path = tmp_path / "records" / "greet.json"
  record_path.parent.mkdir()
  entries = shots.stage(record_path, _items(tmp_path), BODY)

  def read_public(url: str) -> bytes | None:
    prefix = "https://raw.githubusercontent.com/octocat/project/"
    # Like a real HTTP client, the fragment never reaches the server.
    commit, path = url.split("#", 1)[0].removeprefix(prefix).split("/", 1)
    found = subprocess.run(
      ["git", "show", f"{commit}:{path}"], cwd=remote, capture_output=True,
    )
    return found.stdout if found.returncode == 0 else None

  def publish(entries=entries, **overrides):
    return shots.publish(
      repo, record_path, entries, assets_repo="octocat/project",
      branch="fix/header", author=("octocat", "1+octocat@users.noreply.github.com"),
      remote_url=str(remote), **{"read_public": read_public, **overrides},
    )

  return {"remote": remote, "record_path": record_path, "entries": entries,
          "publish": publish}


def test_send_hosts_the_exact_images_and_pins_the_description_to_them(fork):
  commit = fork["publish"]()
  remote = fork["remote"]
  assert _git(remote, "rev-parse", "refs/heads/assets") == commit
  assert _git(remote, "log", "-1", "--format=%an", commit) == "octocat"
  shown = subprocess.run(
    ["git", "show", f"{commit}:assets/fix/header/after.png"],
    cwd=remote, capture_output=True, check=True,
  ).stdout
  assert shown == _png(2)

  body = shots.resolve_body(
    BODY, fork["entries"], assets_repo="octocat/project", commit=commit,
    branch="fix/header",
  )
  assert "screenshot:" not in body
  assert (
    f"(https://raw.githubusercontent.com/octocat/project/{commit}"
    "/assets/fix/header/after.png)"
  ) in body


def test_a_retried_send_links_the_same_upload_without_a_new_commit(fork):
  first = fork["publish"]()
  assert fork["publish"](reuse_commit=first) == first
  assert fork["publish"]() == first
  assert _git(fork["remote"], "rev-list", "--count", "refs/heads/assets") == "1"


def test_send_adds_to_an_existing_assets_branch_without_rewriting_it(fork, tmp_path):
  other = tmp_path / "other"
  _git(tmp_path, "clone", "-q", str(fork["remote"]), str(other))
  _git(other, "checkout", "-q", "-b", "assets")
  (other / "keep.txt").write_text("earlier upload\n")
  _git(other, "add", "keep.txt")
  _git(other, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "-m", "old")
  _git(other, "push", "-q", "origin", "assets")
  earlier = _git(other, "rev-parse", "HEAD")

  commit = fork["publish"]()
  assert _git(fork["remote"], "rev-parse", f"{commit}^") == earlier
  assert _git(fork["remote"], "show", f"{commit}:keep.txt") == "earlier upload"


def test_send_refuses_an_image_that_changed_after_approval(fork):
  stored = shots.blob_dir(fork["record_path"]) / f"{fork['entries'][0]['sha256']}.png"
  stored.write_bytes(_png(9))
  with pytest.raises(ContributionSubmitError) as raised:
    fork["publish"]()
  assert raised.value.code == "screenshot_changed"
  assert subprocess.run(
    ["git", "rev-parse", "--verify", "--quiet", "refs/heads/assets"],
    cwd=fork["remote"], capture_output=True,
  ).returncode != 0


def test_send_stops_when_github_does_not_serve_the_images_publicly(fork):
  with pytest.raises(ContributionSubmitError) as raised:
    fork["publish"](read_public=lambda _url: None)
  assert raised.value.code == "screenshot_not_public"


def test_an_open_pr_update_cannot_carry_screenshots(fork):
  with pytest.raises(ContributionSubmitError) as raised:
    _publish_reviewed_screenshots(
      {}, fork["record_path"].with_suffix(".diff"), Path("/nonexistent"),
      fork["entries"], BODY, upstream_repo="octo/project", login="octocat",
      branch="fix/header", author=("octocat", "e"), is_update=True,
      record_patch={"head_repository": "octocat/project"},
    )
  assert raised.value.code == "screenshot_changed"
  assert raised.value.record_patch == {"head_repository": "octocat/project"}


# --- Record fields are re-checked at Send -------------------------------------


def test_a_tampered_reuse_commit_never_reaches_the_public_link(fork):
  # The fragment is not sent, so the injected value still proves real bytes.
  after = [fork["entries"][1]]
  real = fork["publish"](after)
  injected = f"{real}/assets/fix/header/after.png#) **Approved** ("
  commit = fork["publish"](after, reuse_commit=injected)
  assert commit == real


@pytest.mark.parametrize("entry", [
  {"name": "../escape.png"},
  {"name": "/tmp/escape.png"},
])
def test_send_refuses_a_screenshot_name_staging_would_not_accept(fork, entry):
  entries = [{**fork["entries"][0], **entry}]
  with pytest.raises(ContributionSubmitError) as raised:
    shots.verify(fork["record_path"], entries)
  assert raised.value.code == "screenshot_changed"


def test_send_refuses_stored_bytes_that_are_not_a_png(fork):
  data = b"not a png at all"
  sha = hashlib.sha256(data).hexdigest()
  (shots.blob_dir(fork["record_path"]) / f"{sha}.png").write_bytes(data)
  with pytest.raises(ContributionSubmitError) as raised:
    shots.verify(fork["record_path"], [{"name": "a.png", "sha256": sha}])
  assert raised.value.code == "screenshot_changed"


def test_restaging_repairs_a_damaged_stored_image(fork, tmp_path):
  stored = shots.blob_dir(fork["record_path"]) / f"{fork['entries'][0]['sha256']}.png"
  stored.write_bytes(b"damaged")
  shots.stage(fork["record_path"], _items(tmp_path), BODY)
  shots.verify(fork["record_path"], fork["entries"])


def test_a_concurrent_upload_is_kept_and_ours_rebuilt_on_top(fork, tmp_path, monkeypatch):
  other = tmp_path / "racer"
  _git(tmp_path, "clone", "-q", str(fork["remote"]), str(other))
  original = shots._assets_commit
  raced = {"done": False}

  def race_then_build(*args, **kwargs):
    commit = original(*args, **kwargs)
    if not raced["done"]:
      raced["done"] = True
      _git(other, "checkout", "-q", "--orphan", "assets")
      (other / "theirs.txt").write_text("another send\n")
      _git(other, "add", "theirs.txt")
      _git(other, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "-m", "theirs")
      _git(other, "push", "-q", "origin", "assets")
    return commit

  monkeypatch.setattr(shots, "_assets_commit", race_then_build)
  commit = fork["publish"]()
  assert _git(fork["remote"], "show", f"{commit}:theirs.txt") == "another send"
  assert _git(fork["remote"], "rev-parse", "refs/heads/assets") == commit


# --- Description references ---------------------------------------------------


def test_code_examples_of_the_syntax_are_neither_checked_nor_rewritten():
  body = "Write `![alt](screenshot:NAME)` to attach one.\n\n```\n![x](screenshot:y.png)\n```\n"
  assert shots.references(body) == []
  shots.check_references([], body)
  assert shots.resolve_body(body, [], assets_repo="o/r", commit="a" * 40, branch="b") == body


def test_a_linked_screenshot_pins_both_the_image_and_its_link():
  body = "[![Wide](screenshot:a.png)](screenshot:a.png)"
  entries = [{"name": "a.png"}]
  resolved = shots.resolve_body(body, entries, assets_repo="o/r", commit="a" * 40, branch="b")
  url = shots.raw_url("o/r", "a" * 40, "b", "a.png")
  assert resolved == f"[![Wide]({url})]({url})"


# --- Send ordering and refusals ---------------------------------------------------


def _send_screenshots(fork, **overrides):
  args = dict(
    record={}, diff_path=fork["record_path"].with_suffix(".diff"),
    repo=Path("/nonexistent"), screenshots=fork["entries"], body=BODY,
    upstream_repo="octo/project", login="octocat", branch="fix/header",
    author=("octocat", "e"), is_update=False, record_patch={"k": "v"},
  )
  args.update(overrides)
  return _publish_reviewed_screenshots(
    args.pop("record"), args.pop("diff_path"), args.pop("repo"),
    args.pop("screenshots"), args.pop("body"), **args,
  )


def test_an_oversized_pinned_body_is_refused_before_any_upload(fork, monkeypatch):
  from app import github_contributions
  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", lambda *_: pytest.fail("forked"))
  with pytest.raises(ContributionSubmitError) as raised:
    # Within the limit as written, over it once both links are pinned.
    _send_screenshots(fork, body=BODY + "x" * (65_500 - len(BODY)))
  assert raised.value.code == "screenshot_body_too_large"


def test_a_fork_that_cannot_be_made_stops_send_with_a_clear_reason(fork, monkeypatch):
  from app import github_contributions

  def no_fork(*_):
    raise ContributionSubmitError("forking is disabled for this repository")

  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", no_fork)
  with pytest.raises(ContributionSubmitError) as raised:
    _send_screenshots(fork)
  assert raised.value.code == "screenshot_upload_failed"
  assert "without screenshots" in raised.value.message
  assert raised.value.record_patch == {"k": "v"}


def test_send_pins_the_body_and_remembers_the_upload(fork, monkeypatch):
  from app import github_contributions
  monkeypatch.setattr(github_contributions, "_ensure_owner_fork_remote", lambda *_: "octocat/project")
  monkeypatch.setattr(
    github_contributions.contribution_screenshots, "publish",
    lambda *a, **k: "b" * 40,
  )
  body, patch = _send_screenshots(fork)
  assert "screenshot:" not in body
  assert patch == {"k": "v", "last_submit_screenshots": {"repo": "octocat/project", "commit": "b" * 40}}


def test_the_image_route_is_never_cached(staging, tmp_path, client, owner_token):
  staging["new_record"](body_draft=BODY, screenshots=_items(tmp_path))
  served = client.get(
    f"/api/github/contributions/{staging['app_id']}/greet/screenshots/before.png",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert served.headers["cache-control"] == "no-store"
  missing = client.get(
    f"/api/github/contributions/{staging['app_id']}/greet/screenshots/none.png",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert missing.status_code == 404


def test_an_open_pr_update_cannot_stage_screenshots(staging, tmp_path, monkeypatch):
  import json
  from app.routes import github as github_routes
  record = staging["new_record"]().json()["record"]
  path = (
    Path(get_settings().data_dir) / "apps" / str(staging["app_id"])
    / "contributions" / "greet.json"
  )
  path.write_text(json.dumps({
    **record, "status": "open", "number": 7,
    "url": "https://github.com/octo/project/pull/7", "head_repository": "octo/project",
  }))
  monkeypatch.setattr(github_routes, "_autopilot_live_target", lambda *_: {
    "error": None, "head_sha": record["plan"]["head_sha"], "base_branch": "main",
    "base_sha": staging["base"], "title": "T", "body": "B",
  })
  refused = staging["stage"](screenshots=_items(tmp_path))
  assert refused.status_code == 422
  assert refused.json()["detail"]["code"] == "stage_public_text"
