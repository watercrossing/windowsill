"""windowsill state layer: ``~/.windowsill/windowsill.db`` (see design/06-state.md §6).

One SQLite database holds all mutable state: ``jobs``, ``runs``,
``window``, ``sched``, ``events``. It doubles as the scheduler's lock —
``BEGIN IMMEDIATE`` (exposed here as :func:`dispatch_lock`) serializes
concurrent dispatch attempts, and because everything lives in the DB,
state is crash-safe for free. WAL is enabled so readers never block the
single writer.

This module only opens the DB, creates the schema, and provides small,
obvious insert/get/update helpers per table — one function per verb, no
query builder, no ORM. Callers get back plain dicts (JSON columns like
``jobs.passthrough`` are decoded for them) and a bare
:class:`sqlite_utils.Database` handle they can drop to raw SQL with
whenever a helper doesn't cover what they need.

The DB path is always explicit or derived from an explicit ``home`` —
mirroring :mod:`windowsill.config` — so tests (and any caller that cares) never
touch the real ``~/.windowsill``.

A job has exactly three identifiers, and a fourth should not be added.
``id`` is the monotonic integer you type (``windowsill del 42``, SGE
muscle memory); ``AUTOINCREMENT`` rather than a bare rowid is what
guarantees it is never reused, even after old rows are deleted. ``name``
is a human label and deliberately *not* unique -- resubmit
``nightly-lint`` every night and every one of those jobs carries it --
so it resolves only among live jobs (:func:`resolve_identifier`).
``conversation_id`` is Claude's ``session_id``: opaque, and the only
identifier windowsill never invents.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import sqlite_utils

from windowsill import config

# ---------------------------------------------------------------------------
# schema (design/06-state.md §6, verbatim)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  state         TEXT NOT NULL,        -- §7
  is_anchor     INTEGER NOT NULL DEFAULT 0,
  submitted_at  TEXT NOT NULL,
  started_at    TEXT, finished_at TEXT,
  cwd           TEXT NOT NULL,
  model         TEXT,                 -- mirrored from pass-through, display only
  body          TEXT NOT NULL,
  passthrough   TEXT NOT NULL,        -- JSON argv list
  at_time       TEXT, before_time TEXT,
  window_policy TEXT NOT NULL DEFAULT 'share',
  resume_id     TEXT, resume_from TEXT,
  hold_jid      TEXT,                 -- JSON list
  retry_max     INTEGER NOT NULL DEFAULT 0,
  retry_count   INTEGER NOT NULL DEFAULT 0,
  priority      INTEGER NOT NULL DEFAULT 0,
  isolate       TEXT, allow_dirty INTEGER NOT NULL DEFAULT 0,
  notify        TEXT, timeout TEXT,
  log_path      TEXT,                 -- §2.3 --log; extra transcript.md destination
  no_resume_on_limit INTEGER NOT NULL DEFAULT 0,
  conversation_id TEXT,               -- opaque; Claude's session_id, the one identifier we never invent
  pid           INTEGER, exit_code INTEGER,
  warm          INTEGER NOT NULL DEFAULT 0,  -- §5.5 --warm: open the window early, run in its tail
  warm_lead     TEXT,                 -- per-job override of [warm].lead
  warmed_by     INTEGER REFERENCES jobs(id)  -- the anchor we spawned to open the window
);

CREATE TABLE IF NOT EXISTS runs (      -- one row per execution attempt
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id        INTEGER NOT NULL REFERENCES jobs(id),
  started_at    TEXT NOT NULL, finished_at TEXT,
  outcome       TEXT,                 -- done | failed | rate_limited | timeout | killed | incomplete
  pct_before    INTEGER, pct_after INTEGER,   -- §4.3
  num_turns     INTEGER, duration_ms INTEGER,
  permission_denials INTEGER,        -- a run refused every write still reports success;
                                     -- this count is the only trace
                                     -- (design/decisions/permission-denials-are-counted.md)
  conversation_id TEXT,
  summary       TEXT                 -- this attempt's own last words; the job dir's stream.jsonl is
                                     -- rewritten by the next attempt, so a requeued run's output only
                                     -- survives if it is copied here (§8.4 multi-run summary mail)
);

CREATE TABLE IF NOT EXISTS window (    -- one row per window we opened; the §5.3 ledger
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  opened_at     TEXT NOT NULL,        -- our dispatch that opened it
  reset_at      TEXT,                 -- from /usage; NULL ⇒ closed (§4.1)
  probed_at     TEXT,
  pct_used      INTEGER,
  weekly_pct    INTEGER, weekly_reset_at TEXT,
  extra_bucket  TEXT, extra_pct INTEGER,   -- 'Fable' today; read the name, don't assume
  opened_by     INTEGER REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS sched (     -- single row, id=1
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  blocked_until TEXT,
  blocked_reason TEXT
);

CREATE TABLE IF NOT EXISTS events (    -- audit trail; feeds windowsill report
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, job_id INTEGER, kind TEXT NOT NULL, detail TEXT
);
"""

# Job states treated as "finished" for §6.1 name resolution. This is the
# terminal set from the design/07-lifecycle.md §7 lifecycle exactly -- note "killed" and
# "timeout" are run *outcomes* (§6 runs.outcome), not job states, so they are
# not here; a killed/timed-out run leaves a "failed" job.
#
# "blocked" (§7) is deliberately **not** here even though nothing moves it on
# its own. It means "needs a human", and every consequence of this set is one
# a blocked job wants the non-terminal side of: `ccstat` without `-a` filters
# on exactly this set, and the blocked job is precisely the one the reader
# must find in the morning; `windowsill del` will cancel it; `resolve_identifier`
# still resolves it by name, so `windowsill resume <name>` works. The re-dispatch
# worry membership would have answered is handled structurally instead --
# the scheduler selects by explicit state name ("queued", "ready", "running"),
# never by "not terminal", so a blocked job is never picked up again.
TERMINAL_STATES: frozenset[str] = frozenset({"done", "failed", "expired", "skipped", "cancelled"})

_JOB_JSON_FIELDS = ("passthrough", "hold_jid")

#: Columns added to the §6 schema after a database may already exist on
#: disk. ``CREATE TABLE IF NOT EXISTS`` leaves an existing table's shape
#: alone, so :func:`connect` ALTERs these in explicitly — the schema above
#: stays the single description of the table, and an old ``~/.windowsill/windowsill.db``
#: keeps its jobs instead of needing a rebuild.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "jobs": {
        "log_path": "TEXT",
        "warm": "INTEGER NOT NULL DEFAULT 0",
        "warm_lead": "TEXT",
        "warmed_by": "INTEGER REFERENCES jobs(id)",
    },
    "runs": {"permission_denials": "INTEGER", "summary": "TEXT"},
}


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class IdentifierError(Exception):
    """Base class for §6.1 identifier resolution failures."""


class UnknownIdentifierError(IdentifierError):
    """Raised when an identifier doesn't resolve to any (eligible) job."""


class AmbiguousIdentifierError(IdentifierError):
    """Raised when a name matches more than one non-terminal job."""


# ---------------------------------------------------------------------------
# open / create
# ---------------------------------------------------------------------------


def default_db_path(home: Path | None = None) -> Path:
    """Return ``<home>/.windowsill/windowsill.db``.

    Uses the real ``Path.home()`` when ``home`` is not given — callers
    that must not touch the real filesystem (e.g. tests) should always
    pass ``home`` explicitly.
    """
    return config.state_dir(home) / "windowsill.db"


def connect(
    db_path: Path | None = None,
    *,
    home: Path | None = None,
    timeout: float = 5.0,
) -> sqlite_utils.Database:
    """Open (creating if needed) the windowsill state DB with the §6 schema.

    Resolution of the file to open mirrors :func:`windowsill.config.load_config`:
    ``db_path`` if given, otherwise ``<home>/.windowsill/windowsill.db``.

    Enables WAL and foreign-key enforcement, creates any of the five §6
    tables that don't already exist (safe to call repeatedly against the
    same file), and seeds the single ``sched`` row if it's missing.
    """
    path = db_path if db_path is not None else default_db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=timeout)
    database = sqlite_utils.Database(conn)
    database.execute("PRAGMA journal_mode = WAL")
    database.execute("PRAGMA foreign_keys = ON")
    database.executescript(SCHEMA_SQL)
    migrate(database)
    database.execute("INSERT OR IGNORE INTO sched (id, blocked_until, blocked_reason) VALUES (1, NULL, NULL)")
    return database


def migrate(database: sqlite_utils.Database) -> list[str]:
    """Add any :data:`ADDED_COLUMNS` an older database is missing.

    Returns the ``"<table>.<column>"`` names actually added (empty on an
    up-to-date or brand-new DB). Idempotent, and called by :func:`connect`
    on every open — the whole migration story is "columns get appended",
    which SQLite does in place and for free.
    """
    added: list[str] = []
    for table, columns in ADDED_COLUMNS.items():
        existing = {column.name for column in database[table].columns}
        for name, declaration in columns.items():
            if name not in existing:
                database.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                added.append(f"{table}.{name}")
    return added


@contextmanager
def dispatch_lock(database: sqlite_utils.Database) -> Iterator[None]:
    """The dispatch lock: ``BEGIN IMMEDIATE`` for the duration of the block.

    Grabs SQLite's write lock immediately (rather than on first write),
    so a second process/connection trying to enter this block while one
    holds it blocks (and eventually raises ``sqlite3.OperationalError:
    database is locked``) instead of interleaving. Commits on a clean
    exit, rolls back on exception.
    """
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        database.conn.execute("ROLLBACK")
        raise
    else:
        database.conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


def _decode_job(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    for field in _JOB_JSON_FIELDS:
        if row.get(field) is not None:
            row[field] = json.loads(row[field])
    return row


def _encode_job_fields(fields: dict[str, Any]) -> dict[str, Any]:
    fields = dict(fields)
    for field in _JOB_JSON_FIELDS:
        if field in fields and fields[field] is not None:
            fields[field] = json.dumps(list(fields[field]))
    return fields


def insert_job(
    database: sqlite_utils.Database,
    *,
    name: str,
    state: str,
    submitted_at: str,
    cwd: str,
    body: str,
    passthrough: list[str],
    is_anchor: bool | int = 0,
    started_at: str | None = None,
    finished_at: str | None = None,
    model: str | None = None,
    at_time: str | None = None,
    before_time: str | None = None,
    window_policy: str = "share",
    resume_id: str | None = None,
    resume_from: str | None = None,
    hold_jid: list[str] | None = None,
    retry_max: int = 0,
    retry_count: int = 0,
    priority: int = 0,
    isolate: str | None = None,
    allow_dirty: bool | int = 0,
    notify: str | None = None,
    timeout: str | None = None,
    log_path: str | None = None,
    no_resume_on_limit: bool | int = 0,
    conversation_id: str | None = None,
    pid: int | None = None,
    exit_code: int | None = None,
    warm: bool | int = 0,
    warm_lead: str | None = None,
    warmed_by: int | None = None,
) -> int:
    """Insert a row into ``jobs``, return its (new, monotonic) id."""
    record = _encode_job_fields(
        dict(
            name=name,
            state=state,
            is_anchor=int(bool(is_anchor)),
            submitted_at=submitted_at,
            started_at=started_at,
            finished_at=finished_at,
            cwd=cwd,
            model=model,
            body=body,
            passthrough=passthrough,
            at_time=at_time,
            before_time=before_time,
            window_policy=window_policy,
            resume_id=resume_id,
            resume_from=resume_from,
            hold_jid=hold_jid,
            retry_max=retry_max,
            retry_count=retry_count,
            priority=priority,
            isolate=isolate,
            allow_dirty=int(bool(allow_dirty)),
            notify=notify,
            timeout=timeout,
            log_path=log_path,
            no_resume_on_limit=int(bool(no_resume_on_limit)),
            conversation_id=conversation_id,
            pid=pid,
            exit_code=exit_code,
            warm=int(bool(warm)),
            warm_lead=warm_lead,
            warmed_by=warmed_by,
        )
    )
    return database["jobs"].insert(record).last_pk  # type: ignore[return-value]


def get_job(database: sqlite_utils.Database, job_id: int) -> dict[str, Any] | None:
    """Return the job row as a dict, or ``None`` if no such id exists."""
    try:
        row = database["jobs"].get(job_id)
    except sqlite_utils.db.NotFoundError:
        return None
    return _decode_job(row)


def update_job(database: sqlite_utils.Database, job_id: int, **fields: Any) -> None:
    """Update arbitrary columns of a ``jobs`` row by id."""
    database["jobs"].update(job_id, _encode_job_fields(fields))


def list_jobs(
    database: sqlite_utils.Database,
    *,
    name: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """List jobs, optionally filtered by exact ``name`` and/or ``state``."""
    clauses: list[str] = []
    params: list[Any] = []
    if name is not None:
        clauses.append("name = ?")
        params.append(name)
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    where = " AND ".join(clauses) if clauses else None
    rows = database["jobs"].rows_where(where, params, order_by="id")
    return [_decode_job(row) for row in rows]


def resolve_identifier(
    database: sqlite_utils.Database,
    identifier: str | int,
    *,
    terminal_states: frozenset[str] = TERMINAL_STATES,
    include_terminal: bool = False,
) -> int:
    """Resolve a ``--hold-jid`` / ``--resume-from`` argument to a job id.

    Per §6.1: parses as ``int`` ⇒ id lookup (existence still checked,
    regardless of that job's state). Otherwise ⇒ treated as a ``name``,
    resolved among *non-terminal* jobs only — names are only guaranteed
    unique there. Zero non-terminal matches is an
    :class:`UnknownIdentifierError`; two or more is an
    :class:`AmbiguousIdentifierError` (can't disambiguate, so we refuse
    to guess — the caller should use the numeric id instead).

    ``include_terminal`` widens the *fallback* only: with no live match, the
    most recent job of that name wins whatever its state. That is what a
    dependency lookup needs — ``--resume-from audit-auth`` names a job that,
    by the time it is resolved, has necessarily finished — while an
    ambiguous live name is still refused. Interactive commands keep the
    narrow default: ``windowsill del nightly-lint`` should not reach back into last
    week's finished run.
    """
    try:
        job_id = int(identifier)
    except (TypeError, ValueError):
        job_id = None

    if job_id is not None:
        if get_job(database, job_id) is None:
            raise UnknownIdentifierError(f"no job with id {job_id}")
        return job_id

    name = str(identifier)
    matches = list(database["jobs"].rows_where("name = ?", [name], order_by="id desc"))
    candidates = [row for row in matches if row["state"] not in terminal_states]
    if len(candidates) > 1:
        raise AmbiguousIdentifierError(f"{len(candidates)} non-terminal jobs named {name!r}; use the numeric id")
    if candidates:
        return candidates[0]["id"]
    if include_terminal and matches:
        return matches[0]["id"]
    raise UnknownIdentifierError(f"no non-terminal job named {name!r}")


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


def insert_run(
    database: sqlite_utils.Database,
    *,
    job_id: int,
    started_at: str,
    finished_at: str | None = None,
    outcome: str | None = None,
    pct_before: int | None = None,
    pct_after: int | None = None,
    num_turns: int | None = None,
    duration_ms: int | None = None,
    permission_denials: int | None = None,
    conversation_id: str | None = None,
    summary: str | None = None,
) -> int:
    """Insert a row into ``runs`` (one execution attempt), return its id."""
    record = dict(
        job_id=job_id,
        started_at=started_at,
        finished_at=finished_at,
        outcome=outcome,
        pct_before=pct_before,
        pct_after=pct_after,
        num_turns=num_turns,
        duration_ms=duration_ms,
        permission_denials=permission_denials,
        conversation_id=conversation_id,
        summary=summary,
    )
    return database["runs"].insert(record).last_pk  # type: ignore[return-value]


def get_run(database: sqlite_utils.Database, run_id: int) -> dict[str, Any] | None:
    try:
        return database["runs"].get(run_id)
    except sqlite_utils.db.NotFoundError:
        return None


def update_run(database: sqlite_utils.Database, run_id: int, **fields: Any) -> None:
    database["runs"].update(run_id, fields)


def list_runs(database: sqlite_utils.Database, *, job_id: int | None = None) -> list[dict[str, Any]]:
    """List runs, in insertion order, optionally filtered to one job."""
    if job_id is None:
        rows = database["runs"].rows_where(order_by="id")
    else:
        rows = database["runs"].rows_where("job_id = ?", [job_id], order_by="id")
    return list(rows)


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------


def insert_window(
    database: sqlite_utils.Database,
    *,
    opened_at: str,
    reset_at: str | None = None,
    probed_at: str | None = None,
    pct_used: int | None = None,
    weekly_pct: int | None = None,
    weekly_reset_at: str | None = None,
    extra_bucket: str | None = None,
    extra_pct: int | None = None,
    opened_by: int | None = None,
) -> int:
    """Insert a row into ``window`` (§5.3 ledger), return its id."""
    record = dict(
        opened_at=opened_at,
        reset_at=reset_at,
        probed_at=probed_at,
        pct_used=pct_used,
        weekly_pct=weekly_pct,
        weekly_reset_at=weekly_reset_at,
        extra_bucket=extra_bucket,
        extra_pct=extra_pct,
        opened_by=opened_by,
    )
    return database["window"].insert(record).last_pk  # type: ignore[return-value]


def get_window(database: sqlite_utils.Database, window_id: int) -> dict[str, Any] | None:
    try:
        return database["window"].get(window_id)
    except sqlite_utils.db.NotFoundError:
        return None


def update_window(database: sqlite_utils.Database, window_id: int, **fields: Any) -> None:
    database["window"].update(window_id, fields)


def current_window(database: sqlite_utils.Database, now: str) -> dict[str, Any] | None:
    """The open window, if any: latest row where ``reset_at > now``.

    A window with no ``reset_at`` yet (never probed) or an expired one
    doesn't count — no row ⇒ closed (§4.1/§6).
    """
    rows = list(database["window"].rows_where("reset_at IS NOT NULL AND reset_at > ?", [now], order_by="id desc", limit=1))
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# sched (single row, id = 1)
# ---------------------------------------------------------------------------


def get_sched(database: sqlite_utils.Database) -> dict[str, Any]:
    """Return the single ``sched`` row. Always present after :func:`connect`."""
    return database["sched"].get(1)  # type: ignore[return-value]


def update_sched(
    database: sqlite_utils.Database,
    *,
    blocked_until: str | None = None,
    blocked_reason: str | None = None,
) -> None:
    database["sched"].update(1, {"blocked_until": blocked_until, "blocked_reason": blocked_reason})


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def log_event(
    database: sqlite_utils.Database,
    *,
    ts: str,
    kind: str,
    job_id: int | None = None,
    detail: str | None = None,
) -> int:
    """Append a row to ``events`` (the audit trail behind ``windowsill report``)."""
    record = dict(ts=ts, job_id=job_id, kind=kind, detail=detail)
    return database["events"].insert(record).last_pk  # type: ignore[return-value]


def list_events(database: sqlite_utils.Database, *, job_id: int | None = None) -> list[dict[str, Any]]:
    """List events, in insertion order, optionally filtered to one job."""
    if job_id is None:
        rows = database["events"].rows_where(order_by="id")
    else:
        rows = database["events"].rows_where("job_id = ?", [job_id], order_by="id")
    return list(rows)
