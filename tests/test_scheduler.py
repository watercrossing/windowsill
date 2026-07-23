"""Tests for ccq.scheduler -- the `ccq tick` dispatch step (§3.1) and the
M1 slice of the §7 job-lifecycle state machine.

These tests spawn NO real process and read NO real clock: `tick`'s `now`,
`spawn_run`, and `alive_check` are all injectable, so every "detached run"
is a recording fake and every timestamp is a fixed string. The DB is always
opened at a tmp_path, never ~/.ccq. Concurrent-tick serialisation is
verified against the *real* `db.dispatch_lock` (a BEGIN IMMEDIATE).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlite_utils

from ccq import db, runner, scheduler

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

NOW = "2026-07-21T12:00:00Z"
PAST = "2026-07-21T02:00:00Z"
FUTURE = "2026-07-21T22:00:00Z"


def make_db(tmp_path: Path, name: str = "ccq.db") -> sqlite_utils.Database:
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
    )


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
# blocked_until gate (§3.1 "a blocked_until expiring")
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
# before_time -> expired (§7: "--before passed => expired")
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
    # no stream.jsonl -> reap derives "killed" -> job state "failed" (§7)
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
    path = tmp_path / "ccq.db"
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
    committed and no-ops (§3.1 concurrent-tick serialisation)."""
    path = tmp_path / "ccq.db"
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
    path = tmp_path / "ccq.db"
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
# the production `spawn_run` default: `ccq run <id>`, detached, no test spawn
# ---------------------------------------------------------------------------


def test_run_argv_shape() -> None:
    assert scheduler.run_argv(7) == ["ccq", "run", "7"]
    assert scheduler.RUN_ARGV == ("ccq", "run")
    assert scheduler.TICK_ARGV == ("ccq", "tick")


def test_default_spawn_run_popens_detached_ccq_run(tmp_path: Path) -> None:
    """The production default Popens `ccq run <id>` in its own session --
    verified through a fake popen so nothing is actually spawned."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return object()

    scheduler._default_spawn_run(42, popen=fake_popen)

    assert calls[0][0] == ["ccq", "run", "42"]
    assert calls[0][1]["start_new_session"] is True
