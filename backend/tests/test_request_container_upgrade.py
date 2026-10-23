"""The self-hosted container-only upgrade request (scripts/request-container-upgrade.py)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "request-container-upgrade.py"
SPEC = importlib.util.spec_from_file_location("request_container_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


def _git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
     "-c", "commit.gpgsign=false", *args],
    capture_output=True, text=True, check=True,
  ).stdout.strip()


# The release images' base: what ``target_environment`` reads the Python
# version from.
_DOCKERFILE = "FROM python:3.12-slim-trixie AS python-runtime\nFROM python-runtime\n"
# Marker values of an image built from ``_DOCKERFILE`` on this host.
_ENVIRONMENT = {
  **{name: value for name, value in upgrade.default_environment().items()
     if name not in {"python_full_version", "implementation_version"}},
  "python_version": "3.12",
}


def _commit(
  repo: Path, name: str, lock: str | None = None, *, bridge: bool = False,
  requirements: str | None = None, dockerfile: str | None = None,
) -> str:
  (repo / name).write_text(name + "\n")
  if dockerfile is not None:
    (repo / "Dockerfile").write_text(dockerfile)
  for path, content in (("requirements.lock", lock), ("requirements.txt", requirements)):
    if content is not None:
      (repo / "backend").mkdir(exist_ok=True)
      (repo / "backend" / path).write_text(content)
  if bridge:
    for required in upgrade.BRIDGE_FILES:
      (repo / required).parent.mkdir(parents=True, exist_ok=True)
      (repo / required).write_text("1\n")
  _git(repo, "add", "-A")
  _git(repo, "commit", "-q", "-m", name)
  return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def install(tmp_path, monkeypatch):
  """An installation running image ``old``, source at release ``old``, with
  ``new`` fetched as origin/main and an idle helper."""
  data = tmp_path / "data"
  platform = data / "platform"
  platform.mkdir(parents=True)
  _git(platform, "init", "-q", "-b", "main")
  old = _commit(platform, "old-release", lock="pkg==1\n", dockerfile=_DOCKERFILE)
  new = _commit(platform, "new-release", lock="pkg==2\n", bridge=True)
  _git(platform, "reset", "-q", "--hard", old)
  _git(platform, "branch", "-f", "upstream", old)
  _git(platform, "update-ref", "refs/remotes/origin/main", new)
  control = data / "mobius-rebuild"
  (control / "inbox").mkdir(parents=True)
  (control / "status.json").write_text(json.dumps({"state": "succeeded"}))
  build_info = tmp_path / "build-info.json"
  build_info.write_text(json.dumps({"sha": old}))
  monkeypatch.setattr(upgrade, "DATA", data)
  monkeypatch.setattr(upgrade, "PLATFORM", platform)
  monkeypatch.setattr(upgrade, "CONTROL", control)
  monkeypatch.setattr(upgrade, "BUILD_INFO", build_info)
  monkeypatch.setattr(upgrade, "RECONCILE_LOCK", data / ".platform-reconcile.lock")
  # The fixture's first release stands in for the real #1311 floor.
  monkeypatch.setattr(upgrade, "SERVED_CODE_FLOOR", old)
  return {"data": data, "control": control, "old": old, "new": new, "platform": platform}


def _request(install) -> Path:
  return install["control"] / "inbox" / "request.json"


def test_requests_only_the_container_for_the_fetched_release(install, capsys):
  assert upgrade.main([]) == 0

  assert json.loads(_request(install).read_text()) == {
    "version": 1, "expected_sha": install["new"],
  }
  assert "install the update from Settings" in capsys.readouterr().out
  # Nothing else changed: the source still serves its own release.
  assert _git(install["platform"], "rev-parse", "HEAD") == install["old"]


def test_check_reports_readiness_without_requesting(install, capsys):
  assert upgrade.main(["--check"]) == 0
  assert "Ready" in capsys.readouterr().out
  assert not _request(install).exists()


def test_an_explicit_target_must_be_a_complete_fetched_release(install):
  assert upgrade.main(["--target", install["new"][:12]]) == 1
  assert upgrade.main(["--target", "f" * 40]) == 1
  assert not _request(install).exists()
  assert upgrade.main(["--target", install["new"]]) == 0


@pytest.mark.parametrize("case", [
  "same_image", "older_than_source_release", "pending_update", "parked_update",
  "helper_running", "request_queued", "helper_missing", "target_without_bridge",
  "no_official_history", "served_code_too_old", "target_packages_differ",
])
def test_refuses_without_touching_the_inbox_when_a_precondition_fails(
  install, case, capsys,
):
  data, control = install["data"], install["control"]
  upgrade_target: list[str] = []
  if case == "same_image":
    upgrade.BUILD_INFO.write_text(json.dumps({"sha": install["new"]}))
  elif case == "older_than_source_release":
    # The installed source took packages from an official release newer than
    # the requested target.
    later = _commit(install["platform"], "later-release", lock="pkg==3\n", bridge=True)
    _git(install["platform"], "update-ref", "refs/remotes/origin/main", later)
    _git(install["platform"], "branch", "-f", "upstream", later)
    upgrade_target.append(install["new"])
  elif case == "target_without_bridge":
    bare = _commit(install["platform"], "release-before-bridge", lock="pkg==2\n")
    _git(install["platform"], "reset", "-q", "--hard", install["old"])
    _git(install["platform"], "update-ref", "refs/remotes/origin/main", bare)
  elif case == "served_code_too_old":
    upgrade.SERVED_CODE_FLOOR = install["new"]
  elif case == "target_packages_differ":
    # An explicit historical target whose packages are not the latest's.
    first = _commit(install["platform"], "first-bridge", lock="pkg==2\n", bridge=True)
    latest = _commit(install["platform"], "latest", lock="pkg==4\n", bridge=True)
    _git(install["platform"], "reset", "-q", "--hard", install["old"])
    _git(install["platform"], "update-ref", "refs/remotes/origin/main", latest)
    upgrade_target.append(first)
  elif case == "no_official_history":
    _git(install["platform"], "update-ref", "-d", "refs/remotes/origin/main")
    upgrade_target.append(install["new"])
  elif case == "pending_update":
    (data / ".platform-prepared-update.json").write_text("{}")
  elif case == "parked_update":
    (data / ".platform-conflict").write_text("x")
  elif case == "helper_running":
    (control / "status.json").write_text(json.dumps({"state": "replacing"}))
  elif case == "request_queued":
    _request(install).write_text("{}")
  elif case == "helper_missing":
    (control / "status.json").unlink()

  before = _request(install).read_text() if _request(install).exists() else None
  args = ["--target", upgrade_target[0]] if upgrade_target else []

  assert upgrade.main(args) == 1

  after = _request(install).read_text() if _request(install).exists() else None
  assert after == before
  assert "Container upgrade not requested" in capsys.readouterr().err


@pytest.mark.parametrize("installed,allowed", [(None, False), (5, False), (True, False), (6, True)])
def test_an_uncertified_helper_is_asked_to_reinstall_instead_of_upgrading(
  install, installed, allowed, capsys,
):
  # Production 2026-10-12: a helper whose installer never certified the
  # release's protocol ran its old cutover again. Refuse before the inbox.
  target = _commit(install["platform"], "helper-protocol-release", lock="pkg==2\n", bridge=True)
  (install["platform"] / "deployment").mkdir(exist_ok=True)
  (install["platform"] / "deployment/self-hosted-helper.required").write_text("6\n")
  _git(install["platform"], "add", "-A")
  _git(install["platform"], "commit", "-q", "-m", "require helper protocol 6")
  target = _git(install["platform"], "rev-parse", "HEAD")
  _git(install["platform"], "reset", "-q", "--hard", install["old"])
  _git(install["platform"], "update-ref", "refs/remotes/origin/main", target)
  status = {"state": "succeeded"}
  if installed is not None:
    status["helper_protocol"] = installed
  (install["control"] / "status.json").write_text(json.dumps(status))

  assert (upgrade.main([]) == 0) is allowed
  assert _request(install).exists() is allowed
  if not allowed:
    assert "install-rebuild-helper.sh" in capsys.readouterr().err


def test_a_release_marker_on_a_local_commit_does_not_block_the_upgrade(install):
  """Older updaters could record a local merge commit as the installed
  release. The target's own history still proves its packages are newer."""
  local = _commit(install["platform"], "local-reconcile")
  _git(install["platform"], "branch", "-f", "upstream", local)

  assert upgrade.main(["--check"]) == 0


def test_check_refuses_when_target_would_drop_a_local_package_pin(install, capsys):
  """A local commit pins a package the target image does not install; the
  upgrade would lose the live install."""
  local = _commit(install["platform"], "local-pin", lock="pkg==1\nlocal==1\n")

  assert upgrade.main(["--check"]) == 1

  err = capsys.readouterr().err
  assert "local: source declares ==1, target image does not install it" in err
  assert f"revert local commit {local[:7]}" in err
  assert not _request(install).exists()


def test_check_names_the_lost_version_of_a_locally_bumped_pin(install, capsys):
  _commit(install["platform"], "local-bump", lock="pkg==3\n")

  assert upgrade.main(["--check"]) == 1
  assert "pkg: source declares ==3, target image installs 2" in capsys.readouterr().err


def test_allow_package_loss_overrides(install):
  _commit(install["platform"], "local-pin", lock="pkg==1\nlocal==1\n")

  assert upgrade.main(["--allow-package-loss"]) == 0
  assert _request(install).exists()


def test_check_allows_target_whose_history_declares_served_packages(install):
  """A source on an older release's packages is covered by the newer image."""
  (install["platform"] / "unrelated.txt").write_text("local\n")

  assert upgrade.main(["--check"]) == 0


def test_target_that_drops_a_distribution_the_source_release_declares_needs_the_override(
  install, capsys,
):
  """History proves the source's release is older than the target, not that
  the target still installs its packages. A dropped one may be unused by
  then, or provided another way (the image's python-jose shim), which the
  lock cannot show: refused by default, naming it, and the owner may accept
  it (official 6c6a8e599 to 12ee0eb4 drops test-only httpx2)."""
  platform = install["platform"]
  release = _commit(platform, "release-with-needed", lock="pkg==1\nneeded==1\n")
  target = _commit(platform, "release-drops-needed", lock="pkg==2\n", bridge=True)
  _git(platform, "update-ref", "refs/remotes/origin/main", target)
  _git(platform, "reset", "-q", "--hard", release)
  _git(platform, "branch", "-f", "upstream", release)
  upgrade.BUILD_INFO.write_text(json.dumps({"sha": release}))

  assert upgrade.main(["--check"]) == 1
  err = capsys.readouterr().err
  assert "no longer installs Python packages" in err and "(needed)" in err
  assert "--allow-package-loss" in err
  assert not _request(install).exists()

  assert upgrade.main(["--allow-package-loss"]) == 0
  assert json.loads(_request(install).read_text())["expected_sha"] == target
  # Only the container moves: the source keeps its release's declaration.
  assert _git(platform, "rev-parse", "HEAD") == release
  assert "needed==1" in (platform / "backend/requirements.lock").read_text()


def test_an_unsatisfied_local_range_is_refused_even_with_the_official_lock_unchanged(
  install, capsys,
):
  """A range is judged by what it admits, not only an exact pin: the target
  installs pkg 2, which a local ``pkg>=3`` does not accept."""
  local = _commit(install["platform"], "local-range", requirements="pkg>=3\n")

  assert upgrade.main(["--check"]) == 1

  err = capsys.readouterr().err
  assert "pkg: source declares >=3, target image installs 2" in err
  assert f"revert local commit {local[:7]}" in err


@pytest.mark.parametrize("declared", ["pkg>=2\n", "pkg~=2.0\n", "pkg==2.*\n", "pkg>=1,<3\n", "pkg\n"])
def test_a_local_declaration_the_target_version_satisfies_is_allowed(install, declared):
  _commit(install["platform"], "local-range", requirements=declared)

  assert upgrade.main(["--check"]) == 0


def test_a_wildcard_pin_accepts_a_matching_release_version():
  assert upgrade.package_loss(b"pkg==2.*\n", {"pkg": "==2.3"}, _ENVIRONMENT) == []
  assert upgrade.package_loss(b"pkg==2.*\n", {"pkg": "==3.0"}, _ENVIRONMENT) == [
    "pkg: source declares ==2.*, target image installs 3.0",
  ]


def test_a_declaration_the_check_cannot_judge_is_refused_not_passed(install, capsys):
  _commit(install["platform"], "local-url", requirements="pkg @ https://example.invalid/pkg.whl\n")

  assert upgrade.main(["--check"]) == 1
  assert "which this check cannot verify" in capsys.readouterr().err


@pytest.mark.parametrize(("declared", "joined"), [
  ("pkg>=3\npkg<4\n", ">=3,<4"),  # the later line alone admits the target's 2
  ("pkg<4\nPkg>=3\n", "<4,>=3"),
  ("pkg<4\npKg >= 3 ; python_version >= '3'\n", "<4,>=3"),
])
def test_every_declaration_of_a_package_must_hold(install, capsys, declared, joined):
  """Repeated declarations, however the name is spelled, all constrain it."""
  _commit(install["platform"], "local-repeat", requirements=declared)

  assert upgrade.main(["--check"]) == 1
  assert f"pkg: source declares {joined}, target image installs 2" in capsys.readouterr().err


@pytest.mark.parametrize("declared", [
  # Mutually exclusive pins: only the target's own (3.12) applies.
  "pkg==1 ; python_version < '3.12'\npkg==2 ; python_version >= '3.12'\n",
  # Declared only for another platform: nothing the target must install.
  "pkg\nelsewhere==1 ; sys_platform == 'no-such-platform'\n",
])
def test_a_declaration_for_another_environment_constrains_nothing(install, declared):
  _commit(install["platform"], "local-markers", requirements=declared)

  assert upgrade.main(["--check"]) == 0


def test_a_marker_the_target_environment_cannot_decide_is_unverifiable(install, capsys):
  """Without a single ``python:X.Y`` base in the target's Dockerfile, its
  Python version is unknown: a version marker is reported, not guessed."""
  platform = install["platform"]
  _git(platform, "checkout", "-q", install["new"])
  later = _commit(platform, "later-release", dockerfile="FROM debian:trixie\n", bridge=True)
  _git(platform, "checkout", "-q", "main")
  _git(platform, "update-ref", "refs/remotes/origin/main", later)
  assert "python_version" not in upgrade.target_environment(later)
  assert upgrade.target_environment(install["new"])["python_version"] == "3.12"
  line = "pkg==2 ; python_version >= '3.12'"
  _commit(platform, "local-marker", requirements=f"{line}\n")

  assert upgrade.main(["--check"]) == 1
  assert f"{line}: a declaration this check cannot verify" in capsys.readouterr().err


def test_repeated_and_differently_spelled_names_join_into_one_spec():
  from app import platform_update

  data = b"Some_Pkg>=3\nsome-pkg<4\nsome.pkg\nother==1\n"
  expected = {"some-pkg": ">=3,<4", "other": "==1"}
  assert upgrade.parse_requirements(data, _ENVIRONMENT) == expected
  assert platform_update.declared_python_packages(data) == expected
  assert platform_update.unmet_declaration(">=3,<4", "2.0") == "version"
  assert platform_update.unmet_declaration(">=3,<4", "3.5") is None


@pytest.mark.parametrize("line", [
  "-e ./local-package", "--editable=./local-package", "-r local-requirements.txt",
  "-c constraints.txt", "./local-package", "/abs/local-package", "local-package/",
  "https://example.invalid/pkg.tar.gz", "git+https://example.invalid/pkg",
  "pkg-1.0-py3-none-any.whl",
])
def test_a_dependency_line_naming_no_package_is_unsupported_not_ignored(
  install, capsys, line,
):
  """Only comments and pure options are skipped: a dependency the check
  cannot name could disappear with the replaced container."""
  from app import platform_update

  data = f"{line}\n--index-url https://example.invalid\n--hash=sha256:aa\n# note\n".encode()
  assert upgrade.parse_requirements(data, _ENVIRONMENT) == {line: None}
  assert platform_update.declared_python_packages(data) == {line: None}
  assert upgrade.unmet_declaration(None, None) == "unsupported"
  assert platform_update.unmet_declaration(None, "1.0") == "unsupported"

  _commit(install["platform"], "local-reference", requirements=f"{line}\n")
  assert upgrade.main(["--check"]) == 1
  err = capsys.readouterr().err
  assert f"{line}: a declaration this check cannot verify" in err
  assert "--allow-package-loss" in err
  assert upgrade.main(["--check", "--allow-package-loss"]) == 0


_REQUIREMENT_CORPUS = (
  b"fastapi[standard]>=0.110 ; python_version >= '3.11'  # web\n"
  b"Some_Pkg.Name==1.2.3 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb\n"
  b"-r other.txt\n--index-url https://example.invalid\n# comment\n\n"
  b"wild==2.*\nranged>=1,<3\ncompat~=2.0\nbare\nurl @ https://example.invalid/u.whl\n"
  b"split==1 ; python_version < '3.12'\nsplit==2 ; python_version >= '3.12'\n"
  b"elsewhere==1 ; sys_platform == 'no-such-platform'\n"
  b"exact ==1 ; python_full_version == '3.12.1'\n"
)


def test_the_script_and_backend_requirement_judgments_agree():
  """The script keeps a copy of the backend's parser and version judgment
  (it runs from fetched upstream and cannot import the backend)."""
  from app import platform_update

  repo = Path(__file__).resolve().parents[2]
  for data in (
    _REQUIREMENT_CORPUS,
    (repo / "backend/requirements.txt").read_bytes(),
    (repo / "backend/requirements.lock").read_bytes(),
  ):
    for environment in (_ENVIRONMENT, {**_ENVIRONMENT, "python_version": "3.13"}):
      assert upgrade.parse_requirements(data, environment) == (
        platform_update.declared_python_packages(data, environment)
      )
  parsed = upgrade.parse_requirements(_REQUIREMENT_CORPUS, _ENVIRONMENT)
  assert parsed["-r other.txt"] is None
  assert parsed["split"] == "==2" and "elsewhere" not in parsed
  assert parsed["exact ==1 ; python_full_version == '3.12.1'"] is None
  for spec in parsed.values():
    for installed in (None, "1.0", "2.3", "3.0", "not a version"):
      assert upgrade.unmet_declaration(spec, installed) == (
        platform_update.unmet_declaration(spec, installed)
      ), (spec, installed)


def test_a_direct_requirement_the_target_keeps_as_a_dependency_is_allowed(install):
  """The image installs the lock, not requirements.txt: a package the target
  stops listing directly but still locks as a dependency is not lost."""
  platform = install["platform"]
  release = _commit(
    platform, "release-lists-needed", lock="pkg==1\nneeded==1\n",
    requirements="pkg\nneeded\n",
  )
  target = _commit(
    platform, "release-locks-needed", lock="pkg==2\nneeded==1\n",
    requirements="pkg\n", bridge=True,
  )
  _git(platform, "update-ref", "refs/remotes/origin/main", target)
  _git(platform, "reset", "-q", "--hard", release)
  _git(platform, "branch", "-f", "upstream", release)
  upgrade.BUILD_INFO.write_text(json.dumps({"sha": release}))

  assert upgrade.main(["--check"]) == 0


def test_check_allows_a_lock_change_that_drops_no_package(install):
  # Only an artifact hash differs; every declared version is installed.
  (install["platform"] / "backend/requirements.lock").write_text(
    "pkg==2 \\\n    --hash=sha256:" + "a" * 64 + "\n"
  )

  assert upgrade.main(["--check"]) == 0


def test_check_names_release_that_declares_served_packages(install, capsys):
  """Source on packages only a later official release declared needs that
  release, which the refusal names; the loss is not overridable."""
  _git(install["platform"], "reset", "-q", "--hard", install["new"])
  later = _commit(install["platform"], "later-release", lock="pkg==3\n", bridge=True)
  latest = _commit(install["platform"], "latest-release", lock="pkg==2\n", bridge=True)
  _git(install["platform"], "update-ref", "refs/remotes/origin/main", latest)
  _git(install["platform"], "reset", "-q", "--hard", later)
  _git(install["platform"], "branch", "-f", "upstream", later)

  assert upgrade.main(["--check", "--allow-package-loss", "--target", install["new"]]) == 1

  err = capsys.readouterr().err
  assert f"they come from release {later[:12]}, so choose a target that includes it" in err
  assert "pkg: source declares ==3, target image installs 2" in err


def test_a_committed_local_package_edit_under_a_local_release_marker_is_local(install, capsys):
  """The marker can name a local commit that also carries a local package
  declaration; neither makes the declaration an official newer release, so
  only the owner's acceptance of the loss is needed."""
  local = _commit(install["platform"], "local-packages", lock="pkg==1\nlocal==1\n")
  _git(install["platform"], "branch", "-f", "upstream", local)

  assert upgrade.main(["--check"]) == 1
  assert "--allow-package-loss" in capsys.readouterr().err
  assert upgrade.main(["--check", "--allow-package-loss"]) == 0


def test_checks_and_request_hold_the_updater_lock(install, monkeypatch):
  held = []
  real = upgrade.check_no_pending_update

  def observed():
    import fcntl
    with open(upgrade.RECONCILE_LOCK, "a+") as other:
      try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(False)
      except BlockingIOError:
        held.append(True)
    return real()

  monkeypatch.setattr(upgrade, "check_no_pending_update", observed)
  assert upgrade.main([]) == 0
  assert held == [True]


def test_refuses_a_shallow_checkout_whose_history_cannot_prove_anything(
  install, tmp_path, monkeypatch, capsys,
):
  """Image seeds are shallow; an input missing from shallow history is not
  proof that it is local."""
  shallow = tmp_path / "shallow"
  _git(install["platform"], "branch", "-f", "release", install["new"])
  subprocess.run(
    ["git", "clone", "-q", "--depth", "2", "--branch", "release",
     f"file://{install['platform']}", str(shallow)],
    check=True,
  )
  _git(shallow, "reset", "-q", "--hard", install["old"])
  _git(shallow, "branch", "-f", "upstream", install["old"])
  _git(shallow, "update-ref", "refs/remotes/origin/main", install["new"])
  monkeypatch.setattr(upgrade, "PLATFORM", shallow)

  assert upgrade.main(["--check"]) == 1
  assert "shallow history" in capsys.readouterr().err
