"""Tests for ccq.db — the SQLite state layer (design/06-state.md §6).

These tests never touch the real ~/.ccq: every DB is opened at a path
under pytest's tmp_path fixture. No network, no clock — timestamps are
always passed in explicitly by the test, never read from the system.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ccq import db

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_db(tmp_path: Path, name: str = "ccq.db") -> "db.sqlite_utils.Database":
    return db.connect(tmp_path / name)


def insert_bare_job(
    conn: "db.sqlite_utils.Database",
    *,
    name: str,
    state: str,
    submitted_at: str = "2026-07-18T00:00:00Z",
    cwd: str = "/tmp/work",
) -> int:
    return db.insert_job(
        conn,
        name=name,
        state=state,
        submitted_at=submitted_at,
        cwd=cwd,
        body="do the thing",
        passthrough=["--model", "claude-opus-4-8"],
    )


# ---------------------------------------------------------------------------
# schema creation
# ---------------------------------------------------------------------------


def test_connect_creates_all_five_tables(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    tables = {row[0] for row in conn.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert {"jobs", "runs", "window", "sched", "events"} <= tables


def test_jobs_table_has_expected_columns(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    cols = {row[1] for row in conn.conn.execute("PRAGMA table_info(jobs)").fetchall()}
    expected = {
        "id",
        "name",
        "state",
        "is_anchor",
        "submitted_at",
        "started_at",
        "finished_at",
        "cwd",
        "model",
        "body",
        "passthrough",
        "at_time",
        "before_time",
        "window_policy",
        "resume_id",
        "resume_from",
        "hold_jid",
        "retry_max",
        "retry_count",
        "priority",
        "isolate",
        "allow_dirty",
        "notify",
        "timeout",
        "log_path",
        "no_resume_on_limit",
        "conversation_id",
        "pid",
        "exit_code",
    }
    assert expected <= cols


def test_migrate_adds_a_missing_column_to_an_existing_database(tmp_path: Path) -> None:
    """A DB created before a column existed gets it added in place, keeping
    its rows -- ``CREATE TABLE IF NOT EXISTS`` alone would leave it behind."""
    path = tmp_path / "old.db"
    conn = db.connect(path)
    job_id = insert_bare_job(conn, name="pre-existing", state="queued")
    conn.conn.execute("ALTER TABLE jobs DROP COLUMN log_path")  # pretend it predates the column
    conn.close()

    reopened = db.connect(path)

    cols = {row[1] for row in reopened.conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "log_path" in cols
    job = db.get_job(reopened, job_id)
    assert job["name"] == "pre-existing"
    assert job["log_path"] is None


def test_runs_table_has_permission_denials_column(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    cols = {row[1] for row in conn.conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "permission_denials" in cols


def test_migrate_adds_permission_denials_to_an_existing_runs_table(tmp_path: Path) -> None:
    """A DB created before this column existed gets it added in place,
    keeping its run rows -- the same story as the jobs.log_path migration
    above, now for runs (§9 "Settled 2026-07-28, second sitting")."""
    path = tmp_path / "old.db"
    conn = db.connect(path)
    job_id = insert_bare_job(conn, name="pre-existing", state="done")
    run_id = db.insert_run(conn, job_id=job_id, started_at="2026-07-18T01:00:00Z", outcome="done")
    conn.conn.execute("ALTER TABLE runs DROP COLUMN permission_denials")  # pretend it predates the column
    conn.close()

    reopened = db.connect(path)

    cols = {row[1] for row in reopened.conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "permission_denials" in cols
    run = db.get_run(reopened, run_id)
    assert run["outcome"] == "done"
    assert run["permission_denials"] is None


def test_migrate_is_a_noop_on_an_up_to_date_database(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    assert db.migrate(conn) == []


def test_connect_is_idempotent(tmp_path: Path) -> None:
    """Reopening an existing DB must not error or wipe data."""
    path = tmp_path / "ccq.db"
    conn1 = db.connect(path)
    job_id = insert_bare_job(conn1, name="foo", state="queued")

    conn2 = db.connect(path)
    assert db.get_job(conn2, job_id)["name"] == "foo"


def test_connect_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "ccq.db"
    conn = db.connect(path)
    assert path.exists()
    insert_bare_job(conn, name="foo", state="queued")


# ---------------------------------------------------------------------------
# WAL
# ---------------------------------------------------------------------------


def test_wal_is_enabled(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    mode = conn.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# ---------------------------------------------------------------------------
# BEGIN IMMEDIATE as the dispatch lock
# ---------------------------------------------------------------------------


def test_dispatch_lock_commits_on_success(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    with db.dispatch_lock(conn):
        insert_bare_job(conn, name="foo", state="queued")
    # visible after the block, i.e. it was committed
    rows = list(conn.conn.execute("SELECT name FROM jobs"))
    assert rows == [("foo",)]


def test_dispatch_lock_rolls_back_on_exception(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    with pytest.raises(RuntimeError):
        with db.dispatch_lock(conn):
            insert_bare_job(conn, name="foo", state="queued")
            raise RuntimeError("boom")
    rows = list(conn.conn.execute("SELECT name FROM jobs"))
    assert rows == []


def test_dispatch_lock_serializes_two_connections(tmp_path: Path) -> None:
    """A second connection must not be able to acquire the dispatch lock
    while the first one holds it — this is the whole point of BEGIN
    IMMEDIATE as the scheduler's mutex."""
    path = tmp_path / "ccq.db"
    conn1 = db.connect(path)

    # Short busy_timeout so the test doesn't hang for seconds.
    raw2 = sqlite3.connect(str(path), timeout=0.2)
    import sqlite_utils

    conn2 = sqlite_utils.Database(raw2)

    lock1 = db.dispatch_lock(conn1)
    lock1.__enter__()
    try:
        with pytest.raises(sqlite3.OperationalError):
            with db.dispatch_lock(conn2):
                pass
    finally:
        lock1.__exit__(None, None, None)

    # Once released, the second connection can acquire it.
    with db.dispatch_lock(conn2):
        pass


def test_dispatch_lock_second_connection_succeeds_after_first_releases(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ccq.db"
    conn1 = db.connect(path)
    with db.dispatch_lock(conn1):
        insert_bare_job(conn1, name="foo", state="queued")

    conn2 = db.connect(path)
    with db.dispatch_lock(conn2):
        insert_bare_job(conn2, name="bar", state="queued")

    names = {row[0] for row in conn2.conn.execute("SELECT name FROM jobs")}
    assert names == {"foo", "bar"}


# ---------------------------------------------------------------------------
# sched: single-row table (id = 1)
# ---------------------------------------------------------------------------


def test_connect_seeds_single_sched_row(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    rows = list(conn.conn.execute("SELECT id, blocked_until, blocked_reason FROM sched"))
    assert rows == [(1, None, None)]


def test_connect_does_not_duplicate_sched_row_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "ccq.db"
    db.connect(path)
    db.connect(path)
    conn = db.connect(path)
    rows = list(conn.conn.execute("SELECT id FROM sched"))
    assert rows == [(1,)]


def test_sched_rejects_second_row_id(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.conn.execute("INSERT INTO sched (id, blocked_until, blocked_reason) VALUES (2, NULL, NULL)")


def test_sched_rejects_duplicate_row_with_id_one(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.conn.execute("INSERT INTO sched (id, blocked_until, blocked_reason) VALUES (1, NULL, NULL)")


def test_get_and_update_sched(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    assert db.get_sched(conn) == {"id": 1, "blocked_until": None, "blocked_reason": None}

    db.update_sched(conn, blocked_until="2026-07-19T00:00:00Z", blocked_reason="rate_limited")

    assert db.get_sched(conn) == {
        "id": 1,
        "blocked_until": "2026-07-19T00:00:00Z",
        "blocked_reason": "rate_limited",
    }


# ---------------------------------------------------------------------------
# jobs: insert / get / update round trip, incl. JSON columns
# ---------------------------------------------------------------------------


def test_insert_and_get_job_round_trips_json_fields(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = db.insert_job(
        conn,
        name="refactor-auth",
        state="queued",
        submitted_at="2026-07-18T00:00:00Z",
        cwd="/home/user/code/myapp",
        body="#!/usr/bin/env ccq\ndo the refactor",
        passthrough=["--model", "claude-opus-4-8", "--max-turns", "60"],
        hold_jid=["17", "audit-auth"],
    )
    assert job_id == 1

    job = db.get_job(conn, job_id)
    assert job["name"] == "refactor-auth"
    assert job["state"] == "queued"
    assert job["passthrough"] == ["--model", "claude-opus-4-8", "--max-turns", "60"]
    assert job["hold_jid"] == ["17", "audit-auth"]
    assert job["is_anchor"] == 0
    assert job["window_policy"] == "share"
    assert job["retry_max"] == 0


def test_get_job_missing_returns_none(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    assert db.get_job(conn, 999) is None


def test_insert_job_without_hold_jid_stores_none(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = insert_bare_job(conn, name="foo", state="queued")
    job = db.get_job(conn, job_id)
    assert job["hold_jid"] is None


def test_update_job(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = insert_bare_job(conn, name="foo", state="queued")

    db.update_job(conn, job_id, state="running", started_at="2026-07-18T01:00:00Z")

    job = db.get_job(conn, job_id)
    assert job["state"] == "running"
    assert job["started_at"] == "2026-07-18T01:00:00Z"


def test_update_job_reserializes_json_fields(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = insert_bare_job(conn, name="foo", state="queued")

    db.update_job(conn, job_id, passthrough=["--new-flag"])

    job = db.get_job(conn, job_id)
    assert job["passthrough"] == ["--new-flag"]

    # underlying storage is JSON text, not a python repr
    raw = conn.conn.execute("SELECT passthrough FROM jobs WHERE id = ?", [job_id]).fetchone()[0]
    assert json.loads(raw) == ["--new-flag"]


def test_ids_are_monotonic_and_not_reused(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    id1 = insert_bare_job(conn, name="a", state="done")
    id2 = insert_bare_job(conn, name="b", state="done")
    assert id2 == id1 + 1


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


def test_insert_and_get_run(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = insert_bare_job(conn, name="foo", state="running")

    run_id = db.insert_run(
        conn,
        job_id=job_id,
        started_at="2026-07-18T01:00:00Z",
        conversation_id="abc-123",
    )
    assert run_id == 1

    run = db.get_run(conn, run_id)
    assert run["job_id"] == job_id
    assert run["outcome"] is None

    db.update_run(
        conn,
        run_id,
        finished_at="2026-07-18T02:00:00Z",
        outcome="done",
        pct_before=10,
        pct_after=42,
    )
    run = db.get_run(conn, run_id)
    assert run["outcome"] == "done"
    assert run["pct_before"] == 10
    assert run["pct_after"] == 42


def test_insert_and_get_run_round_trips_permission_denials(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = insert_bare_job(conn, name="foo", state="running")

    run_id = db.insert_run(conn, job_id=job_id, started_at="2026-07-18T01:00:00Z", permission_denials=2)

    assert db.get_run(conn, run_id)["permission_denials"] == 2


def test_run_requires_existing_job(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_run(conn, job_id=999, started_at="2026-07-18T01:00:00Z")


def test_list_runs_for_job(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = insert_bare_job(conn, name="foo", state="running")
    other_job_id = insert_bare_job(conn, name="bar", state="running")

    r1 = db.insert_run(conn, job_id=job_id, started_at="2026-07-18T01:00:00Z")
    db.insert_run(conn, job_id=other_job_id, started_at="2026-07-18T01:05:00Z")
    r3 = db.insert_run(conn, job_id=job_id, started_at="2026-07-18T02:00:00Z")

    runs = db.list_runs(conn, job_id=job_id)
    assert [r["id"] for r in runs] == [r1, r3]


# ---------------------------------------------------------------------------
# window: latest row where reset_at > now
# ---------------------------------------------------------------------------


def test_current_window_none_when_no_rows(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    assert db.current_window(conn, now="2026-07-18T12:00:00Z") is None


def test_current_window_none_when_expired(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.insert_window(
        conn,
        opened_at="2026-07-18T00:00:00Z",
        reset_at="2026-07-18T05:00:00Z",
    )
    assert db.current_window(conn, now="2026-07-18T12:00:00Z") is None


def test_current_window_returns_latest_open_row(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.insert_window(
        conn,
        opened_at="2026-07-18T00:00:00Z",
        reset_at="2026-07-18T05:00:00Z",
    )
    w2 = db.insert_window(
        conn,
        opened_at="2026-07-18T05:00:00Z",
        reset_at="2026-07-18T10:00:00Z",
    )

    current = db.current_window(conn, now="2026-07-18T06:00:00Z")
    assert current["id"] == w2
    assert current["reset_at"] == "2026-07-18T10:00:00Z"


def test_current_window_null_reset_at_is_closed(tmp_path: Path) -> None:
    """NULL reset_at means the window was opened but never probed — §4.1
    treats that as closed, not open."""
    conn = make_db(tmp_path)
    db.insert_window(conn, opened_at="2026-07-18T00:00:00Z", reset_at=None)
    assert db.current_window(conn, now="2026-07-18T00:30:00Z") is None


def test_update_window(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    w_id = db.insert_window(conn, opened_at="2026-07-18T00:00:00Z")
    db.update_window(
        conn,
        w_id,
        reset_at="2026-07-18T05:00:00Z",
        probed_at="2026-07-18T00:05:00Z",
        pct_used=12,
    )
    w = db.get_window(conn, w_id)
    assert w["reset_at"] == "2026-07-18T05:00:00Z"
    assert w["pct_used"] == 12


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_log_and_list_events(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = insert_bare_job(conn, name="foo", state="queued")

    db.log_event(conn, ts="2026-07-18T00:00:00Z", kind="submitted", job_id=job_id)
    db.log_event(conn, ts="2026-07-18T00:01:00Z", kind="dispatched", job_id=job_id, detail="pid=123")

    events = db.list_events(conn, job_id=job_id)
    assert [e["kind"] for e in events] == ["submitted", "dispatched"]
    assert events[1]["detail"] == "pid=123"


# ---------------------------------------------------------------------------
# §6.1 identifier resolution
# ---------------------------------------------------------------------------


def test_resolve_int_identifier_returns_id(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = insert_bare_job(conn, name="anything", state="done")
    assert db.resolve_identifier(conn, str(job_id)) == job_id
    assert db.resolve_identifier(conn, job_id) == job_id


def test_resolve_int_identifier_terminal_state_still_resolves(tmp_path: Path) -> None:
    """int ⇒ id lookup has no non-terminal restriction — that's a name-only rule."""
    conn = make_db(tmp_path)
    job_id = insert_bare_job(conn, name="anything", state="done")
    assert db.resolve_identifier(conn, job_id) == job_id


def test_resolve_int_identifier_unknown_raises(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    with pytest.raises(db.UnknownIdentifierError):
        db.resolve_identifier(conn, 42)


def test_resolve_name_single_non_terminal_match(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    job_id = insert_bare_job(conn, name="nightly-lint", state="queued")
    assert db.resolve_identifier(conn, "nightly-lint") == job_id


def test_resolve_name_picks_most_recent_non_terminal_ignoring_terminal(
    tmp_path: Path,
) -> None:
    """Resubmitting a job name is normal (SGE muscle memory) — old,
    terminal runs of the same name must not interfere with resolving the
    live one."""
    conn = make_db(tmp_path)
    insert_bare_job(conn, name="nightly-lint", state="done", submitted_at="2026-07-17T00:00:00Z")
    insert_bare_job(conn, name="nightly-lint", state="failed", submitted_at="2026-07-16T00:00:00Z")
    live_id = insert_bare_job(conn, name="nightly-lint", state="queued", submitted_at="2026-07-18T00:00:00Z")

    assert db.resolve_identifier(conn, "nightly-lint") == live_id


def test_resolve_name_no_non_terminal_match_raises(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    insert_bare_job(conn, name="nightly-lint", state="done")
    with pytest.raises(db.UnknownIdentifierError):
        db.resolve_identifier(conn, "nightly-lint")


def test_resolve_name_unknown_raises(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    with pytest.raises(db.UnknownIdentifierError):
        db.resolve_identifier(conn, "does-not-exist")


def test_resolve_name_ambiguous_raises(tmp_path: Path) -> None:
    """Two non-terminal jobs sharing a name can't be disambiguated —
    this is the case §6.1 explicitly calls out as an error."""
    conn = make_db(tmp_path)
    insert_bare_job(conn, name="nightly-lint", state="queued", submitted_at="2026-07-17T00:00:00Z")
    insert_bare_job(conn, name="nightly-lint", state="running", submitted_at="2026-07-18T00:00:00Z")

    with pytest.raises(db.AmbiguousIdentifierError):
        db.resolve_identifier(conn, "nightly-lint")


def test_resolve_name_ambiguous_error_names_the_identifier(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    insert_bare_job(conn, name="dup", state="queued")
    insert_bare_job(conn, name="dup", state="held")

    with pytest.raises(db.AmbiguousIdentifierError, match="dup"):
        db.resolve_identifier(conn, "dup")
