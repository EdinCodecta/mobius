#!/usr/bin/env python3
"""Ask the self-hosted replacement helper for a container-only upgrade.

An installation whose running image predates the image-owned boot transaction
(``backend/runtime/boot-protocol``) cannot take a release that changes Python
packages: its served code refuses to install source for packages its image
lacks. This moves only the container to a newer official image and leaves the
served source untouched. The new image's boot transaction then checks that
unchanged source against itself (an image newer than the source's release is
allowed) and probes it, with the image's own release as the floor. After that,
Settings' ordinary update installs the release, because its packages are
already in the image.

Run it inside the Möbius container as the ``mobius`` user, from the fetched
upstream so an older installation needs nothing new installed::

    git -C /data/platform fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
    # (add --unshallow when the checkout is still the image's shallow seed)
    git -C /data/platform show origin/main:scripts/request-container-upgrade.py \
      | python3 - [--check] [--target <40-hex official release>]

It changes nothing but the helper's inbox, and only after these checks pass:
the target is a fetched official release that ships this bridge, descends
from the running image, and is not older than any official Python package
declaration of the installed source; the target image installs every package
the installed source's release declares and satisfies every local declaration
(``--allow-package-loss`` accepts either loss knowingly, for example a package
the target image provides another way); no update is prepared or parked; and the
helper is installed and idle. They hold the updater's lock while checking and
requesting. The helper must also have certified the target's
``deployment/self-hosted-helper.required`` protocol, so an outdated helper is
asked to be reinstalled instead of running its old cutover. The helper then drains chats, replaces the
container, verifies it, and restores the previous container if it is not
healthy. See scripts/CONTAINER-REBUILD.md.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

try:
    from packaging.markers import (
        InvalidMarker, Marker, UndefinedComparison, UndefinedEnvironmentName,
        default_environment,
    )
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version
except ImportError:  # only a transitive dependency; pip vendors the same library
    from pip._vendor.packaging.markers import (
        InvalidMarker, Marker, UndefinedComparison, UndefinedEnvironmentName,
        default_environment,
    )
    from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
    from pip._vendor.packaging.version import InvalidVersion, Version

DATA = Path(os.environ.get("DATA_DIR", "/data"))
PLATFORM = DATA / "platform"
CONTROL = DATA / "mobius-rebuild"
BUILD_INFO = Path(os.environ.get("MOBIUS_BUILD_INFO_PATH", "/app/build-info.json"))
ACTIVE_STATES = {"queued", "preparing", "replacing", "verifying"}
PYTHON_INPUTS = ("backend/requirements.txt", "backend/requirements.lock")
# What an image installs: the Dockerfile installs only the lock, so a package
# requirements.txt no longer lists can still be installed as a dependency.
INSTALLED_INVENTORY = "backend/requirements.lock"
BRIDGE_FILES = ("backend/runtime/boot-protocol", "scripts/request-container-upgrade.py")
RECONCILE_LOCK = DATA / ".platform-reconcile.lock"
# Served code from this release on discounts package inputs the running image
# already carries (#1311), so after the upgrade it accepts Settings' update.
SERVED_CODE_FLOOR = "531c08dc98f10dbd4ca0a11b6430c97b0a5e3056"
SHA_RE = re.compile(r"[0-9a-f]{40}")


class Refused(Exception):
    """A precondition does not hold; nothing was requested."""


def git(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(PLATFORM), *args],
        capture_output=True, text=True, check=False, env=env, timeout=120,
    )


def resolve(ref: str) -> str:
    out = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else ""


def resolve_blob(commit: str, path: str) -> str:
    out = git("rev-parse", "--verify", "--quiet", f"{commit}:{path}")
    return out.stdout.strip() if out.returncode == 0 else ""


def descends(ancestor: str, descendant: str) -> bool:
    return git("merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def running_image() -> str:
    try:
        sha = str(json.loads(BUILD_INFO.read_text(encoding="utf-8")).get("sha") or "")
    except (OSError, ValueError):
        sha = ""
    if not SHA_RE.fullmatch(sha):
        raise Refused("this container does not record its own release")
    return sha


def declaring_commits(commit: str, path: str, data: bytes) -> list[str]:
    """Commits ``commit`` contains whose ``path`` held exactly ``data``, oldest
    first, merge results included.

    Mirrors the boot transaction's own judgment
    (``platform_update._declared_in_history``), which the target image applies
    before it serves the unchanged source.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    blob = subprocess.run(
        ["git", "-C", str(PLATFORM), "hash-object", "--stdin"],
        input=data, capture_output=True, check=True, env=env, timeout=120,
    ).stdout.decode().strip()
    history = subprocess.run(
        ["git", "-C", str(PLATFORM), "log", "-m", "--reverse", "--format=commit %H",
         "--raw", "--no-abbrev", "--no-renames", commit, "--", path],
        capture_output=True, text=True, check=False, env=env, timeout=120,
    )
    if history.returncode != 0:
        return []
    found: list[str] = []
    current = ""
    for line in history.stdout.splitlines():
        if line.startswith("commit "):
            current = line.split()[1]
            continue
        fields = line.split("\t", 1)[0].split()
        if len(fields) >= 4 and blob in fields[2:4] and current not in found:
            found.append(current)
    return found


def declared_in_history(commit: str, path: str, data: bytes) -> bool:
    """Whether ``path`` held exactly ``data`` at some commit ``commit`` contains."""
    return bool(declaring_commits(commit, path, data))


# Mirrors ``platform_update.marker_applies``,
# ``platform_update.declared_python_packages`` and
# ``platform_update.unmet_declaration``; this script runs from fetched upstream
# on older installations, so it cannot import them.
# ``test_request_container_upgrade`` pins that the copies agree.
_REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?")
_DEPENDENCY_OPTION = re.compile(r"-[erc]|--(?:editable|requirement|constraint)\b")
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
_BASE_PYTHON = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(?:\S+/)?python:(\d+\.\d+)\b", re.M | re.I)


def target_environment(target: str) -> dict[str, str]:
    """The environment-marker values of ``target``'s image, as far as they
    can be established without running it.

    This script runs inside the Möbius container, and the helper replaces it
    on the same host with an official image built from the same Dockerfile
    lineage (the official CPython image on Linux), so the host and
    implementation values (``sys_platform``, ``platform_machine``,
    ``implementation_name`` and the like) are this interpreter's. What a
    release can change is its Python version: ``python_version`` comes from
    the target Dockerfile's ``python:X.Y`` base image, and is left out when
    that is not a single tag. Finer values that tag does not fix
    (``python_full_version``, ``implementation_version``) are left out too, so
    a declaration whose marker needs one is reported unverifiable instead of
    judged against a guessed version.
    """
    environment = dict(default_environment())
    for name in ("python_version", "python_full_version", "implementation_version"):
        environment.pop(name, None)
    versions = set(_BASE_PYTHON.findall((show(target, "Dockerfile") or b"").decode("utf-8", "replace")))
    if len(versions) == 1:
        environment["python_version"] = versions.pop()
    return environment


def marker_applies(marker: str, environment: dict[str, str]) -> bool | None:
    """Whether a declaration's environment ``marker`` holds in ``environment``,
    or None when it cannot be judged there: malformed, or naming a value
    ``environment`` does not establish (never filled in from this interpreter)."""
    try:
        parsed = Marker(marker)
    except InvalidMarker:
        return None
    names = set(re.findall(r"\w+", re.sub(r"'[^']*'|\"[^\"]*\"", "", str(parsed))))
    if not names - {"and", "or", "not", "in"} <= environment.keys():
        return None
    try:
        return parsed.evaluate(dict(environment))
    except (UndefinedComparison, UndefinedEnvironmentName):
        return None


def parse_requirements(data: bytes, environment: dict[str, str]) -> dict[str, str | None]:
    """Package name to version spec for a requirements file or pip lock, in
    the image whose marker values are ``environment`` (``target_environment``).

    Only declarations whose environment marker holds there count; repeated
    applicable declarations of a name join into one spec. A line declaring a
    dependency this cannot name or judge (``-e``, ``-r``, ``-c``, a path,
    archive or URL, or a marker ``environment`` cannot decide) maps its own
    text to None. Only comments and pure options are ignored.
    """
    packages: dict[str, str | None] = {}
    text = re.sub(r"\\\r?\n", " ", data.decode("utf-8", "replace"))
    for logical in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", logical).strip()
        if not line or (line.startswith("-") and not _DEPENDENCY_OPTION.match(line)):
            continue
        match = None if line.startswith("-") else _REQUIREMENT.match(line)
        if (
            match is None or line[match.end():match.end() + 1] in {"/", "\\", ":", "+"}
            or match.group(1).lower().endswith(_ARCHIVE_SUFFIXES)
        ):
            packages[line] = None
            continue
        requirement, _, marker = line[match.end():].partition(";")
        # Options such as ``--hash`` follow the marker on a lock entry.
        marker = re.split(r"(?:^|\s)-", marker, maxsplit=1)[0].strip()
        applies = marker_applies(marker, environment) if marker else True
        if applies is None:
            packages[line] = None
            continue
        if not applies:
            continue
        spec = "".join(token for token in requirement.split() if not token.startswith("--"))
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        packages[name] = ",".join(part for part in (packages.get(name), spec) if part)
    return packages


def unmet_declaration(spec: str | None, installed: str | None) -> str | None:
    """How ``installed`` falls short of the declared ``spec`` (``missing``,
    ``version`` or ``unsupported``), or None: PEP 440 semantics, and a
    declaration this cannot judge (a URL or path reference) never passes."""
    if spec is None:
        return "unsupported"
    if installed is None:
        return "missing"
    try:
        satisfied = SpecifierSet(spec).contains(Version(installed), prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return "unsupported"
    return None if satisfied else "version"


def locked_version(spec: str) -> str:
    """The version a lock entry installs: its exact ``==`` pin."""
    return spec[2:] if spec.startswith("==") else spec


def package_loss(
    served: bytes, installed: dict[str, str], environment: dict[str, str],
) -> list[str]:
    """What the target image (its ``INSTALLED_INVENTORY``, marker values
    ``environment``) would not satisfy of what the source declares, one
    description per package."""
    lost = []
    for name, spec in parse_requirements(served, environment).items():
        pinned = installed.get(name) if spec is not None else None
        version = locked_version(pinned) if pinned is not None else None
        kind = unmet_declaration(spec, version)
        if spec is None:
            lost.append(f"{name}: a declaration this check cannot verify against the target image")
        elif kind == "missing":
            lost.append(f"{name}: source declares {spec or 'it'}, target image does not install it")
        elif kind == "version":
            lost.append(f"{name}: source declares {spec}, target image installs {version}")
        elif kind == "unsupported":
            lost.append(
                f"{name}: source declares {spec}, which this check cannot verify "
                f"against the target image's {installed[name]}"
            )
    return lost


def show(commit: str, path: str) -> bytes | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(
        ["git", "-C", str(PLATFORM), "show", f"{commit}:{path}"],
        capture_output=True, check=False, env=env, timeout=120,
    )
    return out.stdout if out.returncode == 0 else None


def check_target(target: str) -> str:
    image = running_image()
    if target == image:
        raise Refused("this container already runs that release")
    official = resolve("refs/remotes/origin/main")
    if not official:
        raise Refused("fetch origin main first; no official history is available")
    # The checks below reason from what history does not contain, which only
    # a complete clone can show; image seeds are shallow.
    if git("rev-parse", "--is-shallow-repository").stdout.strip() != "false":
        raise Refused(
            "the platform checkout has shallow history; fetch it completely "
            "first (git fetch --unshallow origin)"
        )
    if not descends(target, official):
        raise Refused("the target is not part of the fetched official history")
    if not descends(image, target):
        raise Refused("the target is not newer than the running container")
    # The source's own updater finishes the crossing, so it must be new enough
    # to count the upgraded image's packages as installed.
    if not descends(SERVED_CODE_FLOOR, "HEAD"):
        raise Refused(
            "the installed platform code predates 23 September 2026 (#1311) and "
            "could not install the release afterwards; update it first"
        )
    # Settings then targets the current official release, so the new image
    # must carry exactly that release's packages.
    for path in PYTHON_INPUTS:
        if resolve_blob(target, path) != resolve_blob(official, path):
            raise Refused(
                "the target's Python packages differ from the latest official "
                "release's; use the latest release"
            )
    # Only a release that ships this bridge also judges the unchanged source
    # the way these checks do; an older target could not finish the crossing.
    for required in BRIDGE_FILES:
        if git("cat-file", "-e", f"{target}:{required}").returncode != 0:
            raise Refused("the target release predates the container-only upgrade")
    return image


def check_served_packages_survive(
    target: str, official: str, *, allow_package_loss: bool = False,
) -> None:
    """Refuse a target image that would not install what the source declares,
    naming each package, unless the owner accepts the loss.

    An input declared in the target's history is an older or equal release,
    so the newer image supersedes its versions, but history orders releases
    and does not prove the target still installs each distribution: one the
    target's lock (what its image installs) dropped may be unused by then, or
    provided another way (the image's ``legacy_runtime/jose`` shim covers
    python-jose), which this script cannot tell from the lock. If the source
    still imports one, it fails the new image's startup import check and that
    container serves its own baked platform instead, so
    ``--allow-package-loss`` may accept the risk. An input declared
    only in newer official history needs a target that includes that release,
    with no override. Anything else is a local declaration the target image
    may not satisfy, losing the live install; accepting that names the local
    commit to revert instead.
    """
    environment = target_environment(target)
    installed = parse_requirements(show(target, INSTALLED_INVENTORY) or b"", environment)
    for path in PYTHON_INPUTS:
        try:
            served = (PLATFORM / path).read_bytes()
        except OSError:
            continue
        if declared_in_history(target, path, served):
            # A line naming no distribution is unverifiable, so it counts too.
            dropped = sorted(
                name for name, spec in parse_requirements(served, environment).items()
                if spec is None or name not in installed
            )
            if dropped and not allow_package_loss:
                raise Refused(
                    f"the target image no longer installs Python packages the "
                    f"installed source's release declares in {path} "
                    f"({', '.join(dropped)}); if the source still imports one, "
                    "the new container serves its own baked platform instead "
                    "of this source. Pass --allow-package-loss to replace the "
                    "container anyway"
                )
            continue
        lost = package_loss(served, installed, environment)
        detail = f" ({'; '.join(lost)})" if lost else ""
        releases = declaring_commits(official, path, served)
        if releases:
            raise Refused(
                f"the installed source declares newer Python packages than the "
                f"target{detail}: they come from release {releases[0][:12]}, "
                "so choose a target that includes it"
            )
        if not lost or allow_package_loss:
            continue
        local = git("log", "-1", "--format=%h", "HEAD", "--not", official, "--", path)
        commit = local.stdout.strip()
        fix = (
            f"revert local commit {commit} or wait for its release"
            if commit else "revert the uncommitted edit or wait for its release"
        )
        raise Refused(
            f"the target image would drop Python packages the installed source "
            f"declares in {path} ({'; '.join(lost)}); {fix}, or pass "
            "--allow-package-loss to accept losing them"
        )


def check_no_pending_update() -> None:
    for name in (".platform-prepared-update.json", ".platform-conflict"):
        if (DATA / name).exists():
            raise Refused(
                "an update is prepared or waiting for its resolver; finish or "
                "cancel it in Settings first"
            )


def required_helper_protocol(target: str) -> int:
    """The host helper protocol the target release requires.

    Every release that ships this bridge also ships the requirement, so 0 (no
    requirement) is only a defensive reading. Any requirement, even one older
    than certification, refuses an uncertified helper: the old helper's
    cutover path is the hazard whatever the target.
    """
    shown = git("show", f"{target}:deployment/self-hosted-helper.required")
    if shown.returncode != 0:
        return 0
    try:
        return int(shown.stdout.strip())
    except ValueError:
        raise Refused("the target release's host helper requirement is unreadable") from None


def check_helper_idle(target: str) -> Path:
    inbox = CONTROL / "inbox"
    try:
        status = json.loads((CONTROL / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused(
            "the host replacement helper is not installed; run "
            "scripts/install-rebuild-helper.sh on the host"
        ) from None
    if not inbox.is_dir() or not os.access(inbox, os.W_OK | os.X_OK):
        raise Refused("the helper inbox is not writable from this container")
    if str(status.get("state") or "idle") in ACTIVE_STATES:
        raise Refused("a container replacement is already running")
    required = required_helper_protocol(target)
    installed = status.get("helper_protocol")
    if required and not (isinstance(installed, int) and not isinstance(installed, bool)
                         and installed >= required):
        raise Refused(
            "the host replacement helper is older than this release requires; "
            "update the Möbius checkout on the host and rerun "
            "sudo scripts/install-rebuild-helper.sh there first"
        )
    if (inbox / "request.json").exists():
        raise Refused("a container replacement request is already queued")
    return inbox


def write_request(inbox: Path, target: str) -> None:
    """Publish one request without overwriting a request queued meanwhile.

    Version 1 carries only the release: every installed helper accepts it, and
    no prepared update exists for an operation to confirm.
    """
    temp = inbox / f".request-{secrets.token_hex(12)}.tmp"
    payload = json.dumps({"version": 1, "expected_sha": target}, separators=(",", ":"))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, inbox / "request.json")
        except FileExistsError:
            raise Refused("a container replacement request is already queued") from None
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target", help="official release to move the container to (default: origin/main)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="run every check and report, but request nothing",
    )
    parser.add_argument(
        "--allow-package-loss", action="store_true",
        help="accept that the target image lacks a Python package the source declares",
    )
    args = parser.parse_args(argv)
    try:
        target = resolve(args.target) if args.target else resolve("refs/remotes/origin/main")
        if not target or (args.target and not SHA_RE.fullmatch(args.target)):
            raise Refused("name a fetched, complete 40-character official release")
        # The updater prepares updates under this lock; holding it while
        # checking and requesting keeps Settings from preparing one in between.
        # One prepared afterwards is settled by the new image's own boot.
        with open(RECONCILE_LOCK, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            image = check_target(target)
            check_served_packages_survive(
                target, resolve("refs/remotes/origin/main"),
                allow_package_loss=args.allow_package_loss,
            )
            check_no_pending_update()
            inbox = check_helper_idle(target)
            if args.check:
                print(
                    f"Ready: a container-only upgrade from {image[:12]} to "
                    f"{target[:12]} would be requested."
                )
                return 0
            write_request(inbox, target)
    except Refused as exc:
        print(f"Container upgrade not requested: {exc}.", file=sys.stderr)
        return 1
    print(
        f"Requested a container-only upgrade from {image[:12]} to {target[:12]}. "
        "Möbius restarts once the helper has pulled and checked the image; "
        "afterwards, install the update from Settings."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
