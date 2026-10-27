"""Names a local backend edit still uses after a release moved them."""

import subprocess
from pathlib import Path

import pytest

from app import removed_references


def _git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
  ).stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
  """Commit exactly ``files`` as the whole tree."""
  for path in repo.rglob("*.py"):
    path.unlink()
  for rel, text in files.items():
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
  _git(repo, "add", "-A")
  _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "tree", "--allow-empty")
  return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
  _git(tmp_path, "init", "-q")
  return tmp_path


_PACKAGE = {"backend/app/__init__.py": ""}


def _find(repo, *, baseline, release, local):
  """Owner tree ``baseline``; ``release`` plus the owner's ``local`` files as the candidate."""
  before = _commit(repo, {**_PACKAGE, **baseline, **local})
  shipped = _commit(repo, {**_PACKAGE, **release})
  candidate = _commit(repo, {**_PACKAGE, **release, **local})
  return removed_references.find(repo, shipped, candidate, before)


_MOVED_BEFORE = {"backend/app/install.py": "def read_receipt():\n  return 1\n"}
_MOVED_AFTER = {
  "backend/app/install.py": "def install():\n  pass\n",
  "backend/app/pending_update.py": "def read_receipt():\n  return 1\n",
}


def test_a_lazily_imported_module_attribute_the_release_moved_is_reported(repo):
  local = {"backend/app/routes/apps.py": (
    "def preview():\n  from app import install\n  return install.read_receipt()\n\n"
    "def other():\n  from app import install\n  return install.install\n"
  )}
  assert _find(repo, baseline=_MOVED_BEFORE, release=_MOVED_AFTER, local=local) == [
    "backend/app/routes/apps.py:3 uses app.install.read_receipt",
  ]


@pytest.mark.parametrize("source", [
  "from app.install import read_receipt\n",
  "def later():\n  from app.install import read_receipt\n",
  "import app.install as installer\nVALUE = installer.read_receipt\n",
])
def test_imported_and_aliased_names_the_release_moved_are_reported(repo, source):
  local = {"backend/app/voice.py": source}
  found = _find(repo, baseline=_MOVED_BEFORE, release=_MOVED_AFTER, local=local)
  assert [entry.split(" uses ")[1] for entry in found] == ["app.install.read_receipt"]


def test_relative_imports_resolve_from_the_referring_package(repo):
  local = {"backend/app/routes/apps.py": "from .. import install\nVALUE = install.read_receipt\n"}
  assert _find(repo, baseline=_MOVED_BEFORE, release=_MOVED_AFTER, local=local) == [
    "backend/app/routes/apps.py:2 uses app.install.read_receipt",
  ]


def test_a_removed_module_is_reported(repo):
  local = {"backend/app/voice.py": "from app import install\n"}
  release = {"backend/app/pending_update.py": "def read_receipt():\n  return 1\n"}
  assert _find(repo, baseline=_MOVED_BEFORE, release=release, local=local) == [
    "backend/app/voice.py:1 uses app.install",
  ]


@pytest.mark.parametrize("source", [
  # A rebound alias is no longer known to be the module.
  "from app import install\n\ndef run(install):\n  return install.read_receipt\n",
  # Never resolvable before the update: not this release's doing.
  "from app import install\nVALUE = install.never_existed\n",
  # The names still resolve.
  "from app import pending_update\nVALUE = pending_update.read_receipt\n",
])
def test_references_that_did_not_break_with_this_release_are_not_reported(repo, source):
  local = {"backend/app/voice.py": source}
  assert _find(repo, baseline=_MOVED_BEFORE, release=_MOVED_AFTER, local=local) == []


@pytest.mark.parametrize("source", [
  # An old-name import with a fallback to the new home works across the move.
  "try:\n  from app.install import read_receipt\n"
  "except ImportError:\n  from app.pending_update import read_receipt\n",
  "from app import install, pending_update\n\ndef read():\n"
  "  try:\n    return install.read_receipt()\n"
  "  except AttributeError:\n    return pending_update.read_receipt()\n",
  "try:\n  from app.install import read_receipt\nexcept (ModuleNotFoundError, ImportError):\n  read_receipt = None\n",
  "try:\n  from app.gone import thing\nexcept ModuleNotFoundError:\n  thing = None\n",
  # Any handler that can catch a failed lookup counts, not only the exact error.
  "try:\n  from app.install import read_receipt\nexcept AttributeError:\n  pass\n",
  "from app import install\ntry:\n  VALUE = install.read_receipt\nexcept NameError:\n  pass\n",
  "from app import install\ntry:\n  VALUE = install.read_receipt\nexcept builtins.Exception:\n  pass\n",
  "from app import install\ntry:\n  VALUE = install.read_receipt\nexcept:\n  pass\n",
])
def test_references_guarded_by_a_lookup_fallback_are_left_to_the_tests(repo, source):
  baseline = {**_MOVED_BEFORE, "backend/app/gone.py": "thing = 1\n"}
  local = {"backend/app/voice.py": source}
  assert _find(repo, baseline=baseline, release=_MOVED_AFTER, local=local) == []


@pytest.mark.parametrize("source", [
  "from app import install\ntry:\n  def read():\n    return install.read_receipt()\n"
  "except Exception:\n  read = None\nVALUE = read()\n",
  "from app import install\ntry:\n  read = lambda: install.read_receipt()\n"
  "except AttributeError:\n  read = None\n",
  "from app import install\ntry:\n  def items():\n    yield install.read_receipt()\n"
  "  values = items()\nexcept AttributeError:\n  values = ()\nVALUE = list(values)\n",
])
def test_anything_nested_in_a_handled_try_is_left_to_the_tests(repo, source):
  # The documented limit: the position is lexical, so a body the try only
  # defines is not reported even though it runs later. The tests still run.
  local = {"backend/app/voice.py": source}
  assert _find(repo, baseline=_MOVED_BEFORE, release=_MOVED_AFTER, local=local) == []


@pytest.mark.parametrize("source", [
  "try:\n  pass\nexcept ImportError:\n  from app.install import read_receipt\n",
  "try:\n  pass\nexcept ImportError:\n  pass\nelse:\n  from app.install import read_receipt\n",
  "try:\n  pass\nexcept ImportError:\n  pass\nfinally:\n  from app.install import read_receipt\n",
])
def test_a_handler_else_or_finally_is_outside_its_own_try(repo, source):
  local = {"backend/app/voice.py": source}
  found = _find(repo, baseline=_MOVED_BEFORE, release=_MOVED_AFTER, local=local)
  assert [entry.split(" uses ")[1] for entry in found] == ["app.install.read_receipt"]


def test_a_handler_for_an_unrelated_error_does_not_guard_a_moved_name(repo):
  local = {"backend/app/voice.py": (
    "try:\n  from app.install import read_receipt\nexcept ValueError:\n  pass\n"
  )}
  assert _find(repo, baseline=_MOVED_BEFORE, release=_MOVED_AFTER, local=local) == [
    "backend/app/voice.py:2 uses app.install.read_receipt",
  ]


@pytest.mark.parametrize("release_install", [
  "def __getattr__(name):\n  return name\n",
  "from app.pending_update import *\n",
])
def test_modules_that_resolve_names_dynamically_are_never_reported(repo, release_install):
  release = {**_MOVED_AFTER, "backend/app/install.py": release_install}
  local = {"backend/app/voice.py": "from app import install\nVALUE = install.read_receipt\n"}
  assert _find(repo, baseline=_MOVED_BEFORE, release=release, local=local) == []


@pytest.mark.parametrize("install", [
  "try:\n  import fast\nexcept ImportError:\n  def read_receipt():\n    return 1\n",
  "type read_receipt = int\n",
  "import os\nif (read_receipt := os.getenv('X')) is None:\n  pass\n",
  # A form the reader does not model makes the module define every name.
  "import os\nmatch os.name:\n  case 'posix':\n    def read_receipt():\n      return 1\n",
])
def test_names_defined_inside_top_level_blocks_still_resolve(repo, install):
  release = {**_MOVED_AFTER, "backend/app/install.py": install}
  local = {"backend/app/voice.py": "from app import install\nVALUE = install.read_receipt\n"}
  assert _find(repo, baseline=_MOVED_BEFORE, release=release, local=local) == []


@pytest.mark.parametrize("test", ["TYPE_CHECKING", "typing.TYPE_CHECKING"])
def test_imports_only_type_checkers_read_are_never_reported(repo, test):
  # The block never runs, so a moved name there cannot break the update.
  local = {"backend/app/voice.py": (
    f"import typing\nfrom typing import TYPE_CHECKING\nif {test}:\n"
    "  from app.install import read_receipt\n"
  )}
  assert _find(repo, baseline=_MOVED_BEFORE, release=_MOVED_AFTER, local=local) == []


def test_the_else_of_a_type_checking_block_still_runs(repo):
  local = {"backend/app/voice.py": (
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n  pass\n"
    "else:\n  from app.install import read_receipt\n"
  )}
  found = _find(repo, baseline=_MOVED_BEFORE, release=_MOVED_AFTER, local=local)
  assert [entry.split(" uses ")[1] for entry in found] == ["app.install.read_receipt"]


def test_only_files_the_local_delta_changed_are_read(repo):
  # The release's own files are consistent with the release, even when an
  # older owner tree defined something they no longer use.
  release = {**_MOVED_AFTER, "backend/app/routes/apps.py": "from app import install\n"}
  assert _find(repo, baseline=_MOVED_BEFORE, release=release, local={}) == []


def test_description_names_each_removed_reference_and_the_way_forward():
  message = removed_references.describe([
    f"backend/app/a.py:{line} uses app.b.c" for line in range(1, 8)
  ])
  assert "backend/app/a.py:1 uses app.b.c" in message
  assert "(+2 more)" in message
  assert "previous version keeps running" in message
