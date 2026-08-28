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

import pytest

from windowsill import config as config_module
from windowsill import db, mail, notify, runner, usage

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
#: A run that ends its turn with a subagent still going: a clean success
#: envelope (this is the point -- job 16's looked exactly like this) preceded by
#: a task_started that only ever gets a "stopped" notification.
LIVE_SUBAGENT_SCRIPT = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "system", "subtype": "task_started", "task_id": "aAAA",
                  "tool_use_id": "toolu_A", "description": "Fix notify defaults",
                  "subagent_type": "claude", "task_type": "local_agent"}))
print(json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {"role": "assistant",
                  "content": [{"type": "text", "text": "Two agents are running. In flight: the notify work."}]}}))
print(json.dumps({"type": "system", "subtype": "task_notification", "task_id": "aAAA",
                  "tool_use_id": "toolu_A", "status": "stopped"}))
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "duration_ms": 202468, "duration_api_ms": 202000, "num_turns": 21,
    "result": "Two agents are running. In flight: the notify work.",
    "stop_reason": "end_turn", "session_id": "fake-session-live", "total_cost_usd": 0,
    "usage": {}, "modelUsage": {}, "permission_denials": [], "uuid": "u9",
}))
"""

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
# build_argv: §3.3 safety.allowed_tools default, and the yield rule
# ---------------------------------------------------------------------------


def test_build_argv_omits_allowed_tools_when_not_given() -> None:
    """Unset/empty allowed_tools injects nothing -- the pre-existing default
    behaviour, unchanged for a caller that doesn't pass it at all."""
    argv = runner.build_argv(["--max-turns", "10"])
    assert "--allowed-tools" not in argv


def test_build_argv_omits_allowed_tools_when_empty_list() -> None:
    argv = runner.build_argv(["--max-turns", "10"], allowed_tools=[])
    assert "--allowed-tools" not in argv


def test_build_argv_injects_allowed_tools_as_single_comma_joined_token() -> None:
    argv = runner.build_argv(["--max-turns", "10"], allowed_tools=["Read", "Edit", "Bash"])
    assert "--allowed-tools" in argv
    i = argv.index("--allowed-tools")
    assert argv[i + 1] == "Read,Edit,Bash"
    # Injected ahead of the job's own pass-through directives, same
    # placement rule as --resume.
    assert argv[i + 2 :] == ["--max-turns", "10"]


def test_build_argv_injects_allowed_tools_after_resume() -> None:
    argv = runner.build_argv([], resume="conv-abc", allowed_tools=["Read"])
    assert argv[-4:] == ["--resume", "conv-abc", "--allowed-tools", "Read"]


def test_build_argv_yields_when_passthrough_names_allowed_tools() -> None:
    argv = runner.build_argv(["--allowed-tools", "Bash"], allowed_tools=["Read", "Edit"])
    assert argv.count("--allowed-tools") == 1
    assert argv[-2:] == ["--allowed-tools", "Bash"]


def test_build_argv_yields_when_passthrough_names_allowed_tools_camel_case() -> None:
    argv = runner.build_argv(["--allowedTools", "Bash"], allowed_tools=["Read", "Edit"])
    assert "--allowed-tools" not in argv
    assert argv[-2:] == ["--allowedTools", "Bash"]


def test_build_argv_yields_when_passthrough_names_disallowed_tools() -> None:
    argv = runner.build_argv(["--disallowed-tools", "Bash"], allowed_tools=["Read"])
    assert "--allowed-tools" not in argv


def test_build_argv_yields_when_passthrough_names_permission_mode() -> None:
    argv = runner.build_argv(["--permission-mode", "plan"], allowed_tools=["Read"])
    assert "--allowed-tools" not in argv


def test_build_argv_yields_when_passthrough_names_dangerously_skip_permissions() -> None:
    argv = runner.build_argv(["--dangerously-skip-permissions"], allowed_tools=["Read"])
    assert "--allowed-tools" not in argv


def test_build_argv_yields_on_equals_form() -> None:
    """``--flag=value`` is a form the CLI accepts too, not just ``--flag value``."""
    argv = runner.build_argv(["--permission-mode=plan"], allowed_tools=["Read"])
    assert "--allowed-tools" not in argv


def test_build_argv_does_not_yield_on_unrelated_flags() -> None:
    argv = runner.build_argv(["--model", "claude-opus-4-8", "--max-turns", "10"], allowed_tools=["Read"])
    assert "--allowed-tools" in argv


def test_names_tool_permission_flag_true_for_every_recognised_flag() -> None:
    for flag in runner.TOOL_PERMISSION_FLAGS:
        assert runner.names_tool_permission_flag([flag, "x"]) is True


def test_names_tool_permission_flag_false_for_empty_or_unrelated() -> None:
    assert runner.names_tool_permission_flag([]) is False
    assert runner.names_tool_permission_flag(["--model", "claude-opus-4-8"]) is False


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
# parse_result_event: §3.2 "derive the result from the last result event"
# ---------------------------------------------------------------------------


def test_parse_result_event_missing_file_returns_none(tmp_path: Path) -> None:
    assert runner.parse_result_event(tmp_path / "stream.jsonl") is None


def test_parse_result_event_empty_file_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    path.write_text("")
    assert runner.parse_result_event(path) is None


def test_parse_result_event_reads_the_result_event(tmp_path: Path) -> None:
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


def test_parse_result_event_sees_past_the_background_task_teardown(tmp_path: Path) -> None:
    """Job 2's real shape (2026-08-28): a run that leaves background tasks
    behind emits its result event and *then* six `system` events killing them
    off. Reading only the final line called a successful run `killed`."""
    path = tmp_path / "stream.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"type": "assistant", "message": {}},
                {"type": "result", "subtype": "success", "is_error": False, "session_id": "cb87555e"},
                {"type": "system", "subtype": "background_tasks_changed", "tasks": [{"task_id": "bm0hdqzjx"}]},
                {"type": "system", "subtype": "task_updated", "task_id": "byfox3ke2", "patch": {"status": "killed"}},
                {"type": "system", "subtype": "task_notification", "task_id": "byfox3ke2", "status": "stopped"},
                {"type": "system", "subtype": "background_tasks_changed", "tasks": []},
                {"type": "system", "subtype": "task_updated", "task_id": "bm0hdqzjx", "patch": {"status": "killed"}},
                {"type": "system", "subtype": "task_notification", "task_id": "bm0hdqzjx", "status": "stopped"},
            )
        )
        + "\n"
    )
    event = runner.parse_result_event(path)
    assert event is not None
    assert event["subtype"] == "success"
    # The session id is what the mail needs to offer a resume at all.
    assert event["session_id"] == "cb87555e"


def test_parse_result_event_takes_the_last_of_several(tmp_path: Path) -> None:
    """A stream can carry more than one result event; the run's own outcome is
    the last, not the first."""
    path = tmp_path / "stream.jsonl"
    path.write_text(
        json.dumps({"type": "result", "subtype": "success", "n": 1})
        + "\n"
        + json.dumps({"type": "assistant", "message": {}})
        + "\n"
        + json.dumps({"type": "result", "subtype": "error_during_execution", "n": 2})
        + "\n"
    )
    assert runner.parse_result_event(path) == {"type": "result", "subtype": "error_during_execution", "n": 2}


def test_parse_result_event_none_when_the_stream_holds_no_result(tmp_path: Path) -> None:
    """A killed run's stream ends mid-stream, before a result event was ever
    emitted -- that must not be mistaken for a completed result."""
    path = tmp_path / "stream.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "message": {}}) + "\n" + json.dumps({"type": "system", "subtype": "task_started", "task_id": "t1"}) + "\n"
    )
    assert runner.parse_result_event(path) is None


def test_parse_result_event_none_when_no_result_and_last_line_malformed(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {}}) + "\nnot valid json{{{\n")
    assert runner.parse_result_event(path) is None


def test_parse_result_event_survives_a_torn_final_line(tmp_path: Path) -> None:
    """`iter_events` skips a half-written line by design (a run killed
    mid-write still yields everything up to the truncation), so a result
    event that *was* written is not hidden by the tear that followed it."""
    path = tmp_path / "stream.jsonl"
    path.write_text(json.dumps({"type": "result", "is_error": False}) + "\nnot valid json{{{\n")
    assert runner.parse_result_event(path) == {"type": "result", "is_error": False}


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


def test_outcome_incomplete_when_background_work_was_still_live() -> None:
    """The envelope is a clean success; only the live-task ledger says the run
    stopped early (§7 "blocked")."""
    event = {"type": "result", "is_error": False, "subtype": "success"}
    live = [{"task_id": "aAAA", "description": "d", "task_type": "local_agent"}]
    assert runner.outcome_from_result(event, timed_out=False, live_tasks=live) == "incomplete"


def test_a_genuinely_failed_run_stays_failed_despite_live_tasks() -> None:
    """Only "done" is downgraded -- an errored run is better described as
    failed than as incomplete."""
    event = {"type": "result", "is_error": True, "subtype": "error_max_turns"}
    live = [{"task_id": "aAAA", "description": "d", "task_type": "local_agent"}]
    assert runner.outcome_from_result(event, timed_out=False, live_tasks=live) == "failed"


def test_incomplete_maps_to_the_blocked_job_state() -> None:
    assert runner.JOB_STATE_FOR_OUTCOME["incomplete"] == "blocked"


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


def _make_job(
    conn,
    tmp_path: Path,
    *,
    timeout: str | None = None,
    log_path: str | None = None,
    passthrough: list[str] | None = None,
) -> int:
    return db.insert_job(
        conn,
        name="nightly-lint",
        state="queued",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=passthrough if passthrough is not None else ["--max-turns", "10"],
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


# ---------------------------------------------------------------------------
# run_job: §3.3 safety.allowed_tools -- injection and the yield rule
# ---------------------------------------------------------------------------


def test_run_job_injects_allowed_tools_from_config(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    config = {"safety": {"allowed_tools": ["Read", "Edit", "Bash"]}}

    runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=_write_script(tmp_path, "claude_ok.py", SUCCESS_SCRIPT),
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:05:00Z"]).__next__,
        sleep=noop_sleep,
        config=config,
    )

    dir_ = runner.job_dir(tmp_path, job_id, "nightly-lint")
    argv = json.loads((dir_ / "cmd.json").read_text())["argv"]
    assert "--allowed-tools" in argv
    assert argv[argv.index("--allowed-tools") + 1] == "Read,Edit,Bash"

    # Nothing to yield -- the job named no tool-permission flag of its own --
    # so no yield event is logged.
    kinds = [e["kind"] for e in db.list_events(conn, job_id=job_id)]
    assert "allowed_tools_yielded" not in kinds


def test_run_job_yields_and_logs_when_job_names_its_own_permission_mode(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path, passthrough=["--permission-mode", "plan"])
    config = {"safety": {"allowed_tools": ["Read", "Edit", "Bash"]}}

    runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=_write_script(tmp_path, "claude_ok.py", SUCCESS_SCRIPT),
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:05:00Z"]).__next__,
        sleep=noop_sleep,
        config=config,
    )

    dir_ = runner.job_dir(tmp_path, job_id, "nightly-lint")
    argv = json.loads((dir_ / "cmd.json").read_text())["argv"]
    assert "--allowed-tools" not in argv
    assert "--permission-mode" in argv

    events = db.list_events(conn, job_id=job_id)
    assert [e["kind"] for e in events] == ["allowed_tools_yielded", "dispatched", "done"]
    assert "tool-permission flag" in events[0]["detail"]


def test_run_job_does_not_log_yield_when_config_has_no_allowed_tools(tmp_path: Path) -> None:
    """A job naming its own --permission-mode with no configured allowed_tools
    isn't "yielding" anything -- there was nothing windowsill would have
    injected either way, so no event is logged."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path, passthrough=["--permission-mode", "plan"])

    runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=_write_script(tmp_path, "claude_ok.py", SUCCESS_SCRIPT),
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:05:00Z"]).__next__,
        sleep=noop_sleep,
        config={"safety": {"allowed_tools": []}},
    )

    kinds = [e["kind"] for e in db.list_events(conn, job_id=job_id)]
    assert kinds == ["dispatched", "done"]


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


def test_run_job_with_live_background_work_is_blocked_not_done(tmp_path: Path) -> None:
    """§7 "blocked". The result envelope is a clean success -- is_error false,
    subtype "success", stop_reason "end_turn" -- and the run still is not
    finished, because a subagent was live when the main thread stopped. This is
    job 16 exactly, which windowsill recorded as "done" and emailed as such."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    claude_cmd = _write_script(tmp_path, "claude_live.py", LIVE_SUBAGENT_SCRIPT)

    outcome = runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:01:00Z"]).__next__,
        sleep=noop_sleep,
    )

    assert outcome.outcome == "incomplete"
    assert outcome.job_state == "blocked"
    assert [task["description"] for task in outcome.live_tasks] == ["Fix notify defaults"]

    job = db.get_job(conn, job_id)
    assert job["state"] == "blocked"
    assert job["finished_at"] == "2026-07-21T02:01:00Z"
    assert db.list_runs(conn, job_id=job_id)[0]["outcome"] == "incomplete"


def test_blocked_is_not_terminal_so_ccstat_still_shows_it() -> None:
    """The morning-after reader must find a blocked job without `-a`; `ccstat`
    filters on exactly this set. Nothing re-dispatches it either, because the
    scheduler selects by explicit state name, never by "not terminal"."""
    assert "blocked" not in db.TERMINAL_STATES


def test_a_blocked_job_does_not_consume_a_retry(tmp_path: Path) -> None:
    """Re-running the body would start a second copy of work whose first copy
    may still be live, and the job needs a human either way (§7)."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    db.update_job(conn, job_id, retry_max=3)
    claude_cmd = _write_script(tmp_path, "claude_live.py", LIVE_SUBAGENT_SCRIPT)

    outcome = runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=claude_cmd,
        on_complete=[sys.executable, "-c", "pass"],
        now=iter(["2026-07-21T02:00:00Z", "2026-07-21T02:01:00Z"]).__next__,
        sleep=noop_sleep,
    )

    assert outcome.job_state == "blocked"
    job = db.get_job(conn, job_id)
    assert job["state"] == "blocked"
    assert job["retry_count"] == 0


def test_reap_reports_live_background_work_as_incomplete(tmp_path: Path) -> None:
    """reap() answers from result.json when it exists, but one event cannot
    say whether background work was live -- so it reads the stream too."""
    dir_ = tmp_path / "job"
    dir_.mkdir()
    (dir_ / "stream.jsonl").write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {"type": "system", "subtype": "task_started", "task_id": "aAAA", "description": "d", "task_type": "local_agent"},
                {"type": "result", "is_error": False, "subtype": "success", "session_id": "s1"},
            ]
        )
        + "\n"
    )

    status = runner.reap(dir_, pid=123, alive_check=lambda pid: False)

    assert status.outcome == "incomplete"
    assert [task["task_id"] for task in status.live_tasks] == ["aAAA"]


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
    # `now` is a default rather than fixed, because a job that was rate-limited
    # and requeued runs more than once and the second run is not at 02:00.
    kwargs.setdefault("now", iter(["2026-07-21T02:00:00+00:00", "2026-07-21T02:30:00+00:00"]).__next__)
    return runner.run_job(
        conn,
        job_id,
        root=tmp_path,
        claude_cmd=_write_script(tmp_path, "claude_fake.py", script),
        on_complete=[sys.executable, "-c", "pass"],
        sleep=noop_sleep,
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
    assert f"windowsill resume {job_id}" in body
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


# ---------------------------------------------------------------------------
# §8.4 the multi-run summary mail: every attempt, not just the last
# ---------------------------------------------------------------------------


def _limited(prefix: str) -> str:
    """A rate-limited run whose closing line is its own, so a mail listing
    several attempts can be checked for the right one against the right run."""
    return RATE_LIMITED_SCRIPT.replace('"Claude AI usage limit reached"', f'"{prefix} Claude AI usage limit reached"')


def _clock(*times: str):
    return iter(times).__next__


def _notified_job(conn, tmp_path: Path, channel: str = "email") -> int:
    return db.insert_job(
        conn,
        name="windowsill-todo-3",
        state="running",
        submitted_at="2026-07-20T21:00:00Z",
        cwd=str(tmp_path),
        body="please lint the repo",
        passthrough=[],
        notify=channel,
        retry_max=3,
    )


def test_a_rate_limited_runs_last_words_are_persisted_to_the_run_row(tmp_path: Path) -> None:
    """The job dir holds one stream.jsonl and the next attempt overwrites it,
    so an attempt's summary survives only if it is copied out at the time --
    and the rate-limited attempts are exactly the ones worth recovering."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)

    _run(conn, job_id, tmp_path, script=_limited("parser reworked;"), probe=FakeProbe(_reading(99, None)))

    row = db.list_runs(conn, job_id=job_id)[0]
    assert row["outcome"] == "rate_limited"
    assert row["summary"] == "parser reworked; Claude AI usage limit reached"


def test_a_successful_runs_summary_is_persisted_too(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)

    _run(conn, job_id, tmp_path)

    assert db.list_runs(conn, job_id=job_id)[0]["summary"] == "ok: please lint the repo"


def test_the_summary_mail_covers_every_attempt_with_its_totals(tmp_path: Path) -> None:
    """The user's report: "the summary email only mentions the most recent run.
    It should include the outputs from the previous, rate-limited runs. And
    highlight that it took 3 runs, and include the total budget spent." """
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _notified_job(conn, tmp_path)
    sent: list[tuple] = []

    def fake_notify(channel, title, body, *, config, message=None):
        sent.append((channel, title, body, message))
        return notify.NotifyResult(True, channel)

    _run(
        conn,
        job_id,
        tmp_path,
        script=_limited("parser reworked;"),
        probe=FakeProbe(_reading(30), _reading(100)),
        now=_clock("2026-07-20T21:21:00+00:00", "2026-07-20T21:52:00+00:00"),
        notify_fn=fake_notify,
    )
    _run(
        conn,
        job_id,
        tmp_path,
        script=_limited("tests picked back up;"),
        probe=FakeProbe(_reading(0), _reading(100)),
        now=_clock("2026-07-21T02:00:00+00:00", "2026-07-21T02:35:00+00:00"),
        notify_fn=fake_notify,
    )
    _run(
        conn,
        job_id,
        tmp_path,
        probe=FakeProbe(_reading(0), _reading(4)),
        now=_clock("2026-07-21T07:00:00+00:00", "2026-07-21T07:03:00+00:00"),
        notify_fn=fake_notify,
    )

    # Only the terminal run mailed; the two requeues stayed quiet (§7).
    assert len(sent) == 1
    _, _, _, message = sent[0]
    assert "3 runs" in message["Subject"]

    text = {part.get_content_type(): part for part in message.walk() if not part.is_multipart()}["text/plain"].get_content()
    assert "parser reworked; Claude AI usage limit reached" in text
    assert "tests picked back up; Claude AI usage limit reached" in text
    # 21:21 to 07:03 is the number the reader wants; 70 + 100 + 4 is the floor.
    assert "3 runs · 9h 42m wall" in text
    assert "174% of the window" in text
    assert "floor, not a sum" in text


def test_a_single_run_job_mails_no_attempt_history(tmp_path: Path) -> None:
    """The ordinary case must look exactly as it did before any of this."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _notified_job(conn, tmp_path)
    sent: list[tuple] = []

    _run(
        conn,
        job_id,
        tmp_path,
        notify_fn=lambda channel, title, body, *, config, message=None: (sent.append(message), notify.NotifyResult(True, channel))[1],
    )

    message = sent[0]
    assert "runs" not in message["Subject"]
    assert "Attempts" not in message.as_string()


# ---------------------------------------------------------------------------
# §8.4 [notify].on_rate_limit -- the "still alive, back at 02:00" ping
# ---------------------------------------------------------------------------

ON_RATE_LIMIT: dict[str, Any] = {"notify": {"on_rate_limit": True}}


def _rate_limited_with(conn, job_id: int, tmp_path: Path, config: dict[str, Any] | None) -> list[tuple]:
    sent: list[tuple] = []

    def fake_notify(channel, title, body, *, config, message=None):
        sent.append((channel, title, body, message))
        return notify.NotifyResult(True, notify.split_channel(channel)[0])

    _run(
        conn,
        job_id,
        tmp_path,
        script=RATE_LIMITED_SCRIPT,
        probe=FakeProbe(_reading(30), _reading(100, "2026-07-21T07:00:00+00:00")),
        config=config,
        notify_fn=fake_notify,
    )
    return sent


def test_a_rate_limited_requeue_is_silent_by_default(tmp_path: Path) -> None:
    """Default off, and off means nothing at all is attempted: a job with
    `--retry 3` that keeps hitting the ceiling would otherwise send one of
    these per attempt."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _notified_job(conn, tmp_path, channel="ntfy")

    sent = _rate_limited_with(conn, job_id, tmp_path, {"notify": config_module.DEFAULTS["notify"]})

    assert sent == []
    assert [event["kind"] for event in db.list_events(conn, job_id=job_id)][-1] == "rate_limited"


def test_on_rate_limit_sends_one_ping_with_the_resume_time_usage_and_retries(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _notified_job(conn, tmp_path, channel="ntfy")

    sent = _rate_limited_with(conn, job_id, tmp_path, ON_RATE_LIMIT)

    assert len(sent) == 1
    channel, title, body, message = sent[0]
    assert channel == "ntfy"
    # The time the *queue* will wake, which the fresh probe won over the
    # error's own timestamp.
    resumes = mail._local("2026-07-21T07:00:00+00:00")
    assert title == f"windowsill-todo-3: rate-limited, back at {resumes}"
    assert f"resuming at {resumes}." in body
    assert "This pause cost no retry (3 still available for an actual failure)." in body
    assert "Session window: 100% used" in body
    assert message is None  # ntfy carries the one-liner, not a composed mail


def test_the_rate_limit_ping_does_not_change_the_requeue(tmp_path: Path) -> None:
    """§5.4 is untouched by §8.4: the job is still not terminal, still keeps
    its retry count, and still gets no summary mail."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _notified_job(conn, tmp_path, channel="ntfy")

    sent = _rate_limited_with(conn, job_id, tmp_path, ON_RATE_LIMIT)

    job = db.get_job(conn, job_id)
    assert job["state"] == "ready"
    assert job["retry_count"] == 0
    assert db.get_sched(conn)["blocked_until"] == "2026-07-21T07:00:00+00:00"
    assert sent[0][3] is None  # no multipart summary body went out


def test_the_rate_limit_ping_is_recorded_as_an_ordinary_notify_event(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _notified_job(conn, tmp_path, channel="ntfy")

    _rate_limited_with(conn, job_id, tmp_path, ON_RATE_LIMIT)

    last = db.list_events(conn, job_id=job_id)[-1]
    assert last["kind"] == "notified"  # the same three kinds; which one it was is the detail
    assert last["detail"] == "rate limit: ntfy"


def test_the_rate_limit_ping_composes_a_plain_mail_on_the_email_channel(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _notified_job(conn, tmp_path, channel="email")

    sent = _rate_limited_with(conn, job_id, tmp_path, ON_RATE_LIMIT)

    message = sent[0][3]
    assert message is not None
    assert message["Subject"].startswith("[windowsill ")
    assert message.get_content_type() == "text/plain"
    assert "This pause cost no retry" in message.get_content()


def test_notify_none_stays_silent_even_with_on_rate_limit_set(tmp_path: Path) -> None:
    """The flag turns a notification on; it never turns a channel on."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _notified_job(conn, tmp_path, channel="none")

    sent = _rate_limited_with(conn, job_id, tmp_path, ON_RATE_LIMIT)

    assert sent == []
    assert not [event for event in db.list_events(conn, job_id=job_id) if event["kind"].startswith("notif")]


def test_a_failed_rate_limit_ping_is_logged_and_never_fails_the_requeue(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _notified_job(conn, tmp_path, channel="ntfy")

    outcome = _run(
        conn,
        job_id,
        tmp_path,
        script=RATE_LIMITED_SCRIPT,
        probe=FakeProbe(_reading(99, None)),
        config=ON_RATE_LIMIT,
        notify_fn=lambda *a, **k: notify.NotifyResult(False, "ntfy", "ConnectionError: refused"),
    )

    assert outcome.outcome == "rate_limited"
    assert db.get_job(conn, job_id)["state"] == "ready"
    last = db.list_events(conn, job_id=job_id)[-1]
    assert last["kind"] == "notify_failed"
    assert last["detail"] == "rate limit: ntfy: ConnectionError: refused"


# ---------------------------------------------------------------------------
# §3.2 the default appended system prompt -- constants and the byte budget
# ---------------------------------------------------------------------------


def test_system_prompt_byte_budget() -> None:
    """§0.1: context is *the* cost driver, and the way a system prompt goes bad
    is by quietly accreting -- a sentence here, a caveat there, none of them
    individually worth arguing about. So the budget is a failing test, not a
    comment. The numbers are the plan's, with the headroom deliberately tight
    (~25%): enough to reword, not enough to bolt on a new section without
    someone consciously raising the ceiling and justifying it in a diff.
    """
    always_on = runner.SYSTEM_PROMPT_BASE + runner.SYSTEM_PROMPT_JOURNAL
    assert len(always_on) < 1200, f"the always-injected prompt has grown to {len(always_on)} bytes"

    worst_case = always_on + runner.SYSTEM_PROMPT_REEXEC
    assert len(worst_case) < 1500, f"the fully-expanded prompt has grown to {len(worst_case)} bytes"


def test_system_prompt_constants_are_the_ones_build_argv_yields_to() -> None:
    """The four flags windowsill yields to, and the one it injects. Locked down
    because a typo here fails open: an unrecognised flag means windowsill
    injects its own on top of the job's, and `--append-system-prompt-file`
    plus `--append-system-prompt` is a hard CLI error that kills the run."""
    assert runner.SYSTEM_PROMPT_FLAGS == {
        "--append-system-prompt",
        "--append-system-prompt-file",
        "--system-prompt",
        "--system-prompt-file",
    }
    assert runner.JOURNAL_FILENAME == "journal.md"


def test_journal_prompt_names_the_absolute_path_and_the_four_headings() -> None:
    plan = runner.resolve_system_prompt(
        [],
        config={},
        job_dir=Path("/state/jobs/00042-refactor-auth"),
        is_anchor=False,
        prior_runs=[],
        resume_id=None,
    )
    assert "/state/jobs/00042-refactor-auth/journal.md" in plan.text
    for heading in ("Goal", "Done", "Next", "Notes"):
        assert heading in plan.text
    # BLOCKED.md is deliberate: design/02-job-file.md's own example job file
    # already writes this instruction by hand. The prompt hoists that
    # boilerplate rather than competing with it -- same filename, same idea.
    assert "BLOCKED.md" in plan.text


# ---------------------------------------------------------------------------
# build_argv: §3.2 system prompt / --add-dir injection and ordering
# ---------------------------------------------------------------------------


def test_build_argv_omits_system_prompt_when_none() -> None:
    argv = runner.build_argv(["--max-turns", "10"])
    assert "--append-system-prompt" not in argv
    assert "--add-dir" not in argv


def test_build_argv_injects_system_prompt_before_passthrough() -> None:
    """Ordering is load-bearing. `--append-system-prompt` is registered with a
    plain string parser, so a repeated occurrence replaces rather than
    accumulates -- injecting *first* means a user's own occurrence is later and
    wins. (The yield rule means this should never actually arise; it is the
    belt to that braces.)"""
    argv = runner.build_argv(["--model", "opus"], system_prompt="be brief")
    assert argv.index("--append-system-prompt") < argv.index("--model")
    assert argv[argv.index("--append-system-prompt") + 1] == "be brief"


def test_build_argv_injects_add_dir_once_per_entry() -> None:
    argv = runner.build_argv([], system_prompt="be brief", add_dirs=["/a", "/b"])
    assert argv.count("--add-dir") == 2
    assert argv[argv.index("--add-dir") + 1] == "/a"


def test_build_argv_add_dir_is_the_last_injected_token_before_passthrough() -> None:
    """`--add-dir` takes `<directories...>` -- it is *variadic* on the installed
    CLI (2.1.218), not the single-argument flag the plan assumed. A variadic
    option keeps swallowing tokens until the next one that looks like a flag,
    so a bare positional immediately after it would be eaten as a second
    directory. Injecting it last, directly abutting pass-through, is what keeps
    that harmless: every `#$` directive is itself a flag, so the swallow stops
    at the first pass-through token.
    """
    argv = runner.build_argv(["--model", "opus"], system_prompt="be brief", add_dirs=["/job/dir"])
    assert argv[-3:] == ["/job/dir", "--model", "opus"]
    assert argv[-4] == "--add-dir"


def test_build_argv_injects_system_prompt_after_resume_and_allowed_tools() -> None:
    argv = runner.build_argv([], resume="conv-abc", allowed_tools=["Read"], system_prompt="be brief", add_dirs=["/job/dir"])
    assert argv.index("--resume") < argv.index("--allowed-tools") < argv.index("--append-system-prompt") < argv.index("--add-dir")


def test_build_argv_injects_add_dir_even_without_prompt_text() -> None:
    argv = runner.build_argv([], add_dirs=["/job/dir"])
    assert "--append-system-prompt" not in argv
    assert argv[-2:] == ["--add-dir", "/job/dir"]


# ---------------------------------------------------------------------------
# §3.2 the yield rule: a job that names any system-prompt flag gets its own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "own",
    [
        ["--append-system-prompt", "mine"],
        ["--append-system-prompt-file", "/tmp/p.txt"],
        ["--system-prompt", "mine"],
        ["--system-prompt-file", "/tmp/p.txt"],
        ["--append-system-prompt="],  # the documented per-job opt-out, equals form
        ["--system-prompt=mine"],
    ],
)
def test_build_argv_yields_entirely_when_passthrough_names_a_system_prompt_flag(own: list[str]) -> None:
    """Yielding is total: no prompt *and* no `--add-dir`. Granting directory
    access for a journal the agent was never told about would be pointless, and
    `--append-system-prompt` alongside `--append-system-prompt-file` is a hard
    CLI error that would kill the run outright."""
    argv = runner.build_argv(own, system_prompt="windowsill's", add_dirs=["/job/dir"])
    assert "--append-system-prompt" not in argv[: len(argv) - len(own)]
    assert "--add-dir" not in argv
    assert argv[-len(own) :] == own


def test_build_argv_does_not_yield_system_prompt_on_unrelated_flags() -> None:
    argv = runner.build_argv(["--model", "opus", "--max-turns", "10"], system_prompt="be brief", add_dirs=["/job/dir"])
    assert "--append-system-prompt" in argv
    assert "--add-dir" in argv


@pytest.mark.parametrize(
    "own",
    [
        ["--append-system-prompt", "mine"],
        ["--append-system-prompt-file", "/tmp/p.txt"],
        ["--system-prompt", "mine"],
        ["--system-prompt-file", "/tmp/p.txt"],
        ["--append-system-prompt="],
    ],
)
def test_resolve_system_prompt_yields_and_says_so(own: list[str], tmp_path: Path) -> None:
    plan = runner.resolve_system_prompt(own, config={}, job_dir=tmp_path, is_anchor=False, prior_runs=[], resume_id=None)
    assert plan.text is None
    assert plan.add_dir is None
    # Distinct from "there was nothing to inject": `yielded` is what run_job
    # logs, so the morning's event log says *why* no prompt went in.
    assert plan.yielded is True


def test_resolve_system_prompt_does_not_claim_a_yield_when_nothing_was_on_offer(tmp_path: Path) -> None:
    """A job naming its own `--system-prompt` while `[prompt].enabled = false`
    isn't yielding anything -- windowsill had nothing to inject either way, so
    there is no event worth logging."""
    plan = runner.resolve_system_prompt(
        ["--system-prompt", "mine"],
        config={"prompt": {"enabled": False}},
        job_dir=tmp_path,
        is_anchor=False,
        prior_runs=[],
        resume_id=None,
    )
    assert plan.text is None
    assert plan.yielded is False


# ---------------------------------------------------------------------------
# §5.2 anchor jobs are never injected
# ---------------------------------------------------------------------------


def test_resolve_system_prompt_skips_anchor_jobs(tmp_path: Path) -> None:
    """An anchor is a Haiku "reply with the single word: ok" ping whose whole
    point is to cost nothing. 250 tokens and a journal instruction is not
    nothing."""
    plan = runner.resolve_system_prompt([], config={}, job_dir=tmp_path, is_anchor=True, prior_runs=[], resume_id=None)
    assert plan.text is None
    assert plan.add_dir is None
    assert plan.yielded is False


# ---------------------------------------------------------------------------
# [prompt] config: enabled / journal / text
# ---------------------------------------------------------------------------


def test_resolve_system_prompt_default_config_injects_base_and_journal(tmp_path: Path) -> None:
    plan = runner.resolve_system_prompt([], config={}, job_dir=tmp_path, is_anchor=False, prior_runs=[], resume_id=None)
    assert runner.SYSTEM_PROMPT_BASE in plan.text
    assert "status journal" in plan.text
    assert plan.add_dir == tmp_path


def test_resolve_system_prompt_enabled_false_injects_nothing(tmp_path: Path) -> None:
    plan = runner.resolve_system_prompt([], config={"prompt": {"enabled": False}}, job_dir=tmp_path, is_anchor=False, prior_runs=[], resume_id=None)
    assert plan.text is None
    assert plan.add_dir is None


def test_resolve_system_prompt_journal_false_keeps_prompt_drops_journal_and_add_dir(tmp_path: Path) -> None:
    """The two switches are separate because they cost differently: the prompt
    is ~250 cached tokens, the journal is an Edit/Write per update that stays
    in context forever. Someone may want the unattended-behaviour prompt
    without paying the write overhead."""
    plan = runner.resolve_system_prompt([], config={"prompt": {"journal": False}}, job_dir=tmp_path, is_anchor=False, prior_runs=[], resume_id=None)
    assert plan.text == runner.SYSTEM_PROMPT_BASE
    assert "journal.md" not in plan.text
    assert plan.add_dir is None


def test_resolve_system_prompt_text_override_is_verbatim_with_journal_substituted(tmp_path: Path) -> None:
    plan = runner.resolve_system_prompt(
        [],
        config={"prompt": {"text": "Only this. Journal: {journal}"}},
        job_dir=tmp_path,
        is_anchor=False,
        prior_runs=[],
        resume_id=None,
    )
    assert plan.text == f"Only this. Journal: {tmp_path / 'journal.md'}"
    assert runner.SYSTEM_PROMPT_BASE not in plan.text
    assert plan.add_dir == tmp_path


def test_resolve_system_prompt_text_override_survives_other_braces(tmp_path: Path) -> None:
    """Substitution is a plain string replace, not `str.format` -- a user's
    override is prose, and prose contains braces (JSON examples, f-strings,
    shell expansions). `str.format` would raise KeyError on all of them."""
    plan = runner.resolve_system_prompt(
        [],
        config={"prompt": {"text": 'Emit {"ok": true} and ${HOME}. See {journal}.'}},
        job_dir=tmp_path,
        is_anchor=False,
        prior_runs=[],
        resume_id=None,
    )
    assert plan.text == f'Emit {{"ok": true}} and ${{HOME}}. See {tmp_path / "journal.md"}.'


def test_resolve_system_prompt_text_override_wins_over_the_reexec_line(tmp_path: Path) -> None:
    """`text` replaces the built-in *wholesale*, not "the base part of it"."""
    plan = runner.resolve_system_prompt(
        [],
        config={"prompt": {"text": "Only this."}},
        job_dir=tmp_path,
        is_anchor=False,
        prior_runs=[{"id": 1, "outcome": "timeout"}],
        resume_id=None,
    )
    assert plan.text == "Only this."


def test_resolve_system_prompt_reads_the_real_shipped_defaults(tmp_path: Path) -> None:
    """Guards the seam between config.DEFAULTS and the `.get(..., True)`
    fallbacks in resolve_system_prompt: flipping a shipped default without
    flipping the fallback would give a partial config file different behaviour
    from no config file at all."""
    plan = runner.resolve_system_prompt([], config=config_module.DEFAULTS, job_dir=tmp_path, is_anchor=False, prior_runs=[], resume_id=None)
    assert runner.SYSTEM_PROMPT_BASE in plan.text
    assert "journal.md" in plan.text
    assert plan.add_dir == tmp_path


# ---------------------------------------------------------------------------
# §3.2 the re-execution gate -- the rule most likely to regress
# ---------------------------------------------------------------------------


def test_resolve_system_prompt_no_reexec_line_on_a_first_run(tmp_path: Path) -> None:
    plan = runner.resolve_system_prompt([], config={}, job_dir=tmp_path, is_anchor=False, prior_runs=[], resume_id=None)
    assert "attempt" not in plan.text
    assert "Do not start over" not in plan.text


def test_resolve_system_prompt_reexec_line_when_prior_run_and_no_resume(tmp_path: Path) -> None:
    """The `--retry` / `--no-resume-on-limit` shape: both re-dispatch with a
    brand-new conversation and the original body, discarding everything the
    previous attempt did. This is the only case where windowsill *knows* the
    session is a fresh-context re-execution."""
    plan = runner.resolve_system_prompt(
        [],
        config={},
        job_dir=tmp_path,
        is_anchor=False,
        prior_runs=[{"id": 1, "outcome": "timeout"}],
        resume_id=None,
    )
    assert "This is attempt 2 of this job" in plan.text
    assert "timeout" in plan.text
    assert str(tmp_path / "journal.md") in plan.text
    assert "Do not start over" in plan.text


def test_resolve_system_prompt_no_reexec_line_when_resuming(tmp_path: Path) -> None:
    """A resumed agent carries its own history in context. Telling it to go
    read a file about what it already remembers is pure cost."""
    plan = runner.resolve_system_prompt(
        [],
        config={},
        job_dir=tmp_path,
        is_anchor=False,
        prior_runs=[{"id": 1, "outcome": "rate_limited"}],
        resume_id="conv-abc",
    )
    assert "attempt" not in plan.text


def test_resolve_system_prompt_reexec_attempt_counts_every_prior_run(tmp_path: Path) -> None:
    plan = runner.resolve_system_prompt(
        [],
        config={},
        job_dir=tmp_path,
        is_anchor=False,
        prior_runs=[{"id": 1, "outcome": "failed"}, {"id": 2, "outcome": "timeout"}, {"id": 3, "outcome": "killed"}],
        resume_id=None,
    )
    assert "This is attempt 4 of this job" in plan.text
    # The *previous* attempt's outcome, not the first one's.
    assert "ended: killed" in plan.text


def test_resolve_system_prompt_reexec_tolerates_a_prior_run_with_no_outcome(tmp_path: Path) -> None:
    """A run row whose process vanished before anything was written back still
    has a NULL outcome. Formatting "None" into the prompt would be worse than
    saying so."""
    plan = runner.resolve_system_prompt([], config={}, job_dir=tmp_path, is_anchor=False, prior_runs=[{"id": 1, "outcome": None}], resume_id=None)
    assert "ended: unknown" in plan.text


def test_resolve_system_prompt_no_reexec_line_when_journal_is_off(tmp_path: Path) -> None:
    """The re-execution line's whole instruction is "read the journal". With no
    journal being kept there is nothing to read, so it is 40 wasted tokens
    pointing at a file that does not exist."""
    plan = runner.resolve_system_prompt(
        [],
        config={"prompt": {"journal": False}},
        job_dir=tmp_path,
        is_anchor=False,
        prior_runs=[{"id": 1, "outcome": "timeout"}],
        resume_id=None,
    )
    assert plan.text == runner.SYSTEM_PROMPT_BASE


# ---------------------------------------------------------------------------
# run_job: the wiring -- a real dispatch gets the prompt, the dir, and the gate
# ---------------------------------------------------------------------------


def _dispatched_argv(tmp_path: Path, job_id: int, name: str = "nightly-lint") -> list[str]:
    return json.loads((runner.job_dir(tmp_path, job_id, name) / "cmd.json").read_text())["argv"]


def test_run_job_injects_the_system_prompt_and_the_job_dir(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)

    _run(conn, job_id, tmp_path, config={"prompt": {"enabled": True, "journal": True}})

    argv = _dispatched_argv(tmp_path, job_id)
    dir_ = runner.job_dir(tmp_path, job_id, "nightly-lint")
    assert argv[argv.index("--add-dir") + 1] == str(dir_)
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert runner.SYSTEM_PROMPT_BASE in prompt
    assert str(dir_ / "journal.md") in prompt
    # The journal's directory is reachable and exists by dispatch time, so the
    # agent's first Write does not fail on a missing parent.
    assert dir_.is_dir()


def test_run_job_journal_lives_in_the_job_dir_never_the_working_tree(tmp_path: Path) -> None:
    """§3.3 leaves the branch for morning review. A journal in the worktree
    pollutes that branch and the agent may well commit it. The job dir is also
    keyed on job id, so it survives every re-dispatch of the job -- which is
    the entire point."""
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="nightly-lint",
        state="queued",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(cwd),
        body="please lint the repo",
        passthrough=[],
    )

    _run(conn, job_id, tmp_path)

    argv = _dispatched_argv(tmp_path, job_id)
    journal_dir = Path(argv[argv.index("--add-dir") + 1])
    assert journal_dir == runner.job_dir(tmp_path, job_id, "nightly-lint")
    assert cwd not in journal_dir.parents and journal_dir != cwd


def test_run_job_yields_and_logs_when_the_job_names_its_own_system_prompt(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path, passthrough=["--append-system-prompt", ""])

    _run(conn, job_id, tmp_path)

    argv = _dispatched_argv(tmp_path, job_id)
    assert argv.count("--append-system-prompt") == 1
    assert argv[-2:] == ["--append-system-prompt", ""]
    assert "--add-dir" not in argv

    events = db.list_events(conn, job_id=job_id)
    assert events[0]["kind"] == "system_prompt_yielded"
    assert "system-prompt flag" in events[0]["detail"]


def test_run_job_anchor_gets_no_system_prompt(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = db.insert_job(
        conn,
        name="anchor",
        state="queued",
        submitted_at="2026-07-21T00:00:00Z",
        cwd=str(tmp_path),
        body="Reply with the single word: ok",
        passthrough=[],
        is_anchor=True,
    )

    _run(conn, job_id, tmp_path)

    argv = _dispatched_argv(tmp_path, job_id, "anchor")
    assert "--append-system-prompt" not in argv
    assert "--add-dir" not in argv
    assert "system_prompt_yielded" not in [e["kind"] for e in db.list_events(conn, job_id=job_id)]


def test_run_job_disabled_by_config_injects_nothing(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)

    _run(conn, job_id, tmp_path, config={"prompt": {"enabled": False}})

    argv = _dispatched_argv(tmp_path, job_id)
    assert "--append-system-prompt" not in argv
    assert "--add-dir" not in argv


def test_run_job_second_dispatch_without_resume_gets_the_reexec_line(tmp_path: Path) -> None:
    """The retry path for real: run once, reset to ready with no resume_id (what
    `--retry` does), run again. The second dispatch must know it is attempt 2."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)

    _run(conn, job_id, tmp_path)
    assert "attempt" not in _dispatched_argv(tmp_path, job_id)[_dispatched_argv(tmp_path, job_id).index("--append-system-prompt") + 1]

    db.update_job(conn, job_id, state="ready", resume_id=None)
    _run(conn, job_id, tmp_path)

    argv = _dispatched_argv(tmp_path, job_id)
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "This is attempt 2 of this job" in prompt
    assert "ended: done" in prompt


def test_run_job_resumed_dispatch_gets_no_reexec_line(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)

    _run(conn, job_id, tmp_path)
    db.update_job(conn, job_id, state="ready", resume_id="conv-abc")
    _run(conn, job_id, tmp_path)

    argv = _dispatched_argv(tmp_path, job_id)
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "attempt" not in prompt


# ---------------------------------------------------------------------------
# §8.4 the completion email falls back to the journal only when there is no prose
# ---------------------------------------------------------------------------


def _facts(conn, job: dict, dir_: Path, events: list[dict]) -> Any:
    return runner._run_facts(
        conn,
        job,
        "done",
        outcome="done",
        result_event=None,
        events=events,
        live_tasks=[],
        delta_pct=None,
        permission_denials=None,
        conversation_id="conv-abc",
        reading=None,
        finished_at="2026-07-21T02:30:00+00:00",
        config={},
        job_dir=dir_,
    )


_PROSE_EVENT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "All green."}]}}


def test_run_facts_summary_falls_back_to_the_journal_when_there_is_no_prose(tmp_path: Path) -> None:
    """A run killed by `--timeout` mid-tool-call leaves no closing message, so
    today's email is worthless -- and that is precisely the morning where the
    journal on disk is intact and worth reading."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    dir_ = tmp_path / "jobdir"
    dir_.mkdir()
    (dir_ / "journal.md").write_text("Goal — ship it.\nNext — the mail tests.\n")

    facts = _facts(conn, db.get_job(conn, job_id), dir_, [])

    assert "Next — the mail tests." in facts.summary
    # Flagged as a fallback, so nobody reads a checklist as a closing report.
    assert "no closing message" in facts.summary


def test_run_facts_summary_prefers_prose_and_ignores_the_journal(tmp_path: Path) -> None:
    """The normal case: the final assistant turn is prose addressed to the
    reader, which beats a Goal/Done/Next checklist. Swapping it out would be a
    downgrade for the 90% case."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    dir_ = tmp_path / "jobdir"
    dir_.mkdir()
    (dir_ / "journal.md").write_text("Goal — ship it.\n")

    facts = _facts(conn, db.get_job(conn, job_id), dir_, [_PROSE_EVENT])

    assert facts.summary == "All green."
    assert "Goal" not in facts.summary


def test_run_facts_summary_is_empty_with_neither_prose_nor_journal(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    dir_ = tmp_path / "jobdir"
    dir_.mkdir()

    facts = _facts(conn, db.get_job(conn, job_id), dir_, [])

    assert facts.summary == ""


def test_run_facts_summary_ignores_an_empty_journal(tmp_path: Path) -> None:
    """A journal the agent created and never wrote to is not a summary."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    dir_ = tmp_path / "jobdir"
    dir_.mkdir()
    (dir_ / "journal.md").write_text("   \n\n")

    facts = _facts(conn, db.get_job(conn, job_id), dir_, [])

    assert facts.summary == ""


# ---------------------------------------------------------------------------
# §5.4 the mail reports the sched hold only while it is still holding
# ---------------------------------------------------------------------------


def test_run_facts_passes_on_a_live_block(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    dir_ = tmp_path / "jobdir"
    dir_.mkdir()
    # `_facts` finishes the run at 2026-07-21T02:30.
    db.update_sched(conn, blocked_until="2026-07-21T05:00:00+00:00", blocked_reason="rate limit")

    facts = _facts(conn, db.get_job(conn, job_id), dir_, [])

    assert facts.blocked_until == "2026-07-21T05:00:00+00:00"
    assert facts.blocked_reason == "rate limit"


def test_run_facts_drops_an_expired_block(tmp_path: Path) -> None:
    """`blocked_until` is never cleared when it lapses -- the scheduler just
    stops honouring it -- so the row outlives the hold. Job 2's mail announced
    a hold that had expired a week before the run it was reporting on."""
    conn = db.connect(tmp_path / "windowsill.db")
    job_id = _make_job(conn, tmp_path)
    dir_ = tmp_path / "jobdir"
    dir_.mkdir()
    db.update_sched(conn, blocked_until="2026-07-14T05:00:00+00:00", blocked_reason="rate limit")

    facts = _facts(conn, db.get_job(conn, job_id), dir_, [])

    assert facts.blocked_until is None
    # The reason goes with it: half a lapsed hold is not a fact about the queue.
    assert facts.blocked_reason is None
    assert not any("blocked until" in line for line in mail.next_action(facts))
