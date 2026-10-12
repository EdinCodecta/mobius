#!/usr/bin/env python3
"""Exercise the installer's actual units on a disposable GitHub-hosted runner.

The worker is a deterministic stand-in; systemd, the watcher, and timer are
real. No Docker commands, application data, or existing Mobius units are used;
the generated services retain their dependency on the runner's Docker daemon.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import time


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15)


def render_units(stage, *, prefix, worker, data_source, through="mobius-rebuild-reconcile.timer",
                 mount_ordering=True):
    """Write the installer's actual unit heredocs into ``stage``, renamed to
    ``prefix`` and running ``worker``. ``through`` names the last unit to
    render; ``mount_ordering=False`` reproduces units from before protocol 6."""
    source = (Path(__file__).parent / "install-rebuild-helper.sh").read_text()
    start = source.index('cat >"$STAGE/mobius-rebuild.service"')
    end = (source.index("for unit in mobius-rebuild.service", start)
           if through == "mobius-rebuild-reconcile.timer"
           else source.index("\nEOF\n", source.index(f'cat >"$STAGE/{through}"')) + len("\nEOF\n"))
    units = source[start:end].replace("/usr/local/libexec/mobius-rebuild-host", str(worker))
    units = units.replace("mobius-rebuild", prefix)
    if not mount_ordering:
        units = "\n".join(line for line in units.splitlines()
                           if not line.startswith("RequiresMountsFor=")) + "\n"
    subprocess.run(["bash", "-eu", "-c", units], check=True, timeout=10,
                   env={**os.environ, "STAGE": str(stage), "DATA_SOURCE": str(data_source)})
    return units


def unit_property(unit, name):
    return command("systemctl", "show", "--value", "-p", name, unit).stdout.strip()


def mounted(path):
    """Kernel mount table, not os.path.ismount: a bind mount on the same
    filesystem keeps its parent's device and is invisible to ismount."""
    target = os.path.realpath(path)
    with open("/proc/self/mountinfo", encoding="utf-8") as table:
        return any(line.split()[4].replace("\\040", " ") == target for line in table)


def watcher_follows_the_data_across_a_volume_unmount(*, watch_mountpoint):
    """Production 2026-10-09/12: a bind-option Docker volume is unmounted while
    no container uses it. systemd re-arms a path unit's inotify watches when
    its service stops, so a watcher re-armed during that gap kept watching the
    hidden directory under the mountpoint forever. Returns whether a request
    written through the remounted volume was dispatched without the timer."""
    prefix = f"mobius-watch-test-{os.getpid()}-{int(watch_mountpoint)}"
    names = [f"{prefix}.service", f"{prefix}.path"]
    installed = []
    with tempfile.TemporaryDirectory(prefix="recovery-watch-") as directory:
        root = Path(directory)
        stable, volume = root / "device", root / "volume-mountpoint"
        (stable / prefix / "inbox").mkdir(parents=True)
        volume.mkdir()
        worker = root / "worker"
        worker.write_text(f'''#!/usr/bin/python3
import pathlib, sys
if sys.argv[1] == "run":
    request = pathlib.Path({str(stable / prefix / "inbox" / "request.json")!r})
    if request.exists():
        request.unlink()
        pathlib.Path({str(root / "dispatched")!r}).touch()
''')
        worker.chmod(0o755)
        render_units(root, prefix=prefix, worker=worker, through="mobius-rebuild.path",
                     data_source=volume if watch_mountpoint else stable,
                     mount_ordering=not watch_mountpoint)
        request = volume / prefix / "inbox" / "request.json"  # the app's view of /data
        try:
            command("mount", "--bind", str(stable), str(volume))
            for name in names:
                destination = Path("/run/systemd/system") / name
                if destination.exists() or destination.is_symlink():
                    raise RuntimeError(f"refusing to replace existing unit {name}")
                destination.symlink_to(root / name)
                installed.append(destination)
            command("systemctl", "daemon-reload")
            command("systemctl", "start", f"{prefix}.path")
            # The old container is removed: Docker unmounts the volume, and the
            # worker's service stops while it is gone, re-arming the watcher.
            command("umount", str(volume))
            subprocess.run(["systemctl", "start", f"{prefix}.service"], check=False, timeout=15,
                           capture_output=True)
            # systemd re-arms the watcher on a deferred event once the service
            # is inactive again; remount only after that, as in production
            # (where the next container started much later).
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not (
                    unit_property(f"{prefix}.service", "ActiveState") == "inactive"
                    and unit_property(f"{prefix}.path", "SubState") == "waiting"):
                time.sleep(0.2)
            assert unit_property(f"{prefix}.path", "SubState") == "waiting", "watcher did not re-arm"
            time.sleep(1)
            # The replacement starts: the volume is mounted again and the app
            # queues the next request through it.
            command("mount", "--bind", str(stable), str(volume))
            time.sleep(1)
            request.write_text("next replacement")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not (root / "dispatched").exists():
                time.sleep(0.2)
            dispatched = (root / "dispatched").exists()
            print(f"watcher on {'mountpoint' if watch_mountpoint else 'stable source'}: "
                  f"dispatched={dispatched}")
            print(subprocess.run(["journalctl", "--no-pager", "-o", "short-precise", "-n", "20",
                                  "-u", f"{prefix}.path", "-u", f"{prefix}.service"],
                                 capture_output=True, text=True, timeout=15).stdout)
            return dispatched
        finally:
            try:
                if installed:
                    subprocess.run(["systemctl", "stop", *[p.name for p in installed]], check=False,
                                   timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            finally:
                while mounted(volume):
                    command("umount", str(volume))
                for path in installed:
                    path.unlink()
                command("systemctl", "daemon-reload")
                if installed:
                    subprocess.run(["systemctl", "reset-failed", *[p.name for p in installed]],
                                   check=False, timeout=10,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    if os.geteuid() != 0 or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("run only as root on a disposable GitHub-hosted runner")
    # The hazard is real on this systemd: watching the mountpoint goes blind.
    assert not watcher_follows_the_data_across_a_volume_unmount(watch_mountpoint=True), (
        "expected the mountpoint watcher to be blind after a volume unmount")
    assert watcher_follows_the_data_across_a_volume_unmount(watch_mountpoint=False), (
        "the stable-source watcher missed a request after a volume unmount")
    print("recovery units: the watcher follows the stable data source across a volume unmount")
    prefix = f"mobius-recovery-test-{os.getpid()}"
    names = [f"{prefix}.service", f"{prefix}.path",
             f"{prefix}-reconcile.service", f"{prefix}-reconcile.timer"]
    installed = []
    with tempfile.TemporaryDirectory(prefix="recovery-units-") as directory:
        root = Path(directory)
        worker = root / "worker"
        data = root / "data"
        inbox = data / prefix / "inbox"
        inbox.mkdir(parents=True)
        transaction = root / "transaction"
        transaction.touch()
        worker.write_text(f'''#!/usr/bin/python3
import pathlib, sys
root = pathlib.Path({str(root)!r})
request = pathlib.Path({str(inbox / "request.json")!r})
with (root / "calls").open("a") as log:
    log.write(sys.argv[1] + "\\n")
if sys.argv[1] == "run":
    if not (root / "transaction").exists() and request.exists():
        request.unlink()
        (root / "dispatched").touch()
elif (root / "allow-recovery").exists():
    (root / "transaction").unlink(missing_ok=True)
    (root / "reconciled").touch()
''')
        worker.chmod(0o755)
        render_units(root, prefix=prefix, worker=worker, data_source=data)
        # Scale only the timer interval; the Python contract test checks the
        # installed production budget, and systemd's dispatch semantics remain real.
        # Four seconds also stays below the normal 5-starts/10-seconds service
        # limit; a one-second test timer would itself exhaust that limit.
        timer = root / f"{prefix}-reconcile.timer"
        timer.write_text(timer.read_text().replace("OnBootSec=30", "OnBootSec=4")
                         .replace("OnUnitInactiveSec=30", "OnUnitInactiveSec=4"))
        try:
            for name in names:
                destination = Path("/run/systemd/system") / name
                if destination.exists() or destination.is_symlink():
                    raise RuntimeError(f"refusing to replace existing unit {name}")
                destination.symlink_to(root / name)
                installed.append(destination)
            command("systemctl", "daemon-reload")
            command("systemctl", "start", f"{prefix}.path", f"{prefix}-reconcile.timer")
            (inbox / "request.json").write_text("queued behind recovery")
            time.sleep(8)
            assert not (root / "dispatched").exists(), "request bypassed recovery ownership"
            assert (inbox / "request.json").exists(), "queued request was lost"
            assert command("systemctl", "is-active", f"{prefix}.path").stdout.strip() == "active"
            assert len((root / "calls").read_text().splitlines()) < 20, "watcher is spinning"
            (root / "allow-recovery").touch()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (root / "dispatched").exists():
                time.sleep(0.2)
            assert (root / "reconciled").exists(), "timer did not retry reconciliation"
            assert (root / "dispatched").exists(), "timer did not dispatch the pending request"
            assert not transaction.exists()
            assert command("systemctl", "is-active", f"{prefix}.path").stdout.strip() == "active"
            print("recovery units: pending request survives recovery and dispatches without watcher failure")
        finally:
            owned = [path.name for path in installed]
            try:
                if owned:
                    subprocess.run(["systemctl", "stop", *owned], check=False, timeout=30,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            finally:
                for path in installed:
                    path.unlink()
                command("systemctl", "daemon-reload")
                if owned:
                    subprocess.run(["systemctl", "reset-failed", *owned], check=False, timeout=10,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
