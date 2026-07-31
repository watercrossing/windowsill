"""Tests for windowsill.runner — invocation, detached spawn, reap, timeout (§3.2, §3.3, §6.2).

These tests never spawn a real `claude`, never touch the network, and never
depend on the real wall clock for correctness: every clock/sleep the runner
consumes is an injectable parameter, and every "child process" is either a
plain fake object or a tiny local Python script standing in for `claude`
(never the real thing). The job-directory root is always a tmp_path, never
~/.windowsill, and the DB (where used) is always opened at a tmp_path too.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from windowsill import db, notify, runner, usage

# ---------------------------------------------------------------------------
# helpers: fake "claude" scripts (never the real claude, never a network call)
# ---------------------------------------------------------------------------


def _write_script(tmp_path: Path, name: str, code: str) -> list[str]:
    """Write a tiny standalone Python script and return it as an argv
    prefix (``[sys.executable, path]``) suitable for ``claude_cmd``."""
    path = tmp_path / name
    path.write_text(code)
    return [sys.executable, str(path)]


SUCCESS_SCRIPT = """
import json, sys
body = sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "duration_ms": 537, "duration_api_ms": 400, "num_turns": 3,
    "result": "ok: " + body.strip(), "stop_reason": None,
    "session_id": "fake-session-123", "total_cost_usd": 0,
    "usage": {"input_tokens": 1, "output_tokens": 2}, "modelUsage": {},
    "permission_denials": [], "uuid": "u1",
}))
"""

ERROR_SCRIPT = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({
    "type": "result", "subtype": "error_max_turns", "is_error": True,
    "duration_ms": 12, "duration_api_ms": 10, "num_turns": 60,
    "result": None, "stop_reason": "max_turns",
    "session_id": "fake-session-err", "total_cost_usd": 0,
    "usage": {}, "modelUsage": {}, "permission_denials": [], "uuid": "u2",
}))
"""

ECHO_SCRIPT = """
import sys
sys.stdout.write(sys.stdin.read())
"""

# Emits a partial stream (a non-result event) then exits without ever writing
# a result event -- as a run killed mid-flight (kill -9 / OOM) would leave it.
NO_RESULT_SCRIPT = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
"""

# The regression shape from design/09-findings.md, "Settled 2026-07-28, second
# sitting": subtype "success", is_error False -- an ordinary "done" run -- but
# two Write calls were refused, visible only in permission_denials.
PERMISSION_DENIED_SCRIPT = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "duration_ms": 200, "duration_api_ms": 150, "num_turns": 4,
    "result": "done, but", "stop_reason": None,
    "session_id": "fake-session-denied", "total_cost_usd": 0,
    "usage": {}, "modelUsage": {},
    "permission_denials": [
        {"tool_name": "Write", "tool_input": {"file_path": "a.txt"}},
        {"tool_name": "Write", "tool_input": {"file_path": "b.txt"}}
    ],
    "uuid": "u3",
}))
"""

HANG_IGNORES_SIGTERM_SCRIPT = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.05)
"""

HANG_RESPECTS_SIGTERM_SCRIPT = """
import time
time.sleep(100)
"""


def _wait_for_file(path: Path, timeout: float = 2.0) -> None:
    """Poll for an OS-async side effect (a detached child creating a file).

    This is real, bounded wall-clock polling for something genuinely
    asynchronous at the OS level (a Popen-and-exit spawn we deliberately
    never wait() on) -- not a stand-in for the runner's own timeout logic,
    which is always driven by injectable clock/sleep in these tests.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path} was never created within {timeout}s")


# ---------------------------------------------------------------------------
# fakes for pure, clock-free unit testing
# ---------------------------------------------------------------------------


class FakeProcess:
    """Stands in for subprocess.Popen for pure wait_with_timeout tests."""

    def __init__(self, exit_after_polls: int | None, pid: int = 4242):
        self.pid = pid
        self.returncode: int | None = None
        self._polls = 0
        self._exit_after_polls = exit_after_polls

    def poll(self) -> int | None:
        self._polls += 1
        if self._exit_after_polls is not None and self._polls >= self._exit_after_polls:
            self.returncode = 0
        return self.returncode


class FakeClock:
    """A deterministic, monotonically increasing fake clock."""

    def __init__(self, start: float = 0.0, step: float = 1.0):
        self.value = start
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


class Spy:
    def __init__(self):
        self.calls: list[Any] = []

    def __call__(self, *args, **kwargs) -> None:
        self.calls.append(args)


def noop_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# job_dir: §6.2 layout (zero-padded id-name so ls sorts)
# ---------------------------------------------------------------------------


def test_job_dir_zero_pads_id(tmp_path: Path) -> None:
    d = runner.job_dir(tmp_path, 42, "refactor-auth")
    assert d == tmp_path / "jobs" / "00042-refactor-auth"


def test_job_dir_does_not_create_directory(tmp_path: Path) -> None:
    d = runner.job_dir(tmp_path, 1, "foo")
    assert not d.exists()


# ---------------------------------------------------------------------------
# build_argv: §3.2 invocation, minus "cd <cwd> &&" and minus the body
# ---------------------------------------------------------------------------


def test_build_argv_default_shape() -> None:
    argv = runner.build_argv(["--model", "claude-opus-4-8"])
    assert argv == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-opus-4-8",
    ]


def test_build_argv_includes_resume_when_given() -> None:
    argv = runner.build_argv(["--max-turns", "60"], resume="conv-abc")
    assert argv == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--resume",
        "conv-abc",
        "--max-turns",
        "60",
    ]


def test_build_argv_omits_resume_when_none() -> None:
    argv = runner.build_argv([], resume=None)
    assert "--resume" not in argv


def test_build_argv_claude_cmd_is_injectable() -> None:
    """This is the seam production wires ["claude"] into and tests wire a
    fake executable/argv into."""
    argv = runner.build_argv([], claude_cmd=["/usr/bin/env", "fake-claude"])
    assert argv[:2] == ["/usr/bin/env", "fake-claude"]


# ---------------------------------------------------------------------------
# cmd.json / resume.sh: §6.2 artifacts the runner writes
# ---------------------------------------------------------------------------


def test_write_cmd_json_round_trips(tmp_path: Path) -> None:
    dir_ = tmp_path / "job"
    dir_.mkdir()
    path = runner.write_cmd_json(dir_, argv=["claude", "-p"], cwd="/home/user/app", env={"FOO": "bar"})
    assert path == dir_ / "cmd.json"
    data = json.loads(path.read_text())
    assert data == {"argv": ["claude", "-p"], "cwd": "/home/user/app", "env": {"FOO": "bar"}}


def test_write_cmd_json_defaults_env_to_empty_dict(tmp_path: Path) -> None:
    dir_ = tmp_path / "job"
    dir_.mkdir()
    path = runner.write_cmd_json(dir_, argv=["claude"], cwd="/tmp", env=None)
    assert json.loads(path.read_text())["env"] == {}


def test_write_resume_sh_contents_and_executable(tmp_path: Path) -> None:
    dir_ = tmp_path / "job"
    dir_.mkdir()
    path = runner.write_resume_sh(dir_, cwd="/home/user/my app", claude_cmd=["claude"], conversation_id="abc-123")
    text = path.read_text()
    assert "cd '/home/user/my app'" in text
    assert "claude --resume abc-123" in text
    assert os.access(path, os.X_OK)


# ---------------------------------------------------------------------------
# parse_result_event: §3.2 "derive the result from the last event"
# ---------------------------------------------------------------------------


def test_parse_result_event_missing_file_returns_none(tmp_path: Path) -> None:
    assert runner.parse_result_event(tmp_path / "stream.jsonl") is None


def test_parse_result_event_empty_file_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    path.write_text("")
    assert runner.parse_result_event(path) is None


def test_parse_result_event_reads_last_line(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    path.write_text(
        json.dumps({"type": "system", "subtype": "init"})
        + "\n"
        + json.dumps({"type": "assistant", "message": {}})
        + "\n"
        + json.dumps({"type": "result", "subtype": "success", "is_error": False})
        + "\n"
    )
    event = runner.parse_result_event(path)
    assert event == {"type": "result", "subtype": "success", "is_error": False}


def test_parse_result_event_tolerates_trailing_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    path.write_text(json.dumps({"type": "result", "is_error": False}) + "\n\n\n")
    assert runner.parse_result_event(path) == {"type": "result", "is_error": False}


def test_parse_result_event_none_when_last_line_not_result(tmp_path: Path) -> None:
    """A killed run's stream ends mid-stream, e.g. on a non-result event --
    that must not be mistaken for a completed result."""
    path = tmp_path / "stream.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {}}) + "\n")
    assert runner.parse_result_event(path) is None


def test_parse_result_event_none_when_last_line_malformed(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    path.write_text(json.dumps({"type": "result"}) + "\nnot valid json{{{\n")
    assert runner.parse_result_event(path) is None


# ---------------------------------------------------------------------------
# outcome_from_result: pure decision table
# ---------------------------------------------------------------------------


def test_outcome_done() -> None:
    event = {"type": "result", "is_error": False, "subtype": "success"}
    assert runner.outcome_from_result(event, timed_out=False) == "done"


def test_outcome_failed() -> None:
    event = {"type": "result", "is_error": True, "subtype": "error_max_turns"}
    assert runner.outcome_from_result(event, timed_out=False) == "failed"


def test_outcome_killed_when_no_result_event() -> None:
    assert runner.outcome_from_result(None, timed_out=False) == "killed"


def test_outcome_timeout_overrides_everything() -> None:
    event = {"type": "result", "is_error": False, "subtype": "success"}
    assert runner.outcome_from_result(event, timed_out=True) == "timeout"
    assert runner.outcome_from_result(None, timed_out=True) == "timeout"


# ---------------------------------------------------------------------------
# is_alive: real (fake) child processes, no timing assumptions
# ---------------------------------------------------------------------------


def test_is_alive_true_for_running_process() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert runner.is_alive(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait()


def test_is_alive_false_after_process_exits() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    proc.terminate()
    proc.wait()
    assert runner.is_alive(proc.pid) is False


# ---------------------------------------------------------------------------
# wait_with_timeout: pure unit tests with fake process + fake clock
# ---------------------------------------------------------------------------


def test_wait_with_timeout_process_exits_before_deadline() -> None:
    process = FakeProcess(exit_after_polls=2)
    terminate = Spy()
    kill = Spy()
    timed_out = runner.wait_with_timeout(
        process,
        timeout_seconds=100,
        grace_seconds=30,
        clock=FakeClock(start=0.0, step=1.0),
        sleep=noop_sleep,
        terminate_group=terminate,
        kill_group=kill,
    )
    assert timed_out is False
    assert terminate.calls == []
    assert kill.calls == []


def test_wait_with_timeout_no_timeout_configured_waits_indefinitely() -> None:
    process = FakeProcess(exit_after_polls=3)
    timed_out = runner.wait_with_timeout(
        process,
        timeout_seconds=None,
        clock=FakeClock(),
        sleep=noop_sleep,
    )
    assert timed_out is False
    assert process.returncode == 0


def test_wait_with_timeout_sigterm_then_dies_within_grace() -> None:
    """Deadline passes, SIGTERM sent, process dies before the grace
    period elapses -- SIGKILL must never be sent."""
    process = FakeProcess(exit_after_polls=None)
    terminate_calls = []

    def terminate(pid: int) -> None:
        terminate_calls.append(pid)
        process.returncode = 0  # simulate the SIGTERM actually killing it

    kill = Spy()
    clock = FakeClock(start=0.0, step=1.0)
    timed_out = runner.wait_with_timeout(
        process,
        timeout_seconds=2,
        grace_seconds=30,
        clock=clock,
        sleep=noop_sleep,
        terminate_group=terminate,
        kill_group=kill,
    )
    assert timed_out is True
    assert terminate_calls == [process.pid]
    assert kill.calls == []


def test_wait_with_timeout_escalates_to_sigkill_after_grace() -> None:
    """Process ignores SIGTERM entirely (fake never sets returncode until
    the fake kill_group does) -- must escalate to SIGKILL."""
    process = FakeProcess(exit_after_polls=None)
    terminate = Spy()

    def kill(pid: int) -> None:
        process.returncode = -9

    clock = FakeClock(start=0.0, step=1.0)
    timed_out = runner.wait_with_timeout(
        process,
        timeout_seconds=2,
        grace_seconds=5,
        clock=clock,
        sleep=noop_sleep,
        terminate_group=terminate,
        kill_group=kill,
    )
    assert timed_out is True
    assert len(terminate.calls) == 1
    assert process.returncode == -9


# ---------------------------------------------------------------------------
# wait_with_timeout: real process integration (still no real-clock reliance --
# timeout_seconds=0 / grace_seconds=0 with a deterministic fake clock means
# the escalation is driven on the very first check, not by elapsed wall time)
# ---------------------------------------------------------------------------


def test_wait_with_timeout_kills_real_process_group(tmp_path: Path) -> None:
    argv = _write_script(tmp_path, "hang.py", HANG_IGNORES_SIGTERM_SCRIPT)
    dir_ = tmp_path / "job"
    dir_.mkdir()
    process = runner.spawn(argv, "", cwd=str(tmp_path), dir_=dir_)
    try:
        assert runner.is_alive(process.pid) is True
        # own process group: pgid should equal the child's own pid
        assert os.getpgid(process.pid) == process.pid

        timed_out = runner.wait_with_timeout(
            process,
            timeout_seconds=0,
            grace_seconds=0,
            clock=FakeClock(start=0.0, step=1.0),
            sleep=noop_sleep,
        )
        assert timed_out is True
        assert not runner.is_alive(process.pid)
    finally:
        if runner.is_alive(process.pid):
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


# ---------------------------------------------------------------------------
# spawn: own process group, stdin from body, stdout to stream.jsonl
# ---------------------------------------------------------------------------


def test_spawn_writes_body_to_stdin_and_captures_stdout(tmp_path: Path) -> None:
    argv = _write_script(tmp_path, "echo.py", ECHO_SCRIPT)
    dir_ = tmp_path / "job"
    dir_.mkdir()
    process = runner.spawn(argv, "hello from the job body", cwd=str(tmp_path), dir_=dir_)
    process.wait()
    assert (dir_ / "stream.jsonl").read_text() == "hello from the job body"


def test_spawn_runs_in_its_own_process_group(tmp_path: Path) -> None:
    argv = _write_script(tmp_path, "echo.py", ECHO_SCRIPT)
    dir_ = tmp_path / "job"
    dir_.mkdir()
    process = runner.spawn(argv, "", cwd=str(tmp_path), dir_=dir_)
    process.wait()
    # A closed process' pgid is no longer queryable in general, so check a
    # long-running one instead.
    argv2 = _write_script(tmp_path, "hang.py", HANG_RESPECTS_SIGTERM_SCRIPT)
    dir2 = tmp_path / "job2"
    dir2.mkdir()
    long_proc = runner.spawn(argv2, "", cwd=str(tmp_path), dir_=dir2)
    try:
        assert os.getpgid(long_proc.pid) == long_proc.pid
        assert os.getpgid(long_proc.pid) != os.getpgid(os.getpid())
    finally:
        os.killpg(long_proc.pid, signal.SIGKILL)
        long_proc.wait()


def test_spawn_uses_given_cwd(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    dir_ = tmp_path / "job"
    dir_.mkdir()
    script = "import os, sys\nsys.stdout.write(os.getcwd())\n"
    argv = _write_script(tmp_path, "pwd.py", script)
    process = runner.spawn(argv, "", cwd=str(work_dir), dir_=dir_)
    process.wait()
    assert (dir_ / "stream.jsonl").read_text() == str(work_dir)


def test_spawn_uses_injected_popen(tmp_path: Path) -> None:
    dir_ = tmp_path / "job"
    dir_.mkdir()
    calls = {}

    class FakePopen:
        pid = 999
        returncode = 0

    def fake_popen(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return FakePopen()

    process = runner.spawn(["fake-claude"], "body text", cwd="/tmp", dir_=dir_, popen=fake_popen)
    assert process.pid == 999
    assert calls["argv"] == ["fake-claude"]
    assert calls["kwargs"]["cwd"] == "/tmp"
    assert calls["kwargs"]["start_new_session"] is True
    # the stdin file was still materialised on disk with the body
    assert (dir_ / ".stdin").read_text() == "body text"


# ---------------------------------------------------------------------------
# reap: pid liveness + derive/parse result.json -> outcome
# ---------------------------------------------------------------------------


def test_reap_alive_process_reports_alive(tmp_path: Path) -> None:
    dir_ = tmp_path / "job"
    dir_.mkdir()
    status = runner.reap(dir_, pid=123, alive_check=lambda pid: True)
    assert status.alive is True
    assert status.outcome is None


def test_reap_dead_process_derives_result_json_from_stream(tmp_path: Path) -> None:
    dir_ = tmp_path / "job"
    dir_.mkdir()
    (dir_ / "stream.jsonl").write_text(
        json.dumps({"type": "system"}) + "\n" + json.dumps({"type": "result", "is_error": False, "session_id": "s1"}) + "\n"
    )
    status = runner.reap(dir_, pid=123, alive_check=lambda pid: False)
    assert status.alive is False
    assert status.outcome == "done"
    assert status.result == {"type": "result", "is_error": False, "session_id": "s1"}
    assert json.loads((dir_ / "result.json").read_text()) == status.result


def test_reap_dead_process_uses_existing_result_json(tmp_path: Path) -> None:
    dir_ = tmp_path / "job"
    dir_.mkdir()
    (dir_ / "result.json").write_text(json.dumps({"type": "result", "is_error": True}))
    # stream.jsonl deliberately absent/stale -- result.json already final
    status = runner.reap(dir_, pid=123, alive_check=lambda pid: False)
    assert status.outcome == "failed"
    assert status.result == {"type": "result", "is_error": True}


def test_reap_dead_process_no_result_is_killed(tmp_path: Path) -> None:
    dir_ = tmp_path / "job"
    dir_.mkdir()
    status = runner.reap(dir_, pid=123, alive_check=lambda pid: False)
    assert status.alive is False
    assert status.outcome == "killed"
    assert status.result is None


# ---------------------------------------------------------------------------
# log_destination: resolving --log (§2.3)
# ---------------------------------------------------------------------------


def test_log_destination_none_when_directive_absent() -> None:
    assert runner.log_destination(None, job_id=42, name="lint", cwd="/tmp/work") is None
    assert runner.log_destination("", job_id=42, name="lint", cwd="/tmp/work") is None


def test_log_destination_absolute_file_is_used_verbatim() -> None:
    dest = runner.log_destination("/var/log/lint.md", job_id=42, name="lint", cwd="/tmp/work")
    assert dest == Path("/var/log/lint.md")


def test_log_destination_trailing_slash_means_directory(tmp_path: Path) -> None:
    # A directory that doesn't exist yet is still a directory if it's spelled
    # like one -- run_job creates it on the way to writing the file.
    dest = runner.log_destination(f"{tmp_path}/not-created-yet/", job_id=42, name="lint", cwd="/tmp/work")
    assert dest == tmp_path / "not-created-yet" / "00042-lint.md"


def test_log_destination_existing_directory_gets_job_named_file(tmp_path: Path) -> None:
    dest = runner.log_destination(str(tmp_path), job_id=7, name="lint", cwd="/tmp/work")
    assert dest == tmp_path / "00007-lint.md"


def test_log_destination_relative_path_resolves_against_job_cwd(tmp_path: Path) -> None:
    dest = runner.log_destination("logs/run.md", job_id=42, name="lint", cwd=str(tmp_path))
    assert dest == tmp_path / "logs" / "run.md"


def test_log_destination_relative_directory_resolves_against_job_cwd(tmp_path: Path) -> None:
    dest = runner.log_destination("logs/", job_id=42, name="lint", cwd=str(tmp_path))
    assert dest == tmp_path / "logs" / "00042-lint.md"


# ---------------------------------------------------------------------------
# run_job: the full detached-run-wrapper integration (§3.1/§3.2/§3.3/§6.2)
# ---------------------------------------------------------------------------


def _make_job(conn, tmp_path: Path, *, timeout: str | None = None, log_path: str | None = None) -> int:
    return db.insert_job(
        conn,
        name="nightly-lint",
        state="queued",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=["--max-turns", "10"],
        timeout=timeout,
        log_path=log_path,
    )


def test_run_job_success_updates_db_and_writes_artifacts(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)

    claude_cmd = _write_script(tmp_path, "claude_ok.py", SUCCESS_SCRIPT)
    marker = tmp_path / "on_complete_ran"
    on_complete = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]

    outcome = runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=on_complete,
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:05:00Z"]).__next__,
        sleep=noop_sleep,
    )

    assert outcome.outcome == "done"
    assert outcome.result["session_id"] == "fake-session-123"

    job = db.get_job(conn, job_id)
    assert job["state"] == "done"
    assert job["started_at"] == "2026-07-21T02:00:00Z"
    assert job["finished_at"] == "2026-07-21T02:05:00Z"
    assert job["conversation_id"] == "fake-session-123"
    assert job["pid"] == outcome.pid
    assert job["exit_code"] == 0

    runs = db.list_runs(conn, job_id=job_id)
    assert len(runs) == 1
    assert runs[0]["outcome"] == "done"
    assert runs[0]["num_turns"] == 3
    assert runs[0]["duration_ms"] == 537
    assert runs[0]["conversation_id"] == "fake-session-123"

    events = db.list_events(conn, job_id=job_id)
    assert [e["kind"] for e in events] == ["dispatched", "done"]

    dir_ = runner.job_dir(tmp_path, job_id, "nightly-lint")
    assert json.loads((dir_ / "cmd.json").read_text())["cwd"] == str(tmp_path)
    assert (dir_ / "stream.jsonl").exists()
    result_json = json.loads((dir_ / "result.json").read_text())
    assert result_json["session_id"] == "fake-session-123"
    assert "--resume fake-session-123" in (dir_ / "resume.sh").read_text()

    _wait_for_file(marker)


def test_run_job_copies_transcript_to_log_destination(tmp_path: Path) -> None:
    """§2.3 --log: the rendered transcript is written to the requested path
    *as well as* the canonical job-dir copy (§6.2), parents created."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path, log_path="logs/nightly/")
    claude_cmd = _write_script(tmp_path, "claude_ok.py", SUCCESS_SCRIPT)

    runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        sleep=noop_sleep,
    )

    exported = tmp_path / "logs" / "nightly" / f"{job_id:05d}-nightly-lint.md"
    canonical = runner.job_dir(tmp_path, job_id, "nightly-lint") / "transcript.md"
    assert canonical.exists()
    assert exported.read_text() == canonical.read_text()
    assert "please lint the repo" in exported.read_text()


def test_run_job_unwritable_log_destination_does_not_fail_the_run(tmp_path: Path) -> None:
    """The run already happened -- an unusable --log path must not cost us the
    job dir's transcript or the job's terminal state."""
    conn = db.connect(tmp_path / "windowsill.db")
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    job_id = _make_job(conn, tmp_path, log_path=str(blocker / "run.md"))
    claude_cmd = _write_script(tmp_path, "claude_ok.py", SUCCESS_SCRIPT)

    outcome = runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        sleep=noop_sleep,
    )

    assert outcome.outcome == "done"
    assert db.get_job(conn, job_id)["state"] == "done"
    assert (runner.job_dir(tmp_path, job_id, "nightly-lint") / "transcript.md").exists()


def test_run_job_failure_marks_job_failed(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    claude_cmd = _write_script(tmp_path, "claude_err.py", ERROR_SCRIPT)

    outcome = runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:01:00Z"]).__next__,
        sleep=noop_sleep,
    )

    assert outcome.outcome == "failed"
    job = db.get_job(conn, job_id)
    assert job["state"] == "failed"
    assert job["conversation_id"] == "fake-session-err"


def test_run_job_counts_permission_denials_even_when_envelope_reads_success(tmp_path: Path) -> None:
    """§9 "Settled 2026-07-28, second sitting": a job blocked from doing
    anything must not just read "done" -- the denial count has to land
    somewhere. The run outcome itself is untouched: still whatever the
    envelope says, "done" here, no new job state invented."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    claude_cmd = _write_script(tmp_path, "claude_denied.py", PERMISSION_DENIED_SCRIPT)

    outcome = runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:01:00Z"]).__next__,
        sleep=noop_sleep,
    )

    assert outcome.outcome == "done"
    assert db.get_job(conn, job_id)["state"] == "done"

    run = db.list_runs(conn, job_id=job_id)[0]
    assert run["outcome"] == "done"
    assert run["permission_denials"] == 2


def test_run_job_permission_denials_is_zero_when_envelope_list_is_empty(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    claude_cmd = _write_script(tmp_path, "claude_ok.py", SUCCESS_SCRIPT)

    runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        sleep=noop_sleep,
    )

    run = db.list_runs(conn, job_id=job_id)[0]
    assert run["permission_denials"] == 0


def test_run_job_permission_denials_is_none_without_a_result_event(tmp_path: Path) -> None:
    """A run killed before ever emitting a result event has no envelope to
    read a denial count from -- ``None``, not ``0``, mirrors how num_turns
    and duration_ms already behave for this case."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    claude_cmd = _write_script(tmp_path, "claude_noresult.py", NO_RESULT_SCRIPT)

    runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:01:00Z"]).__next__,
        sleep=noop_sleep,
    )

    run = db.list_runs(conn, job_id=job_id)[0]
    assert run["permission_denials"] is None


def test_run_job_timeout_marks_job_failed_with_timeout_outcome(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path, timeout="0s")
    claude_cmd = _write_script(tmp_path, "claude_hang.py", HANG_IGNORES_SIGTERM_SCRIPT)

    outcome = runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:00:05Z"]).__next__,
        clock=FakeClock(start=0.0, step=1.0),
        sleep=noop_sleep,
        grace_seconds=0,
    )

    assert outcome.outcome == "timeout"
    run = db.list_runs(conn, job_id=job_id)[0]
    assert run["outcome"] == "timeout"
    job = db.get_job(conn, job_id)
    assert job["state"] == "failed"


def test_run_job_killed_midrun_marks_job_failed_not_killed(tmp_path: Path) -> None:
    """A run that dies mid-flight without a result event (kill -9 / OOM) has
    run outcome "killed" (§6) but must leave a *failed* job -- "killed" is not
    a §7 job state -- with a readable partial transcript (§11 acceptance #10)."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    claude_cmd = _write_script(tmp_path, "claude_noresult.py", NO_RESULT_SCRIPT)

    outcome = runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:01:00Z"]).__next__,
        sleep=noop_sleep,
    )

    assert outcome.outcome == "killed"
    run = db.list_runs(conn, job_id=job_id)[0]
    assert run["outcome"] == "killed"
    job = db.get_job(conn, job_id)
    assert job["state"] == "failed"
    assert job["state"] in db.TERMINAL_STATES
    # readable partial transcript survived
    stream = (runner.job_dir(tmp_path, job_id, "nightly-lint") / "stream.jsonl").read_text()
    assert '"type": "system"' in stream


def test_run_job_spawns_on_complete_detached_via_popen_argv(tmp_path: Path) -> None:
    """The on-complete command must be handed to Popen as an argv list and
    never awaited in-process -- verified here with a fake popen spy so no
    real process is involved at all."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    claude_cmd = _write_script(tmp_path, "claude_ok.py", SUCCESS_SCRIPT)

    popen_calls = []
    real_popen = subprocess.Popen

    def spying_popen(argv, **kwargs):
        popen_calls.append((list(argv), kwargs))
        return real_popen(argv, **kwargs)

    # A real, trivial command (not a real "claude") -- spying_popen delegates
    # to the real Popen, so this must actually be spawnable.
    on_complete = [sys.executable, "-c", "pass"]

    outcome = runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=on_complete,
        popen=spying_popen,
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:00:01Z"]).__next__,
        sleep=noop_sleep,
    )

    assert outcome.outcome == "done"
    on_complete_calls = [c for c in popen_calls if c[0] == on_complete]
    assert len(on_complete_calls) == 1
    assert on_complete_calls[0][1].get("start_new_session") is True


def test_run_job_uses_resume_id_from_job_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="continue-work",
        state="queued",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="keep going",
        passthrough=[],
        resume_id="prior-conversation-id",
    )
    claude_cmd = _write_script(tmp_path, "claude_ok.py", SUCCESS_SCRIPT)

    runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:00:01Z"]).__next__,
        sleep=noop_sleep,
    )

    dir_ = runner.job_dir(tmp_path, job_id, "continue-work")
    cmd = json.loads((dir_ / "cmd.json").read_text())
    assert "--resume" in cmd["argv"]
    assert cmd["argv"][cmd["argv"].index("--resume") + 1] == "prior-conversation-id"


# ---------------------------------------------------------------------------
# job-directory root is injectable (never ~/.windowsill)
# ---------------------------------------------------------------------------


def test_run_job_writes_under_injected_root_only(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    claude_cmd = _write_script(tmp_path, "claude_ok.py", SUCCESS_SCRIPT)
    custom_root = tmp_path / "custom-root"

    runner.run_job(
        conn,
        job_id,
        root=custom_root,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:00:01Z"]).__next__,
        sleep=noop_sleep,
    )

    assert (custom_root / "jobs" / "00001-nightly-lint" / "result.json").exists()


# ---------------------------------------------------------------------------
# §4.3 Δ window %, §5.4 rate limits, §2.3 retries, §8.4 notifications
# ---------------------------------------------------------------------------

# A fake claude that reports a rate limit rather than a failure (§5.4).
RATE_LIMITED_SCRIPT = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "rate_limit_event",
                  "rate_limit_info": {"status": "rejected", "resetsAt": 1785200000}}))
print(json.dumps({
    "type": "result", "subtype": "error", "is_error": True,
    "duration_ms": 20, "num_turns": 1, "result": "Claude AI usage limit reached",
    "session_id": "fake-session-limited",
}))
"""


class FakeProbe:
    """A `/usage` oracle answering from a canned list of readings, in order."""

    def __init__(self, *readings: usage.ParsedUsage) -> None:
        self.readings = list(readings)
        self.calls = 0

    def __call__(self) -> usage.ParsedUsage:
        self.calls += 1
        index = min(self.calls - 1, len(self.readings) - 1)
        return self.readings[index]


def _reading(pct: int, reset_at: str | None = None) -> usage.ParsedUsage:
    return usage.ParsedUsage(pct_used=pct, reset_at=datetime.fromisoformat(reset_at) if reset_at else None)


def _run(conn, job_id, tmp_path, script=SUCCESS_SCRIPT, **kwargs):
    return runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=_write_script(tmp_path, "claude_fake.py", script),
        on_complete=[sys.executable, "-c", "pass"],
        sleep=noop_sleep,
        now=iter(["2026-07-21T02:00:00+00:00", "2026-07-21T02:30:00+00:00"]).__next__,
        **kwargs,
    )


def test_probes_before_and_after_record_the_delta(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    probe = FakeProbe(_reading(10, "2026-07-21T07:00:00+00:00"), _reading(25, "2026-07-21T07:00:00+00:00"))

    outcome = _run(conn, job_id, tmp_path, probe=probe)

    assert probe.calls == 2
    assert outcome.delta_pct == 15
    run_row = db.list_runs(conn, job_id=job_id)[0]
    assert run_row["pct_before"] == 10
    assert run_row["pct_after"] == 25


def test_a_failing_probe_does_not_fail_the_run(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)

    def broken_probe():
        raise RuntimeError("claude is not answering")

    outcome = _run(conn, job_id, tmp_path, probe=broken_probe)

    assert outcome.outcome == "done"
    assert outcome.delta_pct is None
    assert db.get_job(conn, job_id)["state"] == "done"


def test_pre_run_probe_completes_the_ledger_row_the_tick_opened(tmp_path: Path) -> None:
    """§5.3: the tick opens a window row with no reset_at (it can't know one);
    the run's own pre-probe is the first read that does."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    db.insert_window(conn, opened_at="2026-07-21T01:59:00+00:00", opened_by=job_id)
    probe = FakeProbe(_reading(10, "2026-07-21T07:00:00+00:00"))

    _run(conn, job_id, tmp_path, probe=probe)

    rows = list(conn["window"].rows)
    assert len(rows) == 1  # completed in place, not duplicated
    assert rows[0]["reset_at"] == "2026-07-21T07:00:00+00:00"
    assert rows[0]["pct_used"] == 10


def test_rate_limited_run_requeues_and_blocks_the_scheduler(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    probe = FakeProbe(_reading(99, None), _reading(99, "2026-07-21T07:00:00+00:00"))

    outcome = _run(conn, job_id, tmp_path, script=RATE_LIMITED_SCRIPT, probe=probe)

    assert outcome.outcome == "rate_limited"

    job = db.get_job(conn, job_id)
    assert job["state"] == "ready"  # §5.4: schedulable event, not a failure
    assert job["retry_count"] == 0  # retry count untouched
    assert job["conversation_id"] == "fake-session-limited"
    assert job["resume_id"] == "fake-session-limited"
    assert job["body"] == runner.RESUME_ON_LIMIT_BODY
    assert job["pid"] is None

    sched = db.get_sched(conn)
    assert sched["blocked_until"] == "2026-07-21T07:00:00+00:00"  # the fresh probe wins
    assert "rate limit" in sched["blocked_reason"]

    assert [event["kind"] for event in db.list_events(conn, job_id=job_id)][-1] == "rate_limited"


def test_rate_limit_falls_back_to_the_signals_own_reset(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    probe = FakeProbe(_reading(99, None))  # probe reports closed, so it can't help

    _run(conn, job_id, tmp_path, script=RATE_LIMITED_SCRIPT, probe=probe)

    expected = datetime.fromtimestamp(1785200000, tz=timezone.utc).isoformat()
    assert db.get_sched(conn)["blocked_until"] == expected


def test_rate_limit_falls_back_to_now_plus_the_window_length(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    script = RATE_LIMITED_SCRIPT.replace(', "resetsAt": 1785200000', "").replace(" reached", " reached (no time given)")

    _run(conn, job_id, tmp_path, script=script, probe=FakeProbe(_reading(99, None)), config={"window": {"length": "5h"}})

    # finished_at is 02:30 in the injected clock; 5h later is 07:30.
    assert db.get_sched(conn)["blocked_until"] == "2026-07-21T07:30:00+00:00"


def test_no_resume_on_limit_keeps_the_original_body(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="nightly-lint",
        state="running",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=[],
        no_resume_on_limit=True,
    )

    _run(conn, job_id, tmp_path, script=RATE_LIMITED_SCRIPT, probe=FakeProbe(_reading(99, None)))

    job = db.get_job(conn, job_id)
    assert job["body"] == "please lint the repo"
    assert job["resume_id"] is None
    assert job["state"] == "ready"


def test_failed_run_with_retries_left_requeues(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="nightly-lint",
        state="running",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=[],
        retry_max=2,
    )

    _run(conn, job_id, tmp_path, script=ERROR_SCRIPT)

    job = db.get_job(conn, job_id)
    assert job["state"] == "ready"
    assert job["retry_count"] == 1
    assert job["finished_at"] is None
    assert db.list_events(conn, job_id=job_id)[-1]["kind"] == "retry"


def test_failed_run_with_retries_exhausted_fails(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="nightly-lint",
        state="running",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=[],
        retry_max=2,
        retry_count=2,
    )

    _run(conn, job_id, tmp_path, script=ERROR_SCRIPT)

    job = db.get_job(conn, job_id)
    assert job["state"] == "failed"
    assert job["retry_count"] == 2


def test_successful_run_notifies_on_the_jobs_channel(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="nightly-lint",
        state="running",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=[],
        notify="ntfy",
    )
    sent: list[tuple] = []

    def fake_notify(channel, title, body, *, config, message=None):
        sent.append((channel, title, body, message))
        return notify.NotifyResult(True, channel)

    _run(conn, job_id, tmp_path, notify_fn=fake_notify, probe=FakeProbe(_reading(10), _reading(13)))

    assert len(sent) == 1
    channel, title, body, _message = sent[0]
    assert channel == "ntfy"
    assert title == "nightly-lint: done"
    assert f"ccq resume {job_id}" in body
    assert "Δ 3%" in body
    assert db.list_events(conn, job_id=job_id)[-1]["kind"] == "notified"


def test_notification_flags_nonzero_permission_denials(tmp_path: Path) -> None:
    """§8.4/§9: the completion notification is exactly where an unattended
    "done" that wrote nothing must be caught before the morning after."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="nightly-lint",
        state="running",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=[],
        notify="ntfy",
    )
    sent: list[tuple] = []

    def fake_notify(channel, title, body, *, config, message=None):
        sent.append((channel, title, body, message))
        return notify.NotifyResult(True, channel)

    _run(conn, job_id, tmp_path, script=PERMISSION_DENIED_SCRIPT, notify_fn=fake_notify)

    assert len(sent) == 1
    _, _, body, _message = sent[0]
    assert "2 permission denials" in body


def test_requeued_run_does_not_notify(tmp_path: Path) -> None:
    """A job that will run again hasn't finished -- no 03:00 phone buzz."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="nightly-lint",
        state="running",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=[],
        notify="ntfy",
    )
    sent: list[tuple] = []

    _run(
        conn,
        job_id,
        tmp_path,
        script=RATE_LIMITED_SCRIPT,
        probe=FakeProbe(_reading(99, None)),
        notify_fn=lambda *a, **k: sent.append(a) or notify.NotifyResult(True, "ntfy"),
    )

    assert sent == []


def test_notification_with_no_destination_is_recorded_as_skipped(tmp_path: Path) -> None:
    """§8.4: notify on with nowhere to send is not a send and not a failure --
    but it is not silent either, or you find out the morning you needed it."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="nightly-lint",
        state="running",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=[],
        notify="email",
    )

    outcome = _run(conn, job_id, tmp_path, notify_fn=lambda *a, **k: notify.NotifyResult(False, "email", "no destination configured", skipped=True))

    assert outcome.outcome == "done"
    last = db.list_events(conn, job_id=job_id)[-1]
    assert last["kind"] == "notify_skipped"
    assert "no destination" in last["detail"]


def test_failed_notification_is_logged_and_never_fails_the_job(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="nightly-lint",
        state="running",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=[],
        notify="ntfy",
    )

    outcome = _run(conn, job_id, tmp_path, notify_fn=lambda *a, **k: notify.NotifyResult(False, "ntfy", "ConnectionError: refused"))

    assert outcome.outcome == "done"
    assert db.get_job(conn, job_id)["state"] == "done"
    last = db.list_events(conn, job_id=job_id)[-1]
    assert last["kind"] == "notify_failed"
    assert "refused" in last["detail"]
