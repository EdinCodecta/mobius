"""Find backend names a local edit still uses after the release moved them.

A text-clean merge can keep an owner's edit that calls ``module.name`` after
the release moved ``name`` to another module. The candidate still imports, so
the import probe passes, and the call fails only when a request reaches it.
Reading the edited files is enough to see such a reference, in seconds and
without running any tests.

A reference is reported only when it resolved on the owner's previous tree and
does not on the candidate: the same pass-before/fail-after rule as the local
tests, so names that were already dynamic or unresolvable never block an
update. Resolution is static and deliberately conservative. A module with a
module-level ``__getattr__`` or a star import is treated as defining every
name, a module alias that the referring file rebinds anywhere is ignored, and
references inside a handled ``try`` or an ``if TYPE_CHECKING:`` block are
never reported: an old-name import with a fallback to the new one works across
a move, and the tests judge it. A module statement the reader does not model
(such as ``match``) makes the module define every name.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import NamedTuple

from app import app_git

PACKAGE = "app"
SOURCE_ROOT = "backend/app"
_DYNAMIC = object()
# Handlers that can catch a failed lookup. A bare ``except`` counts too.
_LOOKUP_HANDLERS = frozenset({
  "AttributeError", "ImportError", "ModuleNotFoundError", "NameError",
  "Exception", "BaseException",
})


def _module_name(path: str) -> str:
  parts = Path(path).with_suffix("").relative_to("backend").parts
  return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


_TYPE_ALIAS = getattr(ast, "TypeAlias", ())
# Statements whose own bindings, if any, the walrus scan below covers.
_BINDS_NOTHING_ITSELF = (
  ast.Expr, ast.If, ast.While, ast.Try, ast.TryStar, ast.ExceptHandler, ast.Pass,
  ast.Raise, ast.Assert, ast.Delete, ast.Global, ast.Nonlocal, ast.Return,
  ast.Break, ast.Continue,
)


def _bound_names(body: list[ast.stmt]) -> set[str] | object:
  """Names a module's top level defines, or ``_DYNAMIC`` when it cannot say."""
  names: set[str] = set()

  def targets(node: ast.AST) -> None:
    if isinstance(node, ast.Name):
      names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
      for element in node.elts:
        targets(element)
    elif isinstance(node, ast.Starred):
      targets(node.value)

  pending = list(body)
  while pending:
    node = pending.pop()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
      names.add(node.name)
      if node.name == "__getattr__":
        return _DYNAMIC
    elif isinstance(node, ast.Assign):
      for target in node.targets:
        targets(target)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
      targets(node.target)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
      targets(node.target)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
      for item in node.items:
        if item.optional_vars is not None:
          targets(item.optional_vars)
    elif isinstance(node, ast.Import):
      names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
      if any(alias.name == "*" for alias in node.names):
        return _DYNAMIC
      names.update(alias.asname or alias.name for alias in node.names)
    elif isinstance(node, _TYPE_ALIAS):
      targets(node.name)
    elif not isinstance(node, _BINDS_NOTHING_ITSELF):
      return _DYNAMIC  # A form this reader does not model, such as ``match``.
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
      # A walrus anywhere in the statement's own expressions binds here.
      for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
          for inner in ast.walk(child):
            if isinstance(inner, ast.NamedExpr):
              targets(inner.target)
      for field in ("body", "orelse", "finalbody", "handlers"):
        pending.extend(getattr(node, field, ()) or ())
  return names


class _Tree:
  """The backend package at one revision, answering what each module defines."""

  def __init__(self, repo: Path, rev: str) -> None:
    self._sources = {
      path: data for path, data in app_git.read_ref_subtree(repo, rev, SOURCE_ROOT).items()
      if path.endswith(".py")
    }
    self._modules = {_module_name(path): path for path in self._sources}
    self._defined: dict[str, set[str] | object | None] = {}

  def source(self, path: str) -> str | None:
    data = self._sources.get(path)
    return None if data is None else data.decode("utf-8", "replace")

  def is_module(self, module: str) -> bool:
    return module in self._modules

  def defines(self, module: str, name: str) -> bool:
    if f"{module}.{name}" in self._modules:
      return True
    if module not in self._defined:
      try:
        tree = ast.parse(self.source(self._modules[module]) or "")
      except (KeyError, SyntaxError):
        self._defined[module] = None
      else:
        self._defined[module] = _bound_names(tree.body)
    names = self._defined[module]
    return names is _DYNAMIC or (names is not None and name in names)


def _absolute(module: str, node: ast.ImportFrom, path: str) -> str | None:
  if not node.level:
    return node.module
  package = module.split(".") if path.endswith("__init__.py") else module.split(".")[:-1]
  if node.level - 1 > len(package):
    return None
  base = package[:len(package) - (node.level - 1)]
  return ".".join([*base, *([node.module] if node.module else [])])


def _catches_lookup(handler: ast.ExceptHandler) -> bool:
  if handler.type is None:
    return True
  kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
  return any(
    (kind.id if isinstance(kind, ast.Name) else kind.attr) in _LOOKUP_HANDLERS
    for kind in kinds if isinstance(kind, (ast.Name, ast.Attribute))
  )


def _guarded(tree: ast.Module) -> set[int]:
  """Every node in the body of a ``try`` that has a lookup-catching handler,
  or of an ``if TYPE_CHECKING:`` block, which never runs.

  The position is lexical: whatever the body nests (a function, a lambda, a
  generator) counts, even when it runs after the ``try`` has finished. A
  missed break here is still caught by the tests; a false rejection would
  block a working update, so the check abstains.
  """
  guarded: set[int] = set()
  for node in ast.walk(tree):
    if ((isinstance(node, (ast.Try, ast.TryStar))
         and any(_catches_lookup(handler) for handler in node.handlers))
        or (isinstance(node, ast.If) and _is_type_checking(node.test))):
      guarded.update(id(child) for statement in node.body for child in ast.walk(statement))
  return guarded


def _is_type_checking(test: ast.expr) -> bool:
  """``if TYPE_CHECKING:`` or ``if typing.TYPE_CHECKING:``, whose body never runs."""
  return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
    isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
  )


class _Reference(NamedTuple):
  line: int
  module: str
  name: str


def _references(tree: ast.Module, path: str, is_module) -> list[_Reference]:
  """Every name this file takes from the backend package."""
  module = _module_name(path)
  guarded = _guarded(tree)
  found: list[_Reference] = []
  # Every binding of a name: the backend module an import binds it to, or
  # None for any other binding. Repeating one import inside each function is
  # common and keeps the name an alias; any other binding makes it unknowable.
  bindings: dict[str, set[str | None]] = {}

  def bind(name: str, target: str | None = None) -> None:
    bindings.setdefault(name, set()).add(target)

  for node in ast.walk(tree):
    if isinstance(node, ast.ImportFrom):
      source = _absolute(module, node, path)
      if not source or not (source == PACKAGE or source.startswith(f"{PACKAGE}.")):
        for alias in node.names:
          bind(alias.asname or alias.name)
        continue
      for alias in node.names:
        if alias.name == "*":
          continue
        if id(node) not in guarded:
          found.append(_Reference(node.lineno, source, alias.name))
        target = f"{source}.{alias.name}"
        bind(alias.asname or alias.name, target if is_module(target) else None)
    elif isinstance(node, ast.Import):
      for alias in node.names:
        if alias.asname and alias.name.startswith(f"{PACKAGE}.") and is_module(alias.name):
          bind(alias.asname, alias.name)
        else:
          bind(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
      bind(node.id)
    elif isinstance(node, ast.arg):
      bind(node.arg)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
      bind(node.name)
    elif isinstance(node, ast.ExceptHandler) and node.name:
      bind(node.name)
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
      for name in node.names:
        bind(name)
    elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
      bind(node.name)
    elif isinstance(node, ast.MatchMapping) and node.rest:
      bind(node.rest)

  stable = {
    name: next(iter(targets)) for name, targets in bindings.items()
    if len(targets) == 1 and None not in targets
  }
  for node in ast.walk(tree):
    if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name) and node.value.id in stable
        and id(node) not in guarded):
      found.append(_Reference(node.lineno, stable[node.value.id], node.attr))
  return found


def find(repo: Path, release: str, candidate: str, baseline: str) -> list[str]:
  """``path:line uses module.name`` for each name the release took away.

  Only backend files the local delta ``release..candidate`` changed are read;
  every other file on the candidate is the release's own consistent source.
  """
  changed = [
    path for path in subprocess.run(
      ["git", "-C", str(repo), "diff", "--name-only", "--no-renames", "--diff-filter=d",
       release, candidate, "--", f"{SOURCE_ROOT}/"],
      capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    if path.endswith(".py")
  ]
  if not changed:
    return []
  after, before = _Tree(repo, candidate), _Tree(repo, baseline)

  def is_module(name: str) -> bool:
    return after.is_module(name) or before.is_module(name)

  removed = set()
  for path in changed:
    try:
      tree = ast.parse(after.source(path) or "")
    except SyntaxError:
      continue  # The import probe owns syntax errors.
    for ref in _references(tree, path, is_module):
      if before.defines(ref.module, ref.name) and not after.defines(ref.module, ref.name):
        removed.add((path, ref.line, f"{ref.module}.{ref.name}"))
  return [f"{path}:{line} uses {name}" for path, line, name in sorted(removed)]


def describe(removed: list[str]) -> str:
  shown = "; ".join(removed[:5])
  more = f" (+{len(removed) - 5} more)" if len(removed) > 5 else ""
  return (
    "Your local changes use names this release moved or removed: "
    f"{shown}{more}. The previous version keeps running; repair the local "
    "changes for this release, then update again."
  )
