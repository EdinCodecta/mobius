"""The boot transaction's failure report is what an operator and a repair agent
have to go on, so it must carry the failing command's own explanation."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from app import platform_boot, platform_update


def _git_refusal() -> subprocess.CalledProcessError:
  return subprocess.CalledProcessError(
    128, ["git", "read-tree", "-n", "-m", "-u", "before", "target"],
    output="", stderr="error: Entry 'backend/app/x.py' not uptodate. Cannot merge.\n",
  )


def test_failure_detail_carries_git_stderr_through_a_wrapping_error():
  try:
    try:
      raise _git_refusal()
    except subprocess.CalledProcessError as exc:
      raise platform_update.BootTransactionError("could not swap in the update") from exc
  except platform_update.BootTransactionError as wrapped:
    detail = platform_boot.failure_detail(wrapped)

  assert detail.startswith("BootTransactionError('could not swap in the update')")
  assert "stderr: error: Entry 'backend/app/x.py' not uptodate. Cannot merge." in detail


def test_activate_failure_is_printed_and_recorded_with_git_stderr(
  tmp_path, monkeypatch, capsys,
):
  log = tmp_path / "platform-boot.jsonl"
  marker = tmp_path / "boot-transaction"
  monkeypatch.setattr(platform_boot, "BOOT_LOG", log)
  monkeypatch.setattr(platform_update, "BOOT_TRANSACTION_MARKER", marker)
  monkeypatch.setenv("MOBIUS_BOOT_ID", "boot-1")

  def refuse(_repo):
    raise _git_refusal()

  monkeypatch.setattr(platform_update, "settle_prepared_update_for_this_image", refuse)

  umask = os.umask(0)
  os.umask(umask)
  try:
    assert platform_boot.main(["platform_boot", "activate"]) == 1
  finally:
    os.umask(umask)  # main sets the boot's umask for its whole process

  assert "not uptodate. Cannot merge." in capsys.readouterr().err
  assert not marker.exists()  # a failed transaction never publishes its protocol
  [record] = [json.loads(line) for line in log.read_text().splitlines()]
  assert record["boot_id"] == "boot-1"
  assert record["command"] == "activate" and record["ok"] is False
  assert "not uptodate. Cannot merge." in record["detail"]


def test_boot_log_keeps_only_the_most_recent_runs(tmp_path, monkeypatch):
  log = tmp_path / "platform-boot.jsonl"
  monkeypatch.setattr(platform_boot, "_BOOT_LOG_RECORDS", 3)
  for number in range(5):
    platform_boot.record_boot_run("guard", ok=True, detail=f"run {number}", log=log)

  details = [json.loads(line)["detail"] for line in log.read_text().splitlines()]
  assert details == ["run 2", "run 3", "run 4"]
  assert sorted(path.name for path in tmp_path.iterdir()) == ["platform-boot.jsonl"]


def test_an_unwritable_boot_log_never_fails_the_boot(tmp_path, capsys):
  missing_dir = tmp_path / "absent" / "platform-boot.jsonl"

  platform_boot.record_boot_run("activate", ok=True, detail="none", log=missing_dir)

  assert "could not record this run" in capsys.readouterr().err


def test_a_damaged_boot_log_never_fails_a_settled_boot(tmp_path, monkeypatch):
  """A boot that settled the platform must exit 0 whatever the log holds;
  otherwise every later boot would refuse to start on the same file."""
  log = tmp_path / "platform-boot.jsonl"
  log.write_bytes(b'{"command": "guard"}\n\xff\xfe not text\n')
  monkeypatch.setattr(platform_boot, "BOOT_LOG", log)
  monkeypatch.setattr(platform_update, "boot_guard_sync", lambda: "clean")

  umask = os.umask(0)
  os.umask(umask)
  try:
    assert platform_boot.main(["platform_boot", "guard"]) == 0
  finally:
    os.umask(umask)

  last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
  assert last["command"] == "guard" and last["ok"] is True


def test_an_unexpected_recording_error_never_escapes(tmp_path, monkeypatch, capsys):
  def unserializable(_record):
    raise TypeError("not serializable")

  monkeypatch.setattr(platform_boot, "json", SimpleNamespace(dumps=unserializable))
  platform_boot.record_boot_run("guard", ok=True, detail="clean", log=tmp_path / "log")

  assert "could not record this run" in capsys.readouterr().err
  assert list(tmp_path.iterdir()) == []


def test_a_failed_boot_log_write_leaves_no_staged_file(tmp_path, monkeypatch, capsys):
  log = tmp_path / "platform-boot.jsonl"

  def refuse(_staged, _log):
    raise OSError("disk full")

  monkeypatch.setattr(platform_boot.os, "replace", refuse)
  platform_boot.record_boot_run("guard", ok=True, detail="clean", log=log)

  assert "could not record this run" in capsys.readouterr().err
  assert list(tmp_path.iterdir()) == []


def test_check_packages_subcommand_reports_gap(tmp_path, monkeypatch, capsys):
  """Restart admission reads this image's verdict: the gap as JSON, and a
  nonzero exit only for a gap its boot would refuse. It records nothing."""
  log = tmp_path / "platform-boot.jsonl"
  monkeypatch.setattr(platform_boot, "BOOT_LOG", log)
  seen = []
  gap = {"packages": [{"name": "pkg", "declared": "==2", "installed": "1",
                       "kind": "version"}], "blocking": False}
  monkeypatch.setattr(
    platform_update, "python_package_gap", lambda repo: seen.append(repo) or gap,
  )

  assert platform_boot.main(["platform_boot", "check-packages", str(tmp_path)]) == 0
  assert json.loads(capsys.readouterr().out) == gap
  assert seen == [tmp_path]

  gap["blocking"] = True
  assert platform_boot.main(["platform_boot", "check-packages"]) == 1
  assert json.loads(capsys.readouterr().out)["blocking"] is True
  assert seen[-1] == platform_update.PLATFORM_REPO

  monkeypatch.setattr(platform_update, "python_package_gap", lambda repo: None)
  assert platform_boot.main(["platform_boot", "check-packages"]) == 0
  assert capsys.readouterr().out.strip() == "null"
  assert not log.exists()


# --- Setting aside a commit that never finishes starting -------------------
#
# The import probe cannot see a crash or hang while the server starts. Without
# ``admit``, the container restarts into the same commit until the host stops
# retrying and the instance stays down.


def test_a_commit_that_never_became_ready_is_set_aside_after_the_limit(tmp_path):
  record = tmp_path / "unready.json"
  for _boot in range(platform_boot.UNREADY_BOOT_LIMIT):
    assert platform_boot.admit_served_commit("aaa", path=record) is None

  refusal = platform_boot.admit_served_commit("aaa", path=record)

  assert refusal and "aaa" in refusal and "set" in refusal


def test_a_ready_boot_forgets_the_count(tmp_path):
  record = tmp_path / "unready.json"
  for _boot in range(platform_boot.UNREADY_BOOT_LIMIT):
    platform_boot.admit_served_commit("aaa", path=record)
    platform_boot.forget_unready_boots(path=record)

  assert platform_boot.admit_served_commit("aaa", path=record) is None


def test_a_new_commit_starts_its_own_count(tmp_path):
  record = tmp_path / "unready.json"
  for _boot in range(platform_boot.UNREADY_BOOT_LIMIT):
    platform_boot.admit_served_commit("broken", path=record)

  assert platform_boot.admit_served_commit("fixed", path=record) is None
  assert json.loads(record.read_text()) == {"sha": "fixed", "unready_boots": 1}


def test_a_damaged_or_unwritable_count_never_stops_a_boot(tmp_path, capsys):
  damaged = tmp_path / "unready.json"
  damaged.write_text("{not json")
  assert platform_boot.admit_served_commit("aaa", path=damaged) is None

  unwritable = tmp_path / "absent" / "unready.json"
  assert platform_boot.admit_served_commit("aaa", path=unwritable) is None
  assert "could not count this boot" in capsys.readouterr().err
  assert sorted(p.name for p in tmp_path.iterdir()) == ["unready.json"]


def test_admit_refusal_is_recorded_for_the_repair_agent(tmp_path, monkeypatch):
  log = tmp_path / "platform-boot.jsonl"
  record = tmp_path / "unready.json"
  record.write_text(json.dumps({
    "sha": "abc123", "unready_boots": platform_boot.UNREADY_BOOT_LIMIT,
  }))
  monkeypatch.setattr(platform_boot, "BOOT_LOG", log)
  monkeypatch.setattr(platform_boot, "UNREADY_BOOTS", record)
  monkeypatch.setattr(platform_update, "_rev", lambda _repo, _ref: "abc123")

  umask = os.umask(0)
  os.umask(umask)
  try:
    assert platform_boot.main(["platform_boot", "admit"]) == 1
    assert platform_boot.main(["platform_boot", "ready"]) == 0
    assert platform_boot.main(["platform_boot", "admit"]) == 0
  finally:
    os.umask(umask)

  records = [json.loads(line) for line in log.read_text().splitlines()]
  assert [(r["command"], r["ok"]) for r in records] == [
    ("admit", False), ("admit", True),
  ]
  assert "abc123" in records[0]["detail"]


def _serve_checkout_outcome(tmp_path, *, probe_ok, admit_results, revert_ok):
  """Run the entrypoint's real serve-selection function with stubbed steps."""
  script = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text(encoding="utf-8")
  functions = []
  for name in ("_platform_servable() {", "_platform_serve_checkout() {"):
    start = script.index(name)
    functions.append(script[start:script.index("\n}\n", start) + 3])
  admits = tmp_path / "admits"
  admits.write_text("\n".join(admit_results) + "\n")
  harness = f"""
_platform_import_probe() {{ {'true' if probe_ok else 'false'}; }}
_platform_boot() {{
  case "$1" in
    admit)
      _next=$(head -n 1 '{admits}'); sed -i 1d '{admits}'
      echo admit >> '{tmp_path}/calls'; [ "$_next" = ok ] ;;
    revert) echo revert >> '{tmp_path}/calls'; {'true' if revert_ok else 'false'} ;;
    *) true ;;
  esac
}}
_platform_use_direct() {{ echo direct; }}
_platform_use_baked() {{ echo baked; }}
{''.join(functions)}
_platform_serve_checkout 2>/dev/null | tail -n 1
"""
  result = subprocess.run(
    ["sh", "-c", harness], capture_output=True, text=True, check=True,
  )
  calls_file = tmp_path / "calls"
  calls = calls_file.read_text().split() if calls_file.exists() else []
  return result.stdout.strip(), calls


def test_entrypoint_serves_an_admitted_commit(tmp_path):
  assert _serve_checkout_outcome(
    tmp_path, probe_ok=True, admit_results=["ok"], revert_ok=False,
  ) == ("direct", ["admit"])


def test_entrypoint_undoes_an_update_whose_commit_never_finished_starting(tmp_path):
  assert _serve_checkout_outcome(
    tmp_path, probe_ok=True, admit_results=["refused", "ok"], revert_ok=True,
  ) == ("direct", ["admit", "revert", "admit"])


def test_entrypoint_serves_the_built_in_copy_when_nothing_else_can_start(tmp_path):
  assert _serve_checkout_outcome(
    tmp_path, probe_ok=True, admit_results=["refused"], revert_ok=False,
  ) == ("baked", ["admit", "revert"])


def test_entrypoint_never_counts_a_boot_that_failed_its_import_probe(tmp_path):
  assert _serve_checkout_outcome(
    tmp_path, probe_ok=False, admit_results=[], revert_ok=False,
  ) == ("baked", ["revert"])


def test_entrypoint_reports_ready_only_for_the_served_platform():
  script = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text(encoding="utf-8")
  probe = script[script.index('_health_url="http://127.0.0.1'):]
  probe = probe[:probe.index(") &\n")]
  assert probe.index('until curl -sf "$_health_url"') < probe.index("_platform_boot ready")
  assert 'if [ "$_use_platform" -eq 1 ]; then\n    _platform_boot ready' in probe
