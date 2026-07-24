"""Tests for ccq.cli -- the M1 command set (DESIGN.md §8.1).

These tests spawn NO real `claude`, touch NO network, block on NO real
clock, and spawn NO real subprocess: every command is driven through
`click.testing.CliRunner` against a temp DB (`--home tmp_path`), and the
handful of side-effecting seams cli.py exposes as private module
attributes (`_iso_now`, `_sleep`, `_terminate`, `_exec`, `_spawn_run`) are
monkeypatched exactly like the sibling modules' injectable keywords.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from ccq import cli, db, scheduler
from ccq import runner as runner_mod

NOW = "2026-07-22T09:00:00+00:00"
PAST = "2026-07-21T09:00:00+00:00"
FUTURE = "2026-07-23T09:00:00+00:00"
OLD = "2026-07-01T09:00:00+00:00"  # well over 7 / 30 days before NOW

# A tiny stand-in for `claude`: reads the body on stdin and emits a canned
# stream-json transcript (a `system` init then a `result`), so `ccq run`'s
# real runner.run_job is exercised end to end without the real binary. Same
# pattern as tests/test_runner.py's fake-claude scripts.
FAKE_CLAUDE_SCRIPT = """
import json, sys
body = sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "duration_ms": 537, "num_turns": 3, "result": "ok: " + body.strip(),
    "session_id": "fake-session-123",
}))
"""


def _fake_claude_cmd(tmp_path: Path) -> list[str]:
    path = tmp_path / "fake_claude.py"
    path.write_text(FAKE_CLAUDE_SCRIPT)
    return [sys.executable, str(path)]


def _noop_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def run(runner: CliRunner, home: Path, *args: str, input: str | None = None):
    return runner.invoke(cli.main, ["--home", str(home), *args], input=input)


def make_db(home: Path) -> "db.sqlite_utils.Database":
    return db.connect(home=home)


def add_job(
    conn: "db.sqlite_utils.Database",
    *,
    name: str = "job",
    state: str = "queued",
    submitted_at: str = PAST,
    started_at: str | None = None,
    finished_at: str | None = None,
    cwd: str = "/tmp/work",
    pid: int | None = None,
    conversation_id: str | None = None,
    at_time: str | None = None,
) -> int:
    return db.insert_job(
        conn,
        name=name,
        state=state,
        submitted_at=submitted_at,
        started_at=started_at,
        finished_at=finished_at,
        cwd=cwd,
        body="do the thing",
        passthrough=["--model", "claude-opus-4-8"],
        pid=pid,
        conversation_id=conversation_id,
        at_time=at_time,
    )


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_iso_now", lambda: NOW)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def pin_tz():
    """Pin the process-local timezone for a test, restoring it afterwards.

    ``--at`` normalization (cli._normalize_at) resolves a naïve wall-clock in
    the *system local* zone, so any test asserting an exact stored UTC value
    must fix that zone rather than depend on the CI machine's. Sets ``TZ`` and
    calls ``time.tzset()`` (POSIX only), then undoes both on teardown.
    """
    saved = os.environ.get("TZ")

    def _set(tz: str) -> None:
        os.environ["TZ"] = tz
        time.tzset()

    yield _set

    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


BASIC_JOB = """\
#$ --name refactor-auth
#$ --at 2026-07-18T02:00
#$ --cwd ~/code/myapp
#$ --window fresh
#$ --retry 2
#$ --notify email

Do the thing.
"""


# ---------------------------------------------------------------------------
# ccq sub
# ---------------------------------------------------------------------------


def test_sub_from_file_parses_directives_and_prints_id(tmp_path: Path, runner: CliRunner, pin_tz) -> None:
    home = tmp_path / "home"
    job_file = tmp_path / "refactor-auth.md"
    job_file.write_text(BASIC_JOB)

    pin_tz("Etc/GMT-1")  # fixed UTC+1, so the naïve "--at" has a knowable UTC value
    result = run(runner, home, "sub", str(job_file))

    assert result.exit_code == 0, result.output
    job_id = int(result.output.strip())

    conn = make_db(home)
    job = db.get_job(conn, job_id)
    assert job["name"] == "refactor-auth"
    assert job["state"] == "queued"
    # naïve "--at 2026-07-18T02:00" local (UTC+1) is stored normalized to UTC,
    # in _iso_now's exact "...+00:00" representation.
    assert job["at_time"] == "2026-07-18T01:00:00+00:00"
    assert job["cwd"] == str(Path("~/code/myapp").expanduser())
    assert job["window_policy"] == "fresh"
    assert job["retry_max"] == 2
    assert job["notify"] == "email"
    assert job["body"] == "Do the thing.\n"
    assert job["submitted_at"] == NOW


def test_sub_from_stdin_defaults_name_to_stdin(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    text = "#$ --cwd /tmp/work\n\nHello from stdin.\n"

    result = run(runner, home, "sub", "-", input=text)

    assert result.exit_code == 0, result.output
    job_id = int(result.output.strip())

    conn = make_db(home)
    job = db.get_job(conn, job_id)
    assert job["name"] == "stdin"
    assert job["body"] == "Hello from stdin.\n"


def test_sub_default_name_is_filename_stem(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    job_file = tmp_path / "my-cool-job.md"
    job_file.write_text("#$ --cwd /tmp/work\n\nbody\n")

    result = run(runner, home, "sub", str(job_file))

    assert result.exit_code == 0, result.output
    job_id = int(result.output.strip())
    job = db.get_job(make_db(home), job_id)
    assert job["name"] == "my-cool-job"


def test_sub_now_bypasses_at(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    job_file = tmp_path / "j.md"
    job_file.write_text(BASIC_JOB)  # has "--at 2026-07-18T02:00"

    result = run(runner, home, "sub", "--now", str(job_file))

    assert result.exit_code == 0, result.output
    job_id = int(result.output.strip())
    job = db.get_job(make_db(home), job_id)
    assert job["at_time"] is None


def test_sub_hold_directive_submits_held(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    job_file = tmp_path / "j.md"
    job_file.write_text("#$ --cwd /tmp/work\n#$ --hold\n\nbody\n")

    result = run(runner, home, "sub", str(job_file))

    assert result.exit_code == 0, result.output
    job_id = int(result.output.strip())
    job = db.get_job(make_db(home), job_id)
    assert job["state"] == "held"


def test_sub_defaults_isolate_and_notify_from_config(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    job_file = tmp_path / "j.md"
    job_file.write_text("#$ --cwd /tmp/work\n\nbody\n")

    result = run(runner, home, "sub", str(job_file))
    job_id = int(result.output.strip())
    job = db.get_job(make_db(home), job_id)

    assert job["isolate"] == "worktree"  # DEFAULTS["safety"]["default_isolate"]
    assert job["notify"] == "email"  # DEFAULTS["notify"]["default"]


def test_sub_mirrors_model_from_passthrough(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    job_file = tmp_path / "j.md"
    job_file.write_text("#$ --cwd /tmp/work\n#$ --model claude-opus-4-8\n\nbody\n")

    result = run(runner, home, "sub", str(job_file))
    job_id = int(result.output.strip())
    job = db.get_job(make_db(home), job_id)

    assert job["model"] == "claude-opus-4-8"
    assert job["passthrough"] == ["--model", "claude-opus-4-8"]


# ---------------------------------------------------------------------------
# ccq sub: --at is normalized to UTC at store time (the naïve-local bug)
# ---------------------------------------------------------------------------


def test_sub_naive_at_stored_as_utc_isoformat(tmp_path: Path, runner: CliRunner, pin_tz) -> None:
    # A naïve local wall-clock "--at" must be stored in _iso_now's exact
    # "...+00:00" UTC representation, not verbatim -- so the scheduler's
    # lexicographic at_time <= now compare is a correct chronological one.
    home = tmp_path / "home"
    job_file = tmp_path / "j.md"
    job_file.write_text("#$ --cwd /tmp/work\n#$ --at 2026-07-23T09:54\n\nbody\n")

    pin_tz("Etc/GMT-1")  # UTC+1, matching the bug report's Europe/London BST
    result = run(runner, home, "sub", str(job_file))

    assert result.exit_code == 0, result.output
    job = db.get_job(make_db(home), int(result.output.strip()))
    at_time = job["at_time"]
    # Shares _iso_now's representation: UTC offset, and 09:54 local (UTC+1) = 08:54 UTC.
    assert at_time.endswith("+00:00")
    assert at_time == "2026-07-23T08:54:00+00:00"


def test_sub_at_with_offset_is_respected_not_double_attached(tmp_path: Path, runner: CliRunner, pin_tz) -> None:
    # An "--at" that already carries an offset is converted to UTC as-is; the
    # local zone must NOT be attached on top of it.
    home = tmp_path / "home"
    job_file = tmp_path / "j.md"
    job_file.write_text("#$ --cwd /tmp/work\n#$ --at 2026-07-23T09:54+02:00\n\nbody\n")

    pin_tz("Etc/GMT-1")  # different from the input's +02:00 on purpose
    result = run(runner, home, "sub", str(job_file))

    assert result.exit_code == 0, result.output
    job = db.get_job(make_db(home), int(result.output.strip()))
    assert job["at_time"] == "2026-07-23T07:54:00+00:00"  # 09:54+02:00 = 07:54 UTC


def test_tick_dispatches_job_whose_local_at_just_passed(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, pin_tz) -> None:
    # Regression for the reported symptom: on a UTC+1 machine a job whose --at
    # has just passed in local time never dispatched (naïve "09:54" sorted after
    # the UTC "08:54:55+00:00" now). With store-time normalization it promotes
    # and dispatches. NOW (via _fixed_clock) is 09:00Z == 10:00 local; --at is
    # 09:54 local == 08:54Z, just past.
    home = tmp_path / "home"
    job_file = tmp_path / "j.md"
    job_file.write_text("#$ --cwd /tmp/work\n#$ --at 2026-07-22T09:54\n\nbody\n")

    pin_tz("Etc/GMT-1")
    spawned: list[int] = []
    monkeypatch.setattr(cli, "_spawn_run", lambda jid: spawned.append(jid))

    sub_result = run(runner, home, "sub", str(job_file))
    assert sub_result.exit_code == 0, sub_result.output
    job_id = int(sub_result.output.strip())

    tick_result = run(runner, home, "tick")

    assert tick_result.exit_code == 0, tick_result.output
    assert spawned == [job_id]
    assert db.get_job(make_db(home), job_id)["state"] == "running"


def test_tick_leaves_job_whose_local_at_is_future_queued(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, pin_tz) -> None:
    # The mirror case: a still-future --at (11:00 local == 10:00Z > 09:00Z now)
    # must not promote or dispatch.
    home = tmp_path / "home"
    job_file = tmp_path / "j.md"
    job_file.write_text("#$ --cwd /tmp/work\n#$ --at 2026-07-22T11:00\n\nbody\n")

    pin_tz("Etc/GMT-1")
    spawned: list[int] = []
    monkeypatch.setattr(cli, "_spawn_run", lambda jid: spawned.append(jid))

    sub_result = run(runner, home, "sub", str(job_file))
    assert sub_result.exit_code == 0, sub_result.output
    job_id = int(sub_result.output.strip())

    tick_result = run(runner, home, "tick")

    assert tick_result.exit_code == 0, tick_result.output
    assert spawned == []
    assert db.get_job(make_db(home), job_id)["state"] == "queued"


# ---------------------------------------------------------------------------
# ccq stat
# ---------------------------------------------------------------------------


def test_stat_lists_non_terminal_jobs(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    add_job(conn, name="queued-one", state="queued")
    add_job(conn, name="running-one", state="running", started_at=PAST)
    add_job(conn, name="done-one", state="done", finished_at=PAST)

    result = run(runner, home, "stat")

    assert result.exit_code == 0, result.output
    assert "queued-one" in result.output
    assert "running-one" in result.output
    assert "done-one" not in result.output
    assert "window: closed" in result.output
    assert "30d sessions: 0" in result.output


def test_stat_all_includes_recent_terminal_but_not_old(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    add_job(conn, name="recent-done", state="done", finished_at=PAST)
    add_job(conn, name="old-done", state="done", finished_at=OLD)

    result = run(runner, home, "stat", "-a")

    assert result.exit_code == 0, result.output
    assert "recent-done" in result.output
    assert "old-done" not in result.output


def test_stat_without_all_excludes_terminal_entirely(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    add_job(conn, name="recent-done", state="done", finished_at=PAST)

    result = run(runner, home, "stat")

    assert "recent-done" not in result.output


# ---------------------------------------------------------------------------
# ccq show
# ---------------------------------------------------------------------------


def test_show_reports_directives_argv_and_no_delta(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="showme", state="queued")

    result = run(runner, home, "show", str(job_id))

    assert result.exit_code == 0, result.output
    assert f"id: {job_id}" in result.output
    assert "name: showme" in result.output
    assert "state: queued" in result.output
    assert "delta%: n/a" in result.output
    assert "argv:" in result.output
    assert "--model claude-opus-4-8" in result.output


def test_show_computes_delta_from_last_run(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="showme", state="done", finished_at=PAST)
    db.insert_run(conn, job_id=job_id, started_at=PAST, finished_at=NOW, pct_before=10, pct_after=25)

    result = run(runner, home, "show", str(job_id))

    assert result.exit_code == 0, result.output
    assert "delta%: 15" in result.output


def test_show_unknown_id_errors(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    make_db(home)

    result = run(runner, home, "show", "999")

    assert result.exit_code != 0
    assert "no job" in result.output.lower()


# ---------------------------------------------------------------------------
# ccq log
# ---------------------------------------------------------------------------


def test_log_prints_existing_transcript(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="logme", state="done", finished_at=PAST)

    from ccq import runner as runner_mod

    dir_ = runner_mod.job_dir(home / ".ccq", job_id, "logme")
    dir_.mkdir(parents=True)
    (dir_ / "stream.jsonl").write_text('{"type": "system"}\n')

    result = run(runner, home, "log", str(job_id))

    assert result.exit_code == 0, result.output
    assert '{"type": "system"}' in result.output


def test_log_follow_drains_new_content_and_stops_when_job_finishes(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="followme", state="running", started_at=PAST, pid=999999)

    from ccq import runner as runner_mod

    dir_ = runner_mod.job_dir(home / ".ccq", job_id, "followme")
    dir_.mkdir(parents=True)
    stream_path = dir_ / "stream.jsonl"
    stream_path.write_text('{"line": 1}\n')

    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        # Simulate the job finishing mid-follow: more output appears, and
        # the job flips to a terminal state, so the follow loop must stop.
        with stream_path.open("a") as f:
            f.write('{"line": 2}\n')
        db.update_job(conn, job_id, state="done", finished_at=NOW)

    monkeypatch.setattr(cli, "_sleep", fake_sleep)

    result = run(runner, home, "log", str(job_id), "-f")

    assert result.exit_code == 0, result.output
    assert '{"line": 1}' in result.output
    assert '{"line": 2}' in result.output
    assert len(calls) == 1  # loop stopped as soon as the job went terminal


def test_log_unknown_id_errors(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    make_db(home)

    result = run(runner, home, "log", "999")

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ccq del
# ---------------------------------------------------------------------------


def test_del_cancels_queued_job(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="cancelme", state="queued")

    result = run(runner, home, "del", str(job_id))

    assert result.exit_code == 0, result.output
    job = db.get_job(conn, job_id)
    assert job["state"] == "cancelled"
    assert job["finished_at"] == NOW


def test_del_sigterms_process_group_when_running(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="killme", state="running", started_at=PAST, pid=424242)

    terminated: list[int] = []
    monkeypatch.setattr(cli, "_terminate", lambda pid: terminated.append(pid))

    result = run(runner, home, "del", str(job_id))

    assert result.exit_code == 0, result.output
    assert terminated == [424242]
    job = db.get_job(conn, job_id)
    assert job["state"] == "cancelled"


def test_del_already_terminal_is_a_noop(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="donejob", state="done", finished_at=PAST)

    result = run(runner, home, "del", str(job_id))

    assert result.exit_code == 0, result.output
    assert "already done" in result.output
    job = db.get_job(conn, job_id)
    assert job["finished_at"] == PAST  # untouched


# ---------------------------------------------------------------------------
# ccq hold / ccq release
# ---------------------------------------------------------------------------


def test_hold_then_release_round_trips(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="holdme", state="queued")

    hold_result = run(runner, home, "hold", str(job_id))
    assert hold_result.exit_code == 0, hold_result.output
    assert db.get_job(conn, job_id)["state"] == "held"

    release_result = run(runner, home, "release", str(job_id))
    assert release_result.exit_code == 0, release_result.output
    assert db.get_job(conn, job_id)["state"] == "queued"


def test_hold_running_job_errors(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="running-one", state="running", started_at=PAST, pid=1)

    result = run(runner, home, "hold", str(job_id))

    assert result.exit_code != 0
    assert db.get_job(conn, job_id)["state"] == "running"


def test_release_non_held_job_errors(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="queued-one", state="queued")

    result = run(runner, home, "release", str(job_id))

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ccq resume
# ---------------------------------------------------------------------------


def test_resume_execs_claude_resume_in_job_cwd(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(
        conn,
        name="resumeme",
        state="done",
        finished_at=PAST,
        cwd="/tmp/somewhere",
        conversation_id="conv-abc123",
    )

    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(cli, "_exec", lambda argv, cwd: calls.append((argv, cwd)))

    result = run(runner, home, "resume", str(job_id))

    assert result.exit_code == 0, result.output
    assert calls == [(["claude", "--resume", "conv-abc123"], "/tmp/somewhere")]


def test_resume_without_conversation_id_errors(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="noresume", state="failed", finished_at=PAST)

    result = run(runner, home, "resume", str(job_id))

    assert result.exit_code != 0
    assert "no conversation" in result.output.lower()


# ---------------------------------------------------------------------------
# ccq tick
# ---------------------------------------------------------------------------


def test_tick_dispatches_due_job_via_injected_spawn(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="ticked", state="queued", at_time=None)

    spawned: list[int] = []
    monkeypatch.setattr(cli, "_spawn_run", lambda jid: spawned.append(jid))

    result = run(runner, home, "tick")

    assert result.exit_code == 0, result.output
    assert spawned == [job_id]
    assert db.get_job(conn, job_id)["state"] == "running"
    assert "dispatched=1" in result.output


def test_tick_leaves_not_yet_due_job_alone(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="notyet", state="queued", at_time=FUTURE)

    spawned: list[int] = []
    monkeypatch.setattr(cli, "_spawn_run", lambda jid: spawned.append(jid))

    result = run(runner, home, "tick")

    assert result.exit_code == 0, result.output
    assert spawned == []
    assert db.get_job(conn, job_id)["state"] == "queued"


def test_tick_quiet_suppresses_summary_but_still_dispatches(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    # -q is for cron, which mails any output: the tick must do its work but
    # print nothing, whereas the bare `ccq tick` still prints the summary.
    home = tmp_path / "home"
    conn = make_db(home)
    job_id = add_job(conn, name="quietly", state="queued", at_time=None)

    spawned: list[int] = []
    monkeypatch.setattr(cli, "_spawn_run", lambda jid: spawned.append(jid))

    result = run(runner, home, "tick", "-q")

    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert spawned == [job_id]
    assert db.get_job(conn, job_id)["state"] == "running"


def test_tick_without_quiet_prints_summary(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_db(home)
    monkeypatch.setattr(cli, "_spawn_run", lambda jid: None)

    result = run(runner, home, "tick")

    assert result.exit_code == 0, result.output
    assert "reaped=0 expired=0 promoted=0 dispatched=0" in result.output


def test_tick_default_spawn_run_is_schedulers_default(tmp_path: Path) -> None:
    # Guards the seam wiring itself: cli._spawn_run must be the same
    # production default scheduler.tick would otherwise fall back to, so
    # `ccq tick` really does chain-dispatch `ccq run <id>` when nothing
    # has monkeypatched it away.
    assert cli._spawn_run is scheduler._default_spawn_run


# ---------------------------------------------------------------------------
# unknown / ambiguous identifiers, generically
# ---------------------------------------------------------------------------


def test_ambiguous_name_errors(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    conn = make_db(home)
    add_job(conn, name="dup", state="queued")
    add_job(conn, name="dup", state="queued")

    result = run(runner, home, "hold", "dup")

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ccq run
# ---------------------------------------------------------------------------


def test_run_executes_job_to_terminal_via_fake_claude(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    conn = make_db(home)
    job_id = add_job(conn, name="runme", state="running", started_at=PAST, cwd=str(work))

    # Fake `claude` via the module-level _claude_cmd seam -- no real binary.
    claude_cmd = _fake_claude_cmd(tmp_path)
    monkeypatch.setattr(cli, "_claude_cmd", claude_cmd)

    # Drive the real runner.run_job through the _run_job seam, but neutralise
    # its detached on-complete chain so no real `ccq tick` process is spawned,
    # and capture the wiring `ccq run` handed it.
    captured: dict[str, Any] = {}

    def fake_run_job(database, jid, *, root, on_complete, claude_cmd):
        captured["job_id"] = jid
        captured["root"] = root
        captured["on_complete"] = list(on_complete)
        captured["claude_cmd"] = list(claude_cmd)
        return runner_mod.run_job(
            database,
            jid,
            root=root,
            on_complete=[sys.executable, "-c", "pass"],
            claude_cmd=claude_cmd,
            sleep=_noop_sleep,
        )

    monkeypatch.setattr(cli, "_run_job", fake_run_job)

    result = run(runner, home, "run", str(job_id))

    assert result.exit_code == 0, result.output

    # `ccq run` wired run_job to the right job, root, chain, and claude seam.
    assert captured["job_id"] == job_id
    assert captured["root"] == home / ".ccq"
    assert captured["on_complete"] == list(scheduler.TICK_ARGV)
    assert captured["claude_cmd"] == claude_cmd

    # Job reached a terminal state and captured the conversation id (§7).
    job = db.get_job(conn, job_id)
    assert job["state"] == "done"
    assert job["state"] in db.TERMINAL_STATES
    assert job["conversation_id"] == "fake-session-123"

    # Expected artifacts were produced under <home>/.ccq.
    dir_ = runner_mod.job_dir(home / ".ccq", job_id, "runme")
    assert (dir_ / "stream.jsonl").exists()
    result_json = json.loads((dir_ / "result.json").read_text())
    assert result_json["session_id"] == "fake-session-123"


def test_run_unknown_id_errors(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "home"
    make_db(home)

    result = run(runner, home, "run", "999")

    assert result.exit_code != 0
    assert "no job" in result.output.lower()


def test_run_default_seam_is_runners_run_job(tmp_path: Path) -> None:
    # Guards the seam wiring: cli._run_job must be runner.run_job so an
    # un-monkeypatched `ccq run` really executes the job via the runner.
    assert cli._run_job is runner_mod.run_job


# ---------------------------------------------------------------------------
# ~/.ccq is created for a fresh install (both --home and the default home)
# ---------------------------------------------------------------------------


def test_missing_home_option_dir_is_created(tmp_path: Path, runner: CliRunner) -> None:
    home = tmp_path / "does-not-exist-yet"
    assert not home.exists()

    result = run(runner, home, "stat")

    assert result.exit_code == 0, result.output
    assert (home / ".ccq").is_dir()
    assert (home / ".ccq" / "ccq.db").exists()


def test_default_home_ccq_dir_is_created(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "fakehome"
    assert not fake_home.exists()
    monkeypatch.setenv("HOME", str(fake_home))

    # No --home: resolves against the (patched) default Path.home().
    result = runner.invoke(cli.main, ["stat"])

    assert result.exit_code == 0, result.output
    assert (fake_home / ".ccq").is_dir()
    assert (fake_home / ".ccq" / "ccq.db").exists()
