"""Tests for windowsill.scheduler -- the `windowsill tick` dispatch step and
the job-lifecycle state machine.

These tests spawn NO real process and read NO real clock: `tick`'s `now`,
`spawn_run`, and `alive_check` are all injectable, so every "detached run"
is a recording fake and every timestamp is a fixed string. The DB is always
opened at a tmp_path, never ~/.windowsill. Concurrent-tick serialisation is
verified against the *real* `db.dispatch_lock` (a BEGIN IMMEDIATE).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import sqlite_utils

from windowsill import db, runner, scheduler, usage

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

NOW = "2026-07-21T12:00:00Z"
PAST = "2026-07-21T02:00:00Z"
FUTURE = "2026-07-21T22:00:00Z"


def make_db(tmp_path: Path, name: str = "windowsill.db") -> sqlite_utils.Database:
    return db.connect(tmp_path / name)


def add_job(
    conn: sqlite_utils.Database,
    *,
    name: str = "job",
    state: str = "queued",
    at_time: str | None = None,
    before_time: str | None = None,
    pid: int | None = None,
    cwd: str = "/tmp",
    window_policy: str = "share",
    is_anchor: bool = False,
    priority: int = 0,
    hold_jid: list[str] | None = None,
    resume_from: str | None = None,
    resume_id: str | None = None,
    conversation_id: str | None = None,
    warm: bool = False,
    warm_lead: str | None = None,
    warmed_by: int | None = None,
) -> int:
    return db.insert_job(
        conn,
        name=name,
        state=state,
        submitted_at=PAST,
        cwd=cwd,
        body="do the thing",
        passthrough=[],
        at_time=at_time,
        before_time=before_time,
        pid=pid,
        window_policy=window_policy,
        is_anchor=is_anchor,
        priority=priority,
        hold_jid=hold_jid,
        resume_from=resume_from,
        resume_id=resume_id,
        conversation_id=conversation_id,
        warm=warm,
        warm_lead=warm_lead,
        warmed_by=warmed_by,
    )


class ProbeSpy:
    """A `/usage` oracle that answers from a canned reading and counts calls.

    A probe only happens when a dispatch decision depends on window state
    (design/decisions/probes-are-free.md), so "how many times was this
    called" is itself the assertion in several tests below. No `claude` is
    ever invoked.
    """

    def __init__(self, *, open_until: str | None, pct_used: int | None = None) -> None:
        self.reset_at = datetime.fromisoformat(open_until) if open_until else None
        # scheduler.warm_gate cares how *full* an open window is, not just
        # that it is open, so the percentage is settable. Left alone it
        # keeps the reading the rest of the suite was written against.
        self.pct_used = (15 if self.reset_at else 0) if pct_used is None else pct_used
        self.calls = 0

    def __call__(self) -> usage.ParsedUsage:
        self.calls += 1
        return usage.ParsedUsage(pct_used=self.pct_used, reset_at=self.reset_at)


def exploding_probe() -> usage.ParsedUsage:
    """A probe that must never be called (design/decisions/probes-are-free.md: don't probe with no question)."""
    raise AssertionError("tick probed /usage when no decision depended on window state")


class SpawnSpy:
    """Records the job ids handed to `spawn_run`, never spawns anything."""

    def __init__(self) -> None:
        self.ids: list[int] = []

    def __call__(self, job_id: int) -> None:
        self.ids.append(job_id)


def cfg(max_parallel: int) -> dict[str, Any]:
    return {"queue": {"max_parallel": max_parallel}}


# ---------------------------------------------------------------------------
# queued -> ready -> running (dispatch) when due
# ---------------------------------------------------------------------------


def test_tick_dispatches_a_due_queued_job(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, at_time=PAST)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.dispatched == [job_id]
    assert spawn.ids == [job_id]
    assert db.get_job(conn, job_id)["state"] == "running"


def test_tick_dispatches_job_with_no_at_time_immediately(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, at_time=None)
    spawn = SpawnSpy()

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert spawn.ids == [job_id]


def test_tick_leaves_not_yet_due_job_queued(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, at_time=FUTURE)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.dispatched == []
    assert result.promoted == []
    assert spawn.ids == []
    assert db.get_job(conn, job_id)["state"] == "queued"


def test_tick_promotes_but_does_not_dispatch_when_slot_taken(tmp_path: Path) -> None:
    """With max_parallel=1 and one job already running, a due job is promoted
    queued->ready but not dispatched (the slot is taken)."""
    conn = make_db(tmp_path)
    add_job(conn, name="running-one", state="running", pid=999)
    waiting = add_job(conn, name="waiting", at_time=PAST)
    spawn = SpawnSpy()

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=spawn,
        alive_check=lambda pid: True,
    )

    assert waiting in result.promoted
    assert result.dispatched == []
    assert spawn.ids == []
    assert db.get_job(conn, waiting)["state"] == "ready"


# ---------------------------------------------------------------------------
# held state is respected (toggled by the CLI, never by the scheduler)
# ---------------------------------------------------------------------------


def test_tick_never_dispatches_a_held_job(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, state="held", at_time=PAST)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.dispatched == []
    assert result.promoted == []
    assert spawn.ids == []
    assert db.get_job(conn, job_id)["state"] == "held"


def test_tick_dispatches_a_released_ready_job(tmp_path: Path) -> None:
    """The CLI releases held->ready; the scheduler dispatches ready directly."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, state="ready", at_time=PAST)
    spawn = SpawnSpy()

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert spawn.ids == [job_id]
    assert db.get_job(conn, job_id)["state"] == "running"


# ---------------------------------------------------------------------------
# max_parallel serialisation
# ---------------------------------------------------------------------------


def test_tick_dispatches_only_up_to_max_parallel(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    a = add_job(conn, name="a", state="ready")
    b = add_job(conn, name="b", state="ready")
    c = add_job(conn, name="c", state="ready")
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(2), now=NOW, spawn_run=spawn)

    assert result.dispatched == [a, b]
    assert spawn.ids == [a, b]
    assert db.get_job(conn, c)["state"] == "ready"


def test_tick_counts_alive_running_jobs_toward_parallel(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    add_job(conn, name="already", state="running", pid=4321)
    ready = add_job(conn, name="ready", state="ready")
    spawn = SpawnSpy()

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=spawn,
        alive_check=lambda pid: True,
    )

    assert result.dispatched == []
    assert db.get_job(conn, ready)["state"] == "ready"


def test_tick_defaults_max_parallel_to_one_without_config(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    a = add_job(conn, name="a", state="ready")
    add_job(conn, name="b", state="ready")
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, now=NOW, spawn_run=spawn)

    assert result.dispatched == [a]


# ---------------------------------------------------------------------------
# blocked_until gate (design/decisions/blocked-until-is-compared-not-cleared.md)
# ---------------------------------------------------------------------------


def test_tick_does_not_dispatch_while_blocked(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, at_time=PAST)
    db.update_sched(conn, blocked_until=FUTURE, blocked_reason="rate limit")
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.dispatched == []
    assert result.promoted == []
    assert spawn.ids == []
    assert db.get_job(conn, job_id)["state"] == "queued"


def test_tick_dispatches_once_block_has_expired(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, at_time=PAST)
    db.update_sched(conn, blocked_until=PAST, blocked_reason="rate limit")
    spawn = SpawnSpy()

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert spawn.ids == [job_id]


# ---------------------------------------------------------------------------
# before_time -> expired ("--before passed => expired")
# ---------------------------------------------------------------------------


def test_tick_expires_queued_job_past_before_time(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, at_time=PAST, before_time=PAST)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.expired == [job_id]
    assert result.dispatched == []
    assert spawn.ids == []
    job = db.get_job(conn, job_id)
    assert job["state"] == "expired"
    assert job["state"] in db.TERMINAL_STATES
    assert job["finished_at"] == NOW


def test_tick_expires_ready_job_past_before_time(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, state="ready", before_time=PAST)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.expired == [job_id]
    assert db.get_job(conn, job_id)["state"] == "expired"


def test_tick_does_not_expire_before_time_still_in_future(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, at_time=PAST, before_time=FUTURE)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.expired == []
    assert spawn.ids == [job_id]  # due and still in-window => dispatched


def test_tick_logs_an_expired_event(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, before_time=PAST)

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy())

    kinds = [e["kind"] for e in db.list_events(conn, job_id=job_id)]
    assert "expired" in kinds


# ---------------------------------------------------------------------------
# reap: the safety net for a running-state job whose process died unrecorded
# ---------------------------------------------------------------------------


def _job_dir_with_stream(root: Path, job_id: int, name: str, stream: str | None) -> None:
    d = runner.job_dir(root, job_id, name)
    d.mkdir(parents=True, exist_ok=True)
    if stream is not None:
        (d / "stream.jsonl").write_text(stream)


def test_tick_reaps_dead_running_job_with_no_result_to_failed(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="crashed", state="running", pid=123)
    # no stream.jsonl -> reap derives "killed" -> job state "failed"
    _job_dir_with_stream(tmp_path, job_id, "crashed", None)
    spawn = SpawnSpy()

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=spawn,
        alive_check=lambda pid: False,
    )

    assert result.reaped == [job_id]
    job = db.get_job(conn, job_id)
    assert job["state"] == "failed"
    assert job["state"] in db.TERMINAL_STATES
    assert job["finished_at"] == NOW


def test_tick_reaps_dead_running_job_with_result_to_done(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="finished", state="running", pid=321)
    stream = (
        json.dumps({"type": "system", "subtype": "init"}) + "\n" + json.dumps({"type": "result", "is_error": False, "session_id": "sess-xyz"}) + "\n"
    )
    _job_dir_with_stream(tmp_path, job_id, "finished", stream)

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=SpawnSpy(),
        alive_check=lambda pid: False,
    )

    assert result.reaped == [job_id]
    job = db.get_job(conn, job_id)
    assert job["state"] == "done"
    assert job["conversation_id"] == "sess-xyz"
    # result.json materialised by the reap safety net
    assert (runner.job_dir(tmp_path, job_id, "finished") / "result.json").exists()


def test_tick_reaps_a_run_with_live_background_work_to_blocked(tmp_path: Path) -> None:
    """The reap path and run_job must agree about what an outcome leaves
    behind -- both read runner.JOB_STATE_FOR_OUTCOME, so "incomplete" is
    "blocked" here too (design/decisions/blocked-not-failed.md)."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="unfinished", state="running", pid=321)
    stream = (
        "\n".join(
            json.dumps(event)
            for event in [
                {"type": "system", "subtype": "init"},
                {"type": "system", "subtype": "task_started", "task_id": "aAAA", "description": "d", "task_type": "local_agent"},
                {"type": "result", "is_error": False, "subtype": "success", "session_id": "sess-live"},
            ]
        )
        + "\n"
    )
    _job_dir_with_stream(tmp_path, job_id, "unfinished", stream)

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), alive_check=lambda pid: False)

    assert result.reaped == [job_id]
    job = db.get_job(conn, job_id)
    assert job["state"] == "blocked"
    assert job["state"] not in db.TERMINAL_STATES


def test_tick_never_dispatches_a_blocked_job(tmp_path: Path) -> None:
    """`blocked` is not terminal, so the guard against re-dispatch has to come
    from the scheduler selecting by explicit state name
    (design/decisions/blocked-not-failed.md)."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="stuck", state="blocked")
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.dispatched == []
    assert spawn.ids == []
    assert db.get_job(conn, job_id)["state"] == "blocked"


def test_tick_leaves_alive_running_job_untouched(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="live", state="running", pid=777)

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=SpawnSpy(),
        alive_check=lambda pid: True,
    )

    assert result.reaped == []
    assert db.get_job(conn, job_id)["state"] == "running"


def test_tick_skips_reap_for_running_job_without_a_pid(tmp_path: Path) -> None:
    """A just-claimed job (state=running, pid=NULL) whose detached run has
    not yet recorded its pid must not crash reap nor be falsely terminated."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="claimed", state="running", pid=None)

    def boom(pid: int) -> bool:
        raise AssertionError("alive_check must not be called for a NULL pid")

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=SpawnSpy(),
        alive_check=boom,
    )

    assert result.reaped == []
    assert db.get_job(conn, job_id)["state"] == "running"


def test_tick_reaped_slot_is_freed_for_dispatch(tmp_path: Path) -> None:
    """Reaping a dead running job in the same tick frees its slot so a due
    job is dispatched -- the whole point of reaping before dispatching."""
    conn = make_db(tmp_path)
    dead = add_job(conn, name="dead", state="running", pid=555)
    _job_dir_with_stream(tmp_path, dead, "dead", None)
    waiting = add_job(conn, name="waiting", at_time=PAST)
    spawn = SpawnSpy()

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=spawn,
        alive_check=lambda pid: False,
    )

    assert dead in result.reaped
    assert result.dispatched == [waiting]
    assert spawn.ids == [waiting]


# ---------------------------------------------------------------------------
# `now` may be a string or a callable
# ---------------------------------------------------------------------------


def test_tick_accepts_now_as_callable(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, before_time=PAST)

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=lambda: NOW, spawn_run=SpawnSpy())

    assert db.get_job(conn, job_id)["finished_at"] == NOW


# ---------------------------------------------------------------------------
# claim-then-spawn: the claim is committed BEFORE the detached run is spawned
# ---------------------------------------------------------------------------


def test_tick_commits_claim_before_spawning_run(tmp_path: Path) -> None:
    """`spawn_run` runs AFTER the dispatch lock commits, so a fresh
    connection opened inside it already sees the job as `running`. This is
    what lets the detached `run_job` proceed without contending on the
    tick's transaction."""
    path = tmp_path / "windowsill.db"
    conn = db.connect(path)
    add_job(conn, state="ready")
    seen: dict[str, Any] = {}

    def spawn_run(jid: int) -> None:
        other = db.connect(path)
        seen["state"] = db.get_job(other, jid)["state"]

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn_run)

    assert seen["state"] == "running"


def test_two_ticks_serialise_on_the_real_dispatch_lock(tmp_path: Path) -> None:
    """Two ticks, one temp DB, the real BEGIN IMMEDIATE dispatch lock: the
    first claims the single slot; the second sees the winner's claim
    committed and no-ops (concurrent-tick serialisation)."""
    path = tmp_path / "windowsill.db"
    conn1 = db.connect(path)
    conn2 = db.connect(path)
    job_id = add_job(conn1, state="ready")

    spawn1 = SpawnSpy()
    spawn2 = SpawnSpy()

    r1 = scheduler.tick(conn1, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn1)
    r2 = scheduler.tick(conn2, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn2)

    assert r1.dispatched == [job_id]
    assert spawn1.ids == [job_id]
    # loser sees the claim (job already running) and dispatches nothing
    assert r2.dispatched == []
    assert spawn2.ids == []
    assert db.get_job(conn2, job_id)["state"] == "running"


def test_dispatch_lock_is_actually_held_across_the_tick(tmp_path: Path) -> None:
    """While one tick holds the dispatch lock, a second connection cannot
    acquire BEGIN IMMEDIATE -- proving the tick's evaluation is serialised
    for real (not merely by claim-visibility)."""
    path = tmp_path / "windowsill.db"
    conn1 = db.connect(path)
    add_job(conn1, state="ready")

    raw2 = sqlite3.connect(str(path), timeout=0.2)
    conn2 = sqlite_utils.Database(raw2)

    # Hold conn1's lock and show conn2 cannot enter (short busy_timeout so the
    # test does not hang for seconds).
    lock1 = db.dispatch_lock(conn1)
    lock1.__enter__()
    try:
        with pytest.raises(sqlite3.OperationalError):
            with db.dispatch_lock(conn2):
                pass
    finally:
        lock1.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# the production `spawn_run` default: `windowsill run <id>`, detached, no test spawn
# ---------------------------------------------------------------------------


def test_run_argv_shape() -> None:
    assert scheduler.run_argv(7) == ["windowsill", "run", "7"]
    assert scheduler.RUN_ARGV == ("windowsill", "run")
    assert scheduler.TICK_ARGV == ("windowsill", "tick")


def test_default_spawn_run_popens_detached_windowsill_run(tmp_path: Path) -> None:
    """The production default Popens `windowsill run <id>` in its own session --
    verified through a fake popen so nothing is actually spawned."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return object()

    scheduler._default_spawn_run(42, popen=fake_popen)

    assert calls[0][0] == ["windowsill", "run", "42"]
    assert calls[0][1]["start_new_session"] is True


# ---------------------------------------------------------------------------
# dependencies: --hold-jid and --resume-from gate promotion
# ---------------------------------------------------------------------------


def test_hold_jid_keeps_a_job_queued_until_its_dependency_is_done(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    dep_id = add_job(conn, name="first", state="running", pid=None)
    waiter = add_job(conn, name="second", hold_jid=[str(dep_id)])
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(2), now=NOW, spawn_run=spawn)

    assert waiter not in result.promoted
    assert db.get_job(conn, waiter)["state"] == "queued"

    db.update_job(conn, dep_id, state="done", finished_at=NOW)
    result = scheduler.tick(conn, root=tmp_path, config=cfg(2), now=NOW, spawn_run=spawn)

    assert result.dispatched == [waiter]


def test_hold_jid_resolves_a_dependency_by_name_after_it_finished(tmp_path: Path) -> None:
    # A name resolves among non-terminal jobs (db.resolve_identifier) -- but a
    # dependency is only satisfied once it is terminal, so the lookup has to
    # keep working.
    conn = make_db(tmp_path)
    add_job(conn, name="audit", state="done")
    waiter = add_job(conn, name="refactor", hold_jid=["audit"])
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.dispatched == [waiter]


def test_hold_jid_dependency_that_failed_skips_the_dependent(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    dep_id = add_job(conn, name="first", state="failed", at_time=PAST)
    waiter = add_job(conn, name="second", hold_jid=[str(dep_id)])
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.skipped == [waiter]
    assert spawn.ids == []
    job = db.get_job(conn, waiter)
    assert job["state"] == "skipped"
    assert job["finished_at"] == NOW
    detail = db.list_events(conn, job_id=waiter)[-1]["detail"]
    assert "failed" in detail


def test_resume_from_resolves_the_conversation_at_dispatch(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    add_job(conn, name="audit", state="done", conversation_id="conv-audit-1")
    follow_on = add_job(conn, name="refactor", resume_from="audit")
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert result.dispatched == [follow_on]
    assert db.get_job(conn, follow_on)["resume_id"] == "conv-audit-1"


def test_resume_from_waits_for_the_conversation_to_exist(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    add_job(conn, name="audit", state="running", pid=None)
    follow_on = add_job(conn, name="refactor", resume_from="audit")
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(2), now=NOW, spawn_run=spawn)

    assert follow_on not in result.dispatched
    assert db.get_job(conn, follow_on)["state"] == "queued"


def test_explicit_resume_id_is_not_overwritten(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, resume_id="conv-explicit")
    spawn = SpawnSpy()

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert db.get_job(conn, job_id)["resume_id"] == "conv-explicit"


# ---------------------------------------------------------------------------
# --priority orders dispatch (lower runs first)
# ---------------------------------------------------------------------------


def test_lower_priority_number_dispatches_first(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    add_job(conn, name="ordinary", priority=0)
    urgent = add_job(conn, name="urgent", priority=-10)
    spawn = SpawnSpy()

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert spawn.ids == [urgent]  # submitted second, dispatched first


def test_equal_priority_dispatches_fifo_by_id(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    first = add_job(conn, name="first")
    add_job(conn, name="second")
    spawn = SpawnSpy()

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn)

    assert spawn.ids == [first]


# ---------------------------------------------------------------------------
# --window policies (window.policy_permits) and the probe rule
# (design/decisions/probes-are-free.md)
# ---------------------------------------------------------------------------


def test_share_policy_never_probes(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, window_policy="share")
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn, probe=exploding_probe)

    assert result.dispatched == [job_id]
    assert result.probed is False


def test_fresh_policy_dispatches_when_the_probe_reports_no_window(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, window_policy="fresh")
    probe = ProbeSpy(open_until=None)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn, probe=probe)

    assert result.dispatched == [job_id]
    assert result.probed is True
    assert probe.calls == 1


def test_fresh_policy_defers_while_a_window_is_open(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, window_policy="fresh")
    probe = ProbeSpy(open_until=FUTURE)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn, probe=probe)

    assert result.deferred == [job_id]
    assert result.dispatched == []
    # Deferred, not terminal: the job stays ready for the next tick.
    assert db.get_job(conn, job_id)["state"] == "ready"


def test_probe_is_taken_once_per_tick_and_cached_in_the_window_table(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    add_job(conn, name="a", window_policy="fresh")
    add_job(conn, name="b", window_policy="fresh")
    probe = ProbeSpy(open_until=FUTURE)

    scheduler.tick(conn, root=tmp_path, config=cfg(2), now=NOW, spawn_run=SpawnSpy(), probe=probe)

    assert probe.calls == 1
    cached = db.current_window(conn, NOW)
    assert cached is not None
    assert cached["pct_used"] == 15
    assert cached["probed_at"] == NOW


def test_offpeak_policy_defers_inside_the_peak_band(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, window_policy="offpeak")
    spawn = SpawnSpy()

    # NOW is 12:00Z on a Tuesday = 13:00 Europe/London, the start of the band.
    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn, probe=exploding_probe)

    assert result.deferred == [job_id]
    assert spawn.ids == []


def test_offpeak_policy_dispatches_outside_the_peak_band(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, window_policy="offpeak")
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=PAST, spawn_run=spawn, probe=exploding_probe)

    assert result.dispatched == [job_id]


# ---------------------------------------------------------------------------
# anchors fire conditionally (examples/06-anchor.md)
# ---------------------------------------------------------------------------


def test_anchor_skips_when_a_window_is_already_open(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    anchor = add_job(conn, name="morning-anchor", is_anchor=True)
    probe = ProbeSpy(open_until=FUTURE)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn, probe=probe)

    assert result.skipped == [anchor]
    assert spawn.ids == []
    assert db.get_job(conn, anchor)["state"] == "skipped"


def test_anchor_dispatches_when_no_window_is_open(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    anchor = add_job(conn, name="morning-anchor", is_anchor=True)
    probe = ProbeSpy(open_until=None)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn, probe=probe)

    assert result.dispatched == [anchor]


# ---------------------------------------------------------------------------
# the window ledger and the 30d ceiling (window.at_or_over_ceiling,
# design/01-motivation.md)
# ---------------------------------------------------------------------------


def test_dispatch_while_closed_records_the_window_it_opened(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn)

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=exploding_probe)

    rows = list(conn["window"].rows)
    assert len(rows) == 1
    assert rows[0]["opened_at"] == NOW
    assert rows[0]["opened_by"] == job_id


def test_dispatch_into_an_open_window_records_nothing(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.insert_window(conn, opened_at=PAST, reset_at=FUTURE)
    add_job(conn)

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=exploding_probe)

    assert len(list(conn["window"].rows)) == 1  # still just the open one


def test_two_dispatches_in_one_tick_open_only_one_window(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    add_job(conn, name="a")
    add_job(conn, name="b")

    result = scheduler.tick(conn, root=tmp_path, config=cfg(2), now=NOW, spawn_run=SpawnSpy(), probe=exploding_probe)

    assert len(result.dispatched) == 2
    assert len(list(conn["window"].rows)) == 1


def _fill_window_ledger(conn: sqlite_utils.Database, count: int) -> None:
    for _ in range(count):
        db.insert_window(conn, opened_at=PAST)


def test_anchor_is_refused_at_the_30d_ceiling(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _fill_window_ledger(conn, 3)
    anchor = add_job(conn, name="morning-anchor", is_anchor=True)
    config = {"queue": {"max_parallel": 1, "max_sessions_per_month": 3}}

    result = scheduler.tick(conn, root=tmp_path, config=config, now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=None))

    assert result.skipped == [anchor]
    assert "ceiling" in db.list_events(conn, job_id=anchor)[-1]["detail"]


def test_real_job_warns_at_the_ceiling_but_still_runs(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _fill_window_ledger(conn, 3)
    job_id = add_job(conn)
    config = {"queue": {"max_parallel": 1, "max_sessions_per_month": 3}}

    result = scheduler.tick(conn, root=tmp_path, config=config, now=NOW, spawn_run=SpawnSpy(), probe=exploding_probe)

    assert result.dispatched == [job_id]
    assert len(result.warnings) == 1
    assert "ceiling" in result.warnings[0]
    assert [event["kind"] for event in db.list_events(conn, job_id=job_id)] == ["ready", "warning", "claimed"]


# ---------------------------------------------------------------------------
# window warming (scheduler.warm_gate; README "--warm"; examples/07-warm.md)
#
# The clock in this section: NOW is 12:00Z. TAIL_RESET is 20 minutes later,
# so with the default `[warm].lead` of 30m a window resetting then is already
# in its tail; FUTURE (22:00Z) is ten hours out, so a window resetting then
# is not. Nothing here reads a real clock or spawns anything -- a "warmer
# dispatch" is the same recording SpawnSpy as everywhere else.
# ---------------------------------------------------------------------------

#: An open window whose reset is inside the default 30m lead.
TAIL_RESET = "2026-07-21T12:20:00Z"

#: `[warm].min_pct_free` is 20, so 95% used (5% free) is the abort case and
#: 10% used is the "plenty left to burn" case.
NEARLY_SPENT = 95
PLENTY_FREE = 10


def warm_events(conn: sqlite_utils.Database, job_id: int) -> list[str]:
    return [event["detail"] for event in db.list_events(conn, job_id=job_id) if event["kind"] == "warm"]


def only_warmer(conn: sqlite_utils.Database, parent_name: str = "nightly") -> dict[str, Any]:
    warmers = db.list_jobs(conn, name=scheduler.warmer_name(parent_name))
    assert len(warmers) == 1, f"expected exactly one warmer, got {len(warmers)}"
    return warmers[0]


def test_warm_job_with_no_window_spawns_a_warmer_instead_of_dispatching(tmp_path: Path) -> None:
    """The whole point of warming: a warm job that arrives with no window open
    must NOT open one itself -- doing that spends a window and owns exactly
    one. It inserts an anchor to open the window and waits for the tail."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", at_time=PAST, warm=True)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn, probe=ProbeSpy(open_until=None))

    assert result.dispatched == []
    assert spawn.ids == []
    assert result.deferred == [job_id]
    # Deferred, never terminal: a later tick asks again.
    assert db.get_job(conn, job_id)["state"] == "ready"
    # ... and nothing was written to the window ledger, because nothing ran.
    assert list(conn["window"].rows) == []


def test_the_spawned_warmer_is_an_anchor_carrying_the_anchor_config(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", cwd="/srv/project", priority=-5, warm=True)

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=None))

    warmer = only_warmer(conn)
    assert warmer["is_anchor"] == 1  # anchor handling does the dispatching, not warming
    assert warmer["state"] == "ready"
    assert warmer["body"] == "Reply with the single word: ok"
    assert warmer["cwd"] == "/srv/project"
    assert warmer["priority"] == -5  # stands in for the parent, queues where it does
    assert warmer["timeout"] == scheduler.WARMER_TIMEOUT
    # The model has to reach claude's argv, not just the display column --
    # a warmer that pings Opus charges the parent's own quota for it.
    assert warmer["model"] == "haiku"
    assert warmer["passthrough"] == ["--model", "haiku"]
    assert db.get_job(conn, job_id)["warmed_by"] == warmer["id"]


def test_the_spawned_warmer_is_dispatched_by_the_existing_anchor_path(tmp_path: Path) -> None:
    """Warming inserts a row and stops; the next tick's ordinary anchor
    handling is what runs it and writes the window ledger."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)
    spawn = SpawnSpy()

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn, probe=ProbeSpy(open_until=None))
    warmer_id = only_warmer(conn)["id"]
    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn, probe=ProbeSpy(open_until=None))

    assert result.dispatched == [warmer_id]
    assert spawn.ids == [warmer_id]
    assert result.deferred == [job_id]  # the parent still waits, in the same tick
    rows = list(conn["window"].rows)
    assert len(rows) == 1 and rows[0]["opened_by"] == warmer_id


def test_warm_job_does_not_spawn_a_second_warmer_while_the_first_is_live(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    warmer_id = add_job(conn, name="warm-nightly", state="running", pid=4242, is_anchor=True)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, warmed_by=warmer_id)

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(2),
        now=NOW,
        spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until=None),
        alive_check=lambda pid: True,
    )

    assert db.list_jobs(conn, name="warm-nightly") == [db.get_job(conn, warmer_id)]
    assert result.deferred == [job_id]
    assert "running" in warm_events(conn, job_id)[-1]


def test_a_failed_warmer_is_not_replaced_on_the_next_tick(tmp_path: Path) -> None:
    """The runaway guard. `tick` is chain-driven, so "no live warmer and no
    window" recurs within seconds; replacing a warmer that failed would
    submit and burn one job per tick for as long as claude stays broken."""
    conn = make_db(tmp_path)
    warmer_id = add_job(conn, name="warm-nightly", state="failed", is_anchor=True)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, warmed_by=warmer_id)

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=None))

    assert len(db.list_jobs(conn, name="warm-nightly")) == 1
    assert result.deferred == [job_id]
    assert result.dispatched == []
    assert "failed" in warm_events(conn, job_id)[-1]


def test_a_done_warmer_is_replaced_once_its_window_has_closed(tmp_path: Path) -> None:
    """The other side of that guard: a warmer that worked, whose window has
    since closed (a tick spent blocked, say), leaves the parent needing a
    fresh one -- and five hours is not a runaway."""
    conn = make_db(tmp_path)
    first = add_job(conn, name="warm-nightly", state="done", is_anchor=True)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, warmed_by=first)

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=None))

    warmers = db.list_jobs(conn, name="warm-nightly")
    assert len(warmers) == 2
    assert db.get_job(conn, job_id)["warmed_by"] == warmers[-1]["id"]


def test_warm_job_defers_until_the_tail_of_an_open_window(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=FUTURE))

    assert result.deferred == [job_id]
    assert result.dispatched == []
    assert db.get_job(conn, job_id)["state"] == "ready"
    # The reason names the time it will go: 30m before the 22:00 reset.
    assert warm_events(conn, job_id) == ["waiting for the window tail at 2026-07-21T21:30:00+00:00"]
    # No warmer: there is a window already, it is just too young to spend.
    assert db.list_jobs(conn, name="warm-nightly") == []


def test_warm_job_dispatches_in_the_tail_with_room_left(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)
    spawn = SpawnSpy()

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=spawn,
        probe=ProbeSpy(open_until=TAIL_RESET, pct_used=PLENTY_FREE),
    )

    assert result.dispatched == [job_id]
    assert spawn.ids == [job_id]
    assert db.get_job(conn, job_id)["state"] == "running"


def test_warm_job_in_the_tail_of_a_spent_window_defers_rather_than_fails(tmp_path: Path) -> None:
    """A window with nothing left in it is not an error. Failing or
    skipping the job here would throw away work over a condition that fixes
    itself in five hours."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until=TAIL_RESET, pct_used=NEARLY_SPENT),
    )

    assert result.deferred == [job_id]
    assert result.dispatched == []
    assert result.skipped == []
    job = db.get_job(conn, job_id)
    assert job["state"] == "ready"
    assert job["finished_at"] is None
    assert warm_events(conn, job_id) == ["only 5% of the window free, under [warm].min_pct_free (20)"]


def test_min_pct_free_is_read_from_config(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)
    config = {"queue": {"max_parallel": 1}, "warm": {"min_pct_free": 5}}

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=config,
        now=NOW,
        spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until=TAIL_RESET, pct_used=NEARLY_SPENT),
    )

    assert result.dispatched == [job_id]  # 5% free clears a 5% floor


#: Ten minutes after NOW: inside the default 20m `[warm].deadline_margin`.
CLOSE_DEADLINE = "2026-07-21T12:10:00Z"


def test_warm_job_near_its_deadline_dispatches_instead_of_waiting_for_a_late_tail(tmp_path: Path) -> None:
    """The 22:00 window's tail is hours after the 12:10 deadline. Waiting for
    it means expiring at 12:10 without ever running; going now does not."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, before_time=CLOSE_DEADLINE)

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until=FUTURE, pct_used=PLENTY_FREE),
    )

    assert result.dispatched == [job_id]
    assert "is close" in warm_events(conn, job_id)[-1]


def test_warm_job_near_its_deadline_opens_its_own_window_rather_than_warming(tmp_path: Path) -> None:
    """A warmer would take a tick to open the window and the parent another
    to follow it; with the deadline this close there is no second tick."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, before_time=CLOSE_DEADLINE)

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=None))

    assert result.dispatched == [job_id]
    assert db.list_jobs(conn, name="warm-nightly") == []


def test_warm_job_near_its_deadline_starts_into_a_spent_window(tmp_path: Path) -> None:
    """A rate limit requeues the run; an expiry is final."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, before_time=CLOSE_DEADLINE)

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until=TAIL_RESET, pct_used=NEARLY_SPENT),
    )

    assert result.dispatched == [job_id]


def test_warm_job_with_a_distant_deadline_still_waits_for_the_tail(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, before_time=FUTURE)

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=FUTURE))

    assert result.deferred == [job_id]
    assert warm_events(conn, job_id) == ["waiting for the window tail at 2026-07-21T21:30:00+00:00"]


def test_deadline_margin_is_read_from_config(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, before_time=CLOSE_DEADLINE)
    config = {"queue": {"max_parallel": 1}, "warm": {"deadline_margin": "5m"}}

    result = scheduler.tick(conn, root=tmp_path, config=config, now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=FUTURE))

    assert result.deferred == [job_id]  # 10 minutes out is not within 5


def test_warm_lead_directive_overrides_the_configured_lead(tmp_path: Path) -> None:
    """The 22:00 window is nowhere near its tail under a 30m lead, but a job
    that asks for ten hours of tail is in it already."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, warm_lead="10h")
    config = {"queue": {"max_parallel": 1}, "warm": {"lead": "30m"}}

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=config,
        now=NOW,
        spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until=FUTURE, pct_used=PLENTY_FREE),
    )

    assert result.dispatched == [job_id]


def test_configured_lead_applies_when_the_job_names_none(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)
    config = {"queue": {"max_parallel": 1}, "warm": {"lead": "10h"}}

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=config,
        now=NOW,
        spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until=FUTURE, pct_used=PLENTY_FREE),
    )

    assert result.dispatched == [job_id]


def test_an_unparseable_lead_falls_back_instead_of_breaking_every_tick(tmp_path: Path) -> None:
    """`warm_lead_seconds` runs inside the dispatch lock, so a ValueError
    there would roll back the tick -- one bad job file would stop the whole
    queue dispatching, every minute, until someone read the cron mail."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, warm_lead="thirty minutes")

    result = scheduler.tick(
        conn,
        root=tmp_path,
        config=cfg(1),
        now=NOW,
        spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until=TAIL_RESET, pct_used=PLENTY_FREE),
    )

    # Fell through to `[warm].lead` (30m), which puts TAIL_RESET in its tail.
    assert result.dispatched == [job_id]
    assert scheduler.warm_lead_seconds({"warm_lead": "thirty minutes"}, None) == 30 * 60


def test_a_rate_lead_starts_as_many_minutes_early_as_percent_is_free(tmp_path: Path) -> None:
    """`1%/min` with 95% free: the tail starts 95 minutes before the reset.
    A reset 100 minutes away is not in it yet; one 90 minutes away is."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True, warm_lead="1%/min")

    early = scheduler.tick(
        conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until="2026-07-21T13:40:00Z", pct_used=5),
    )
    assert early.deferred == [job_id]
    assert warm_events(conn, job_id) == ["waiting for the window tail at 2026-07-21T12:05:00+00:00"]

    late = scheduler.tick(
        conn, root=tmp_path, config=cfg(1), now="2026-07-21T12:10:00Z", spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until="2026-07-21T13:40:00Z", pct_used=5),
    )
    assert late.dispatched == [job_id]


def test_a_rate_lead_shrinks_when_the_window_is_already_spent(tmp_path: Path) -> None:
    """70% used leaves 30%: at 2%/min that is a 15-minute tail, so a reset
    20 minutes away waits and the same job with 1%/min would already go."""
    conn = make_db(tmp_path)
    fast = add_job(conn, name="fast", state="ready", warm=True, warm_lead="2%/min")
    slow = add_job(conn, name="slow", state="ready", warm=True, warm_lead="1%/min")

    result = scheduler.tick(
        conn, root=tmp_path, config=cfg(2), now=NOW, spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until=TAIL_RESET, pct_used=70),
    )

    assert result.deferred == [fast]
    assert result.dispatched == [slow]


def test_a_rate_lead_can_come_from_config(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)
    config = {"queue": {"max_parallel": 1}, "warm": {"lead": "1%/min"}}

    result = scheduler.tick(
        conn, root=tmp_path, config=config, now=NOW, spawn_run=SpawnSpy(),
        probe=ProbeSpy(open_until="2026-07-21T13:00:00Z", pct_used=30),
    )

    assert result.dispatched == [job_id]  # 70 free -> 70 minutes, reset in 60


def test_warm_rate_parsing() -> None:
    assert scheduler.warm_rate("1%/min") == 1.0
    assert scheduler.warm_rate(" 0.8 % / min ") == 0.8
    assert scheduler.warm_rate("0%/min") is None
    assert scheduler.warm_rate("30m") is None
    assert scheduler.warm_rate(None) is None
    # A rate needs the window reading; without one there is no lead yet.
    assert scheduler.warm_lead_seconds({"warm_lead": "1%/min"}, None) is None
    assert scheduler.warm_lead_seconds({"warm_lead": "1%/min"}, None, pct_free=95) == 95 * 60


def test_warm_job_forces_a_probe_even_on_the_share_policy(tmp_path: Path) -> None:
    """A probe only happens when the decision needs one
    (design/decisions/probes-are-free.md) -- and this decision does:
    `min_pct_free` is about the window right now."""
    conn = make_db(tmp_path)
    add_job(conn, name="nightly", state="ready", window_policy="share", warm=True)
    probe = ProbeSpy(open_until=TAIL_RESET, pct_used=PLENTY_FREE)

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=probe)

    assert probe.calls == 1
    assert result.probed is True


def test_warming_never_bypasses_blocked_until(tmp_path: Path) -> None:
    """Warming is an extra gate, not a way past the existing ones: a
    rate-limit block suppresses the warmer too, or the block would be spent
    opening windows nothing may run in."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)
    db.update_sched(conn, blocked_until=FUTURE, blocked_reason="rate limit")

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=exploding_probe)

    assert result.deferred == []
    assert db.list_jobs(conn, name="warm-nightly") == []
    assert db.get_job(conn, job_id)["warmed_by"] is None


def test_no_warmer_is_spawned_at_the_30d_ceiling(tmp_path: Path) -> None:
    """A warmer is an anchor, and an anchor gives way first at the 30d
    ceiling (window.at_or_over_ceiling). Spawning one anyway would also
    loop -- the anchor path skips it, a skipped warmer is replaceable, and
    the next tick submits another."""
    conn = make_db(tmp_path)
    _fill_window_ledger(conn, 3)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)
    config = {"queue": {"max_parallel": 1, "max_sessions_per_month": 3}}

    result = scheduler.tick(conn, root=tmp_path, config=config, now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=None))

    assert db.list_jobs(conn, name="warm-nightly") == []
    assert result.deferred == [job_id]
    assert "ceiling" in warm_events(conn, job_id)[-1]
    # And no "this job opens a window at the ceiling" warning: it doesn't.
    assert result.warnings == []


def test_the_tail_wait_is_logged_once_not_once_per_tick(tmp_path: Path) -> None:
    """A warm job waits out most of a five-hour window; `tick` runs every
    cron minute and again after every finished run. One event per decision,
    not one per tick."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)

    for _ in range(3):
        scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=FUTURE))

    assert len(warm_events(conn, job_id)) == 1


def test_a_new_window_says_so_again(tmp_path: Path) -> None:
    """The dedupe is on the text, and the text names the time being waited
    for -- so a different window is a different line, immediately."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", state="ready", warm=True)

    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=FUTURE))
    scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until="2026-07-21T23:00:00Z"))

    assert len(warm_events(conn, job_id)) == 2


def test_a_window_opened_this_tick_is_not_guessed_at(tmp_path: Path) -> None:
    """A window an earlier dispatch in this same tick opened is real but has
    no `reset_at` yet. Deriving one from `opened_at + [window].length` is
    exactly the mistake warming exists to avoid, so the job waits a tick."""
    conn = make_db(tmp_path)
    other = add_job(conn, name="ordinary", state="ready")
    job_id = add_job(conn, name="nightly", state="ready", warm=True)

    result = scheduler.tick(conn, root=tmp_path, config=cfg(2), now=NOW, spawn_run=SpawnSpy(), probe=ProbeSpy(open_until=None))

    assert result.dispatched == [other]
    assert result.deferred == [job_id]
    assert "reset time is not known" in warm_events(conn, job_id)[-1]
    assert db.list_jobs(conn, name="warm-nightly") == []


# ---------------------------------------------------------------------------
# ... and none of it happens to a job that didn't ask (warming defaults to off)
# ---------------------------------------------------------------------------


def test_a_non_warm_job_takes_the_old_path_exactly(tmp_path: Path) -> None:
    """The default-off proof: no probe (`exploding_probe` asserts it), no
    warmer, no warm event, and the dispatch that opens a window still opens
    it -- byte for byte the tick a job got before warming existed."""
    conn = make_db(tmp_path)
    job_id = add_job(conn, name="nightly", at_time=PAST)
    spawn = SpawnSpy()

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=spawn, probe=exploding_probe)

    assert result.dispatched == [job_id]
    assert spawn.ids == [job_id]
    assert result.probed is False
    assert result.deferred == []
    assert db.list_jobs(conn, name="warm-nightly") == []
    assert warm_events(conn, job_id) == []
    assert [event["kind"] for event in db.list_events(conn, job_id=job_id)] == ["ready", "claimed"]
    assert len(list(conn["window"].rows)) == 1


def test_a_non_warm_job_dispatches_into_a_nearly_spent_window(tmp_path: Path) -> None:
    """`min_pct_free` is a warming gate only. An ordinary job in a 95%-used
    window still runs -- refusing it would be a new way for the queue to
    silently not do the work it was given."""
    conn = make_db(tmp_path)
    db.insert_window(conn, opened_at=PAST, reset_at=TAIL_RESET, pct_used=NEARLY_SPENT)
    job_id = add_job(conn, name="nightly", state="ready")

    result = scheduler.tick(conn, root=tmp_path, config=cfg(1), now=NOW, spawn_run=SpawnSpy(), probe=exploding_probe)

    assert result.dispatched == [job_id]
