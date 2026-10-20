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
name, a module alias that the referring file rebinds anywhere is ignored, and a
reference whose failed lookup an enclosing ``try`` catches is left to the
tests: an old-name import with a fallback to the new one works across a move.
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
# The handler names that catch each failed lookup: the error itself and its
# built-in bases. A broad handler counts: the static check cannot tell what
# the fallback does, so it abstains.
_CATCHERS = {
  "AttributeError": frozenset({"AttributeError", "Exception", "BaseException"}),
  "ImportError": frozenset({"ImportError", "Exception", "BaseException"}),
  "ModuleNotFoundError": frozenset({
    "ModuleNotFoundError", "ImportError", "Exception", "BaseException",
  }),
}


def _module_name(path: str) -> str:
  parts = Path(path).with_suffix("").relative_to("backend").parts
  return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


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
    elif isinstance(node, ast.Expr) and isinstance(node.value, ast.NamedExpr):
      targets(node.value.target)
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
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


def _caught(handler: ast.ExceptHandler) -> set[str]:
  """Exception class names one handler names; a bare ``except`` catches all."""
  if handler.type is None:
    return {"BaseException"}
  kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
  return {
    kind.id if isinstance(kind, ast.Name) else kind.attr
    for kind in kinds if isinstance(kind, (ast.Name, ast.Attribute))
  }


def _called_within(body: list[ast.stmt]) -> set[int]:
  """Functions and lambdas a block both defines and calls.

  Their bodies may run while the block is still executing, so a guard around
  the block may cover them; the check abstains as it does for the block's own
  statements. A lambda counts when it is called on the spot or bound to a
  name the block calls.
  """
  nodes = [node for statement in body for node in ast.walk(statement)]
  calls = [node for node in nodes if isinstance(node, ast.Call)]
  called = {call.func.id for call in calls if isinstance(call.func, ast.Name)}
  found = {id(call.func) for call in calls if isinstance(call.func, ast.Lambda)}
  for node in nodes:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in called:
      found.add(id(node))
    elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda)
          and any(isinstance(target, ast.Name) and target.id in called
                  for target in node.targets)):
      found.add(id(node.value))
  return found


def _guards(tree: ast.Module) -> dict[int, frozenset[str]]:
  """Exception names the enclosing ``try`` handlers catch when each node runs.

  A function or lambda body runs only when called. One the ``try`` body also
  calls may run under that ``try``'s handlers and keeps their protection; any
  other starts unguarded, because it can run after the ``try`` has finished.
  ``try`` statements inside a body always count. Handlers, ``else`` and
  ``finally`` are outside their own ``try``'s protection.
  """
  guards: dict[int, frozenset[str]] = {}
  # (node, handlers catching when it runs, callables run under those handlers)
  pending: list[tuple[ast.AST, frozenset[str], frozenset[int]]] = [
    (tree, frozenset(), frozenset()),
  ]
  while pending:
    node, caught, run_here = pending.pop()
    if caught:
      guards[id(node)] = caught
    if isinstance(node, (ast.Try, ast.TryStar)):
      inner = caught.union(*(_caught(handler) for handler in node.handlers))
      run_inner = run_here | _called_within(node.body)
      pending.extend((statement, inner, run_inner) for statement in node.body)
      pending.extend(
        (part, caught, run_here)
        for part in (*node.handlers, *node.orelse, *node.finalbody)
      )
      continue
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
      deferred = {id(statement) for statement in node.body}
    elif isinstance(node, ast.Lambda):
      deferred = {id(node.body)}
    else:
      deferred = set()
    if id(node) in run_here:
      deferred = set()
    pending.extend(
      (child, frozenset(), frozenset()) if id(child) in deferred else (child, caught, run_here)
      for child in ast.iter_child_nodes(node)
    )
  return guards


class _Reference(NamedTuple):
  line: int
  module: str
  name: str
  imported: bool  # ``from module import name``, not ``module.name``
  caught: frozenset[str]  # what enclosing handlers catch when it runs


def _absorbs(reference: _Reference, candidate: _Tree) -> bool:
  """Whether a handler around ``reference`` catches the error its lookup raises."""
  if not reference.imported:
    raised = "AttributeError"
  elif candidate.is_module(reference.module):
    raised = "ImportError"
  else:
    raised = "ModuleNotFoundError"
  return not reference.caught.isdisjoint(_CATCHERS[raised])


def _references(tree: ast.Module, path: str, is_module) -> list[_Reference]:
  """Every name this file takes from the backend package."""
  module = _module_name(path)
  guards = _guards(tree)
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
        found.append(_Reference(
          node.lineno, source, alias.name, True, guards.get(id(node), frozenset()),
        ))
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
        and isinstance(node.value, ast.Name) and node.value.id in stable):
      found.append(_Reference(
        node.lineno, stable[node.value.id], node.attr, False,
        guards.get(id(node), frozenset()),
      ))
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
      if (before.defines(ref.module, ref.name) and not after.defines(ref.module, ref.name)
          and not _absorbs(ref, after)):
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
