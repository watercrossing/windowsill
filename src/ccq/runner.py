"""ccq run execution: invocation, detached spawn, reap, timeout.

Implements design/03-execution-model.md §3.2 (the invocation), §3.3 (safety for unattended
runs), and the parts of §6.2 (the job-directory filesystem layout) that a
run touches: ``cmd.json``, ``stream.jsonl``, ``result.json``, ``resume.sh``.
(``job.md`` is the verbatim submission and is written at submit time, not
here.)

Every seam a caller might need to fake is a keyword argument with a real
default:

- ``claude_cmd`` -- the argv prefix that gets exec'd. Production passes
  ``("claude",)``; tests substitute a fake local script that writes a
  canned ``stream-json`` transcript and exits, so no real ``claude`` ever
  runs.
- ``popen`` -- defaults to :class:`subprocess.Popen`; tests can substitute
  a spy/fake to assert on how a process would have been spawned without
  spawning one.
- ``clock`` / ``sleep`` -- default to :func:`time.monotonic` /
  :func:`time.sleep`; tests substitute deterministic fakes so timeout
  enforcement is tested without depending on real wall-clock time.
- ``now`` -- default produces a real UTC ISO-8601 timestamp; tests pass a
  fixed/deterministic callable.
- ``root`` -- the job-directory root (normally ``~/.ccq``); always passed
  explicitly by callers, so tests point it at a ``tmp_path``.
- ``on_complete`` -- the argv of the next detached process to spawn once a
  run finishes (chain dispatch, wired up by a later module). This module
  does not know or care what it is; it just ``Popen``s it and exits,
  never awaiting it in-process, so a fast-failing job can't fork-bomb.

Runs are spawned in their own process group (``start_new_session=True``,
i.e. ``setsid``) so §3.3's "SIGTERM the process group, then SIGKILL after
the grace period" can target the whole tree with :func:`os.killpg`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from ccq import db, transcript

# ---------------------------------------------------------------------------
# job directory layout (§6.2)
# ---------------------------------------------------------------------------


def job_dir(root: Path, job_id: int, name: str) -> Path:
    """``<root>/jobs/<5-digit zero-padded id>-<name>`` (§6.2: "so ls sorts").

    Pure path arithmetic -- does not create the directory.
    """
    return root / "jobs" / f"{job_id:05d}-{name}"


def write_cmd_json(dir_: Path, *, argv: Sequence[str], cwd: str, env: dict[str, str] | None) -> Path:
    """Write ``cmd.json``: the resolved argv + env + cwd, for reproducibility."""
    path = dir_ / "cmd.json"
    path.write_text(json.dumps({"argv": list(argv), "cwd": cwd, "env": env or {}}, indent=2) + "\n")
    return path


def write_result_json(dir_: Path, result_event: dict[str, Any]) -> Path:
    """Write ``result.json``: the final ``stream-json`` result event."""
    path = dir_ / "result.json"
    path.write_text(json.dumps(result_event, indent=2) + "\n")
    return path


def write_resume_sh(dir_: Path, *, cwd: str, claude_cmd: Sequence[str], conversation_id: str) -> Path:
    """Write ``resume.sh``: ``cd <cwd> && claude --resume <id>`` (§6.2)."""
    path = dir_ / "resume.sh"
    cmd = " ".join(shlex.quote(part) for part in claude_cmd)
    path.write_text(f"cd {shlex.quote(cwd)} && {cmd} --resume {shlex.quote(conversation_id)}\n")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# §3.2 the invocation
# ---------------------------------------------------------------------------


def build_argv(
    passthrough: Sequence[str],
    *,
    resume: str | None = None,
    claude_cmd: Sequence[str] = ("claude",),
) -> list[str]:
    """The argv for §3.2's invocation, minus ``cd <cwd> &&`` (that's the
    subprocess ``cwd=`` kwarg) and minus the body (goes on stdin, never
    argv -- see :func:`spawn`).

    ``claude_cmd`` is the injectable command seam: production passes
    ``("claude",)``; tests substitute a fake executable/argv that writes a
    canned stream + result and exits.
    """
    argv = list(claude_cmd) + ["-p", "--output-format", "stream-json", "--verbose"]
    if resume:
        argv += ["--resume", resume]
    argv += list(passthrough)
    return argv


# ---------------------------------------------------------------------------
# timeout string -> seconds (§3.3 "--timeout")
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$")
_DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 1}


def parse_timeout_seconds(value: str | None) -> float | None:
    """Parse a duration like ``"4h"``, ``"30m"``, ``"90s"`` into seconds.

    ``None`` (no ``--timeout`` recorded on the job) means "do not enforce
    a timeout at all", not "zero".
    """
    if not value:
        return None
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ValueError(f"unparseable timeout: {value!r}")
    amount, unit = match.groups()
    return float(amount) * _DURATION_UNIT_SECONDS[unit]


# ---------------------------------------------------------------------------
# pid liveness + process-group signalling
# ---------------------------------------------------------------------------


def is_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process, via ``kill(pid, 0)``.

    This is how a *different* ccq invocation than the one that spawned the
    run (e.g. a later ``ccq tick``) checks on it -- it isn't the parent in
    the ``wait()`` sense, so signal-0 liveness is the only portable check.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# detached spawn (§3.3: own process group, output to the job dir)
# ---------------------------------------------------------------------------


def spawn(
    argv: Sequence[str],
    body: str,
    *,
    cwd: str,
    dir_: Path,
    env: dict[str, str] | None = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> subprocess.Popen:
    """Spawn ``argv`` detached in its own process group.

    The body goes on stdin via a file in ``dir_`` (never argv -- §3.2:
    bodies exceed ``ARG_MAX`` and contain everything a shell hates).
    stdout is redirected straight to ``dir_/stream.jsonl`` so a killed run
    still leaves a usable partial transcript (§3.2). stderr is discarded
    rather than interleaved into the same file, which would corrupt the
    one-json-object-per-line stream format.

    ``popen`` is the injectable spawn seam (defaults to
    :class:`subprocess.Popen`); ``argv`` (via :func:`build_argv`'s
    ``claude_cmd``) is the injectable command seam.
    """
    stdin_path = dir_ / ".stdin"
    stdin_path.write_text(body)
    stdout_path = dir_ / "stream.jsonl"
    with stdin_path.open("rb") as stdin_f, stdout_path.open("wb") as stdout_f:
        process = popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdin=stdin_f,
            stdout=stdout_f,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return process


# ---------------------------------------------------------------------------
# timeout enforcement (§3.3: SIGTERM the process group, SIGKILL after grace)
# ---------------------------------------------------------------------------


def wait_with_timeout(
    process: Any,
    *,
    timeout_seconds: float | None,
    grace_seconds: float = 30.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.05,
    terminate_group: Callable[[int], None] = _terminate_process_group,
    kill_group: Callable[[int], None] = _kill_process_group,
) -> bool:
    """Block until ``process`` exits, enforcing ``timeout_seconds``.

    On timeout, SIGTERM's the process group and waits up to
    ``grace_seconds`` more, then SIGKILL's it (§3.3). Returns whether a
    timeout was enforced. ``process`` only needs ``.pid`` and a
    ``.poll() -> int | None`` method (a real :class:`subprocess.Popen`
    satisfies this; so does a bare test fake).

    ``clock``/``sleep`` are the injectable time seam -- tests drive this
    deterministically with fakes rather than real wall-clock waits.
    """
    if timeout_seconds is None:
        while process.poll() is None:
            sleep(poll_interval)
        return False

    deadline = clock() + timeout_seconds
    while process.poll() is None and clock() < deadline:
        sleep(poll_interval)

    if process.poll() is not None:
        return False  # exited on its own before the deadline

    terminate_group(process.pid)
    grace_deadline = clock() + grace_seconds
    while process.poll() is None and clock() < grace_deadline:
        sleep(poll_interval)

    if process.poll() is None:
        kill_group(process.pid)
        while process.poll() is None:
            sleep(poll_interval)

    return True


# ---------------------------------------------------------------------------
# result derivation (§3.2: "derive the result from the last event")
# ---------------------------------------------------------------------------


def parse_result_event(stream_path: Path) -> dict[str, Any] | None:
    """Read ``stream.jsonl`` and return its last event, iff it's a
    ``"result"`` event. ``None`` for a missing/empty file, a malformed
    last line, or a stream that ends mid-run (killed before a result
    event was ever emitted).
    """
    if not stream_path.exists():
        return None

    last_line: str | None = None
    with stream_path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                last_line = stripped

    if last_line is None:
        return None

    try:
        event = json.loads(last_line)
    except json.JSONDecodeError:
        return None

    if not isinstance(event, dict) or event.get("type") != "result":
        return None
    return event


def outcome_from_result(result_event: dict[str, Any] | None, *, timed_out: bool) -> str:
    """Derive a ``runs.outcome`` value from a parsed result event.

    ``timed_out`` (our own §3.3 enforcement fired) always wins. Otherwise:
    no result event at all means the process died without ever emitting
    one (crashed, OOM-killed, etc.) -- ``"killed"``. A result event's
    ``is_error`` flag (§3.2) decides ``"done"`` vs. ``"failed"``.
    ``rate_limited`` is not produced here -- that's a later milestone
    (§4/§5 rate-limit handling), deliberately out of scope for this module.
    """
    if timed_out:
        return "timeout"
    if result_event is None:
        return "killed"
    return "failed" if result_event.get("is_error") else "done"


# ---------------------------------------------------------------------------
# reap: the lightweight pid-liveness + result check (§3.1 step 2, "ccq tick")
# ---------------------------------------------------------------------------


@dataclass
class ReapStatus:
    alive: bool
    outcome: str | None
    result: dict[str, Any] | None


def reap(
    dir_: Path,
    pid: int,
    *,
    alive_check: Callable[[int], bool] = is_alive,
) -> ReapStatus:
    """Check a run's pid liveness and, if it's gone, its result.

    This is the resilience path a later scheduler module uses per-tick:
    it doesn't (can't) ``wait()`` on a pid that isn't its own child, so it
    checks liveness and, on exit, materialises/derives ``result.json`` the
    same way :func:`run_job` does -- covering the case where the detached
    run wrapper itself died uncleanly before finishing that step.

    ``result.json`` is trusted verbatim if already present; otherwise it's
    derived from ``stream.jsonl``'s last event and written out.
    """
    if alive_check(pid):
        return ReapStatus(alive=True, outcome=None, result=None)

    result_path = dir_ / "result.json"
    if result_path.exists():
        result_event: dict[str, Any] | None = json.loads(result_path.read_text())
    else:
        result_event = parse_result_event(dir_ / "stream.jsonl")
        if result_event is not None:
            write_result_json(dir_, result_event)

    outcome = outcome_from_result(result_event, timed_out=False)
    return ReapStatus(alive=False, outcome=outcome, result=result_event)


# ---------------------------------------------------------------------------
# run_job: the detached run wrapper (§3.1, §3.2, §3.3, §6.2, end to end)
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# job.state on completion. Run outcomes (§6 runs.outcome) are a different
# vocabulary from job states (§7): "timeout" and "killed" are run outcomes,
# never job states, so both map to the terminal job state "failed" -- and a
# kill -9 mid-run must leave a *failed* job (§11 acceptance #10), not a
# "killed" one.
_JOB_STATE_FOR_OUTCOME = {
    "done": "done",
    "failed": "failed",
    "timeout": "failed",
    "killed": "failed",
}


@dataclass
class RunOutcome:
    job_id: int
    run_id: int
    pid: int
    exit_code: int | None
    outcome: str
    result: dict[str, Any] | None


def run_job(
    database: "db.sqlite_utils.Database",
    job_id: int,
    *,
    root: Path,
    on_complete: Sequence[str],
    claude_cmd: Sequence[str] = ("claude",),
    env: dict[str, str] | None = None,
    grace_seconds: float = 30.0,
    now: Callable[[], str] = _iso_now,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.05,
) -> RunOutcome:
    """Run one already-selected job to completion: build its invocation,
    spawn it detached under ``root``'s job directory, block until it
    exits (this wrapper is the child's real parent, so a genuine
    ``wait()``-style block is correct here -- unlike ``ccq tick``, which
    never blocks on a running job per §3.1), enforce its ``--timeout``,
    persist the outcome to ``runs``/``jobs``/``events``, and finally hand
    off to ``on_complete`` detached (Popen-and-exit, §3.1: "never
    recursive in-process, or a fast-failing job becomes a fork bomb").

    Every non-DB dependency (which command to exec, how to spawn it, the
    clock, the on-complete command) is an injectable keyword argument;
    ``database`` is a plain :mod:`ccq.db`-opened handle so tests point it
    at a ``tmp_path`` DB, never the real ``~/.ccq/ccq.db``.
    """
    job = db.get_job(database, job_id)
    if job is None:
        raise ValueError(f"no job with id {job_id}")

    dir_ = job_dir(root, job_id, job["name"])
    dir_.mkdir(parents=True, exist_ok=True)

    argv = build_argv(job["passthrough"], resume=job.get("resume_id"), claude_cmd=claude_cmd)
    write_cmd_json(dir_, argv=argv, cwd=job["cwd"], env=env)

    started_at = now()
    run_id = db.insert_run(database, job_id=job_id, started_at=started_at)
    db.log_event(database, ts=started_at, job_id=job_id, kind="dispatched", detail=f"run_id={run_id}")

    process = spawn(argv, job["body"], cwd=job["cwd"], dir_=dir_, env=env, popen=popen)
    db.update_job(database, job_id, state="running", started_at=started_at, pid=process.pid)

    timeout_seconds = parse_timeout_seconds(job.get("timeout"))
    timed_out = wait_with_timeout(
        process,
        timeout_seconds=timeout_seconds,
        grace_seconds=grace_seconds,
        clock=clock,
        sleep=sleep,
        poll_interval=poll_interval,
    )

    result_event = parse_result_event(dir_ / "stream.jsonl")
    if result_event is not None:
        write_result_json(dir_, result_event)

    # Render the human-readable transcript (§6.2) from whatever the run left
    # behind -- even a killed/partial stream is worth reading back.
    transcript.write_transcript(dir_, prompt=job["body"])

    outcome = outcome_from_result(result_event, timed_out=timed_out)
    finished_at = now()
    conversation_id = result_event.get("session_id") if result_event else None

    db.update_run(
        database,
        run_id,
        finished_at=finished_at,
        outcome=outcome,
        num_turns=(result_event.get("num_turns") if result_event else None),
        duration_ms=(result_event.get("duration_ms") if result_event else None),
        conversation_id=conversation_id,
    )

    job_state = _JOB_STATE_FOR_OUTCOME.get(outcome, "failed")
    db.update_job(
        database,
        job_id,
        state=job_state,
        finished_at=finished_at,
        exit_code=process.returncode,
        conversation_id=conversation_id,
    )
    db.log_event(database, ts=finished_at, job_id=job_id, kind=job_state, detail=outcome)

    if conversation_id:
        write_resume_sh(dir_, cwd=job["cwd"], claude_cmd=claude_cmd, conversation_id=conversation_id)

    # Chain dispatch (§3.1): hand off to the next tick detached, never a
    # recursive in-process call, regardless of this run's outcome.
    popen(list(on_complete), start_new_session=True)

    return RunOutcome(
        job_id=job_id,
        run_id=run_id,
        pid=process.pid,
        exit_code=process.returncode,
        outcome=outcome,
        result=result_event,
    )
