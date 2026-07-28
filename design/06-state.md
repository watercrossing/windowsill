# 6. State: SQLite

One DB at `~/.ccq/ccq.db` via `sqlite-utils`. It is also the lock — `BEGIN IMMEDIATE` is the dispatch lock, and state is crash-safe for free. Enable WAL.

```sql
CREATE TABLE jobs (
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
  conversation_id TEXT,               -- opaque; Claude's session_id
  pid           INTEGER, exit_code INTEGER
);

CREATE TABLE runs (                   -- one row per execution attempt
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id        INTEGER NOT NULL REFERENCES jobs(id),
  started_at    TEXT NOT NULL, finished_at TEXT,
  outcome       TEXT,                 -- done | failed | rate_limited | timeout | killed
  pct_before    INTEGER, pct_after INTEGER,   -- §4.3
  num_turns     INTEGER, duration_ms INTEGER,
  conversation_id TEXT
);

CREATE TABLE window (                 -- one row per window we opened; the §5.3 ledger
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  opened_at     TEXT NOT NULL,        -- our dispatch that opened it
  reset_at      TEXT,                 -- from /usage; NULL ⇒ closed (§4.1)
  probed_at     TEXT,
  pct_used      INTEGER,
  weekly_pct    INTEGER, weekly_reset_at TEXT,
  extra_bucket  TEXT, extra_pct INTEGER,   -- 'Fable' today; read the name, don't assume
  opened_by     INTEGER REFERENCES jobs(id)
);

CREATE TABLE sched (                  -- single row, id=1
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  blocked_until TEXT,
  blocked_reason TEXT
);

CREATE TABLE events (                 -- audit trail; feeds ccq report
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, job_id INTEGER, kind TEXT NOT NULL, detail TEXT
);
```

Current window = latest `window` row where `reset_at > now`. No row, or expired ⇒ closed.

## 6.1 Identifiers

Three namespaces; do not add a fourth.

- **`id`** — monotonic integer, never reused (even after `gc`). This is what you type: `ccq del 42`. SGE muscle memory is the point.
- **`name`** — deliberately *not* globally unique. Resubmit `nightly-lint` nightly and 42, 57, 88 all carry it. Unique only among non-terminal jobs, which is what makes `--hold-jid` and `--resume-from` resolvable.
- **`conversation_id`** — a UUID, but not ours. Claude emits it; it is opaque; it is the only identifier in the system we never invent.

Resolution for `--hold-jid` / `--resume-from`: parses as `int` ⇒ id lookup; otherwise ⇒ name, resolved to the most recent match, erroring if two non-terminal jobs share it.

## 6.2 Filesystem layout

Artifacts on disk, state in SQLite.

```
~/.ccq/
  config.toml
  ccq.db
  jobs/00042-refactor-auth/
    job.md          # verbatim submission
    cmd.json        # resolved argv + env + cwd, for reproducibility
    stream.jsonl    # raw stream-json
    result.json     # final result event
    transcript.md   # rendered, human-readable
    resume.sh       # cd <cwd> && claude --resume <id>
  worktrees/00042-refactor-auth/
  logs/ccq.log      # the scheduler's own log, never the jobs'
```

Zero-padded `<id>-<name>` so `ls` sorts.

The job dir is always written, whatever else is configured.
`--log` ([§2.3](02-job-file.md)) adds a *second* destination for `transcript.md` — it never moves the canonical one, because everything else here (`ccq show`, `ccq log`, `ccq gc`) addresses artifacts by job dir.
