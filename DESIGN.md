# ccq — a local batch queue for Claude Code

**One-liner:** SGE-style job files that queue prompts and run them unattended in 5-hour windows you would otherwise sleep through.

---

## 0. Instructions for the implementing agent

You are building this project. Read this section fully. **Do not read the rest of this document.**

### 0.1 Why you are being told not to read it

Context length is the cost driver. Measured on the target account: one large prompt consumed **15% of a 5-hour window**, with `61% of your usage was at >150k context`. This is a subscription; the constraint is window percentage, not dollars. Every token you hold in the orchestrator's context is paid for again on every subsequent turn of that conversation.

So: **you orchestrate, subagents implement.** Your context should hold this section, the work breakdown, and test results. Nothing else.

### 0.2 Subagent protocol

- **Delegate by section number, never by summary.** A subagent brief reads: *"Read §4 of `DESIGN.md`. Implement `src/ccq/usage.py`, tests first. Do not read other sections."* It must **never** read: *"Here's what §4 says: the usage parser should..."*
- This document is precise where it is precise on purpose. "The reset clause is the discriminator, not the percentage, not the line" survives verbatim reading and dies in paraphrase. **Paraphrasing the spec into a brief is the single most likely way to build the wrong thing.** If you find yourself explaining a section to a subagent, stop and give it the section number instead.
- **Use Sonnet for implementation subagents.** Opus for integration, design judgement, and anything where §0.4's tensions apply. Most of this codebase is unremarkable Python and does not need Opus.
- **Subagents report file paths and test results, not code.** Never let a subagent paste an implementation back into your context. You read the diff only if a test fails twice.
- **One module per subagent.** The §0.5 breakdown is designed so that modules and spec sections align, and a subagent needs exactly one section.
- Subagents cannot ask clarifying questions. If a brief is ambiguous, resolve it *before* dispatching, using §0.4.

### 0.3 Method

**Test-driven, genuinely.** Tests first, and they are not ceremony here: §9 records four findings that were established by experiment, three of which contradicted a plausible assumption. This spec has already been wrong once about `/usage` output in a way that only a fixture would catch.

- Every parser gets fixture-based tests before it gets an implementation. Fixtures for `/usage` output are in §4 verbatim — **both the open and closed forms**. A parser tested only against live output will only ever meet the open branch, which is the branch that works.
- No network, no `claude` invocation, and no real clock in the test suite. The runner takes an injectable command; the scheduler takes an injectable `now`.
- `pytest`. Coverage is not a target; the fixtures in §2, §4 and §7 are.

**Commit as you go.** Each module lands as its own commit with its tests passing. This project's own subject matter is unattended overnight execution — you will want a clean history when you start letting it run itself.

### 0.4 Standing tensions

You will hit these. They are decided; do not relitigate:

- **Pass-through beats validation** (§2.2). When tempted to validate a directive, don't.
- **Window count has a ceiling** (§1, §5.3). When tempted to harvest more windows, don't.
- **Scoping beats resuming** (§5.4). Resume-on-limit is a fallback for jobs that overrun, not a strategy for long work. Context is the cost.
- **The oracle is authoritative** (§4). §4 and §9 record behaviour observed on the target machine on 2026-07-17. Where they conflict with your training data about how `/usage` or Claude Code behaves, **§4 and §9 win.** Do not "correct" them.

### 0.5 Work breakdown

Modules map 1:1 onto spec sections. Dispatch one subagent per row.

| Module | Reads | Milestone |
|---|---|---|
| `ccq/jobfile.py` — directive parser, pass-through split | §2 | M1 |
| `ccq/db.py` — schema, migrations, `BEGIN IMMEDIATE` | §6 | M1 |
| `ccq/config.py` — TOML load, defaults | §8.3 | M1 |
| `ccq/runner.py` — invoke, spawn detached, reap, timeout | §3.2, §3.3 | M1 |
| `ccq/scheduler.py` — `tick`, dispatch, chain | §3.1, §7 | M1 |
| `ccq/cli.py` — click commands | §8.1 | M1 |
| `ccq/usage.py` — `/usage` oracle, probe rule | §4 | M2 |
| `ccq/window.py` — policies, anchors, accounting | §5 | M2/M3 |
| `ccq/notify.py` — ntfy, sendmail | §8.4 | M2 |
| `README.md` | §0.6, §1, §2, §8.1 | M1 |

Build M1 end-to-end and *use it for a night* before starting M2. §9's remaining open question — the rate-limit signal shape — is only answerable by observing a real limit hit, and M1 running real jobs is what produces one.

### 0.6 Deliverable shape

A proper installable package. Not a single file.

```
ccq/
├── pyproject.toml          # hatchling; requires-python = ">=3.13"
├── README.md               # install, job file format, the 3 commands that matter
├── DESIGN.md               # this document
├── src/ccq/
│   ├── __init__.py, cli.py, config.py, db.py, jobfile.py,
│   ├── notify.py, runner.py, scheduler.py, usage.py, window.py
└── tests/
    ├── fixtures/usage_open.txt, usage_closed.txt, jobs/*.md
    └── test_*.py
```

- Dependencies: `click`, `sqlite-utils`. Dev: `pytest`. Nothing else unless it saves a tone of work — stdlib covers `tomllib`, `zoneinfo`, `subprocess`, `shlex`, `signal`, `smtplib`.
- Entry point: `[project.scripts] ccq = "ccq.cli:main"`.
- Install: `uv tool install .` — or `uvx --from . ccq` to try it without installing.
- The `ccq` on `PATH` must work from cron with a minimal environment (§9 confirms `claude` itself does).

---

## 1. Motivation and objective

The binding constraint on a Claude Code subscription is **not** the weekly cap — it is the 5-hour window. Observed: the 5-hour limit is hit two or more times per working day while the weekly allowance sits at ~13% consumed. Weekly headroom is enormous; per-window headroom during waking hours is exhausted daily.

A window starts with your first message and lasts 5 hours ([support docs](https://support.anthropic.com/en/articles/11014257-about-claude-max-plan-usage)). Windows are not banked: one you sleep through is gone. Windows are account-global — Claude Code, the app, and claude.ai all draw from the same one.

**Objective: harvest the overnight windows that would otherwise expire unused, up to a configured monthly ceiling.**

The ceiling is not decoration. The same support article states that exceeding **50 sessions per month** may result in Anthropic limiting access — a flexible, case-by-case benchmark rather than a hard cut-off, but enforced by judgement about what looks excessive. A week holds ~33 windows; using all of them is ~143/month, roughly 3x the benchmark. Current organic usage is already ~60–90/month. So the realistic target is **+1 to +2 windows per night** (~+10 to +14/month), and the tool must count and enforce this itself (§5.3). A queue that maximises window count is a queue that gets its account throttled.

Secondary benefit: weekday peak hours reportedly drain the window faster (5–11am PT ≈ 13:00–19:00 Europe/London). Work deferred out of that band is cheaper per unit. Reported behaviour, not documented policy — keep it in config, never in code.

### Non-goals

- Cloud execution — Routines does that, without local file access.
- Recurring cadences — Desktop scheduled tasks do that well.
- Sleep/wake management. Target is an always-on VM.
- Multi-machine scheduling. Job records are local and stay local.
- A web UI.

### Nomenclature — read this before naming anything

Three distinct things are called "session". Mixing them will cause real bugs:

| Concept | This spec calls it | Where you meet it |
|---|---|---|
| The 5-hour billing segment | **window** | `/usage` line 1 ("Current session: 15% used"); the 50/month benchmark |
| A Claude Code conversation with a `session_id` | **conversation** | `--resume`, `~/.claude/projects/`, `/usage`'s "6 sessions" |
| One execution attempt of a queued job | **run** | This tool only |

The identifier `session` MUST NOT appear in the codebase. `/usage` uses the word for two of the three, in one screen of output.

---

## 2. The job file

A job is a UTF-8 text file: a directive header, a blank line, then the prompt body (Markdown, free-form, any length).

```
#!/usr/bin/env ccq
#$ --name refactor-auth
#$ --at 2026-07-18T02:00
#$ --model claude-opus-4-8
#$ --cwd ~/code/myapp
#$ --resume-from audit-auth
#$ --window fresh
#$ --isolate worktree
#$ --max-turns 60
#$ --retry 2
#$ --notify email

Continue the auth refactor from where the audit job left off.

Done means: every call site of `legacy_verify()` migrated to `verify_token()`,
`pytest tests/auth` green, and a single squashed commit on the branch.
If you hit a blocker, do not stop — write what you tried and what you would
need to `BLOCKED.md`, then continue with the next call site.
```

### 2.1 Parsing rules

1. Directives are lines matching `^#\$\s+(.*)$`, appearing **before** the first non-directive, non-blank, non-shebang line. A header terminated by prose cannot resume later in the file.
2. A leading `#!` line is ignored, so job files can be `chmod +x` and self-submit.
3. Everything from the first blank line after the header to EOF is the body, **verbatim**. No templating, no substitution, no escaping. What you type is what the model gets.
4. `#$ #...` and bare `#` lines inside the header are comments.
5. A trailing `\` continues a directive onto the next line.
6. Tokenise directive arguments with `shlex.split`, never a bare `.split()`.

### 2.2 Directive namespace — the central design rule

Two kinds of directive, distinguished by lookup rather than syntax:

- **Scheduler directives** are consumed by `ccq` and never reach the CLI (§2.3).
- **Everything else is passed through verbatim to `claude`**, in order, as argv.

`#$ --model claude-opus-4-8` becomes `claude --model claude-opus-4-8`. `#$ --max-turns 60` becomes `--max-turns 60`. `ccq` never needs to know what those mean, so a new Claude Code flag works on day one with no parser change.

Non-negotiable consequences:

- `ccq` MUST NOT validate or reject unknown directives. Pass them through; let `claude` complain.
- Pass-through args are argv elements, never a concatenated shell string. No `shell=True`. Ever.
- Where a scheduler directive shadows a real `claude` flag, the scheduler wins and it must be documented. That set is currently exactly one: `--resume` (§2.3), which needs late resolution.
- `--model` is pass-through, but is *also* mirrored into the `jobs.model` column for display and anchor defaults. Mirror, don't intercept.

### 2.3 Scheduler directives (complete set)

| Directive | Default | Meaning |
|---|---|---|
| `--name <slug>` | filename stem | Human label. Unique only among non-terminal jobs |
| `--at <when>` | ASAP | Earliest start. ISO-8601, `tomorrow 02:00`, or `+3h` |
| `--before <time>` | none | Do not start after this; job becomes `expired` if missed |
| `--window <policy>` | `share` | `fresh` \| `share` \| `offpeak` (§5.1) |
| `--anchor` | false | This is an anchor job (§5.2). Implies `--model claude-haiku-4-5` unless overridden |
| `--cwd <path>` | submit-time cwd | Working directory. `~` expanded at submit |
| `--resume <conversation_id>` | — | Continue an explicit conversation |
| `--resume-from <job>` | — | Continue the conversation produced by a completed job (resolved at dispatch) |
| `--hold-jid <job>[,<job>]` | — | Wait for those jobs to reach `done` |
| `--retry <n>` | `0` | Retries on non-rate-limit failure. Rate-limit requeues are free and unbounded (§5.4) |
| `--priority <-20..19>` | `0` | Tie-break within a window; lower runs first |
| `--isolate <mode>` | config | `none` \| `worktree` |
| `--allow-dirty` | false | Permit running against a dirty tree |
| `--notify <channel>` | config | `none` \| `ntfy` \| `email` |
| `--hold` | false | Submit `held`; needs `ccq release` |
| `--timeout <dur>` | `4h` | Wall-clock kill |
| `--no-resume-on-limit` | false | On rate-limit requeue, restart from the original body instead of resuming (§5.4) |

`--resume` and `--resume-from` are mutually exclusive. Both produce `claude --resume <id>` at dispatch.

---

## 3. Execution model

### 3.1 Dispatch: chain-driven, cron-floored

An asymmetry determines this design. **Idle time inside an open window is destroyed forever. Idle time while no window is open costs nothing**, because the next window anchors on the next message — blocked until 04:37 and dispatching at 04:45 loses 8 minutes of wall clock and zero minutes of allowance.

So latency only matters between jobs inside a live window, and polling is the wrong tool for it:

- **Chain dispatch (the latency path).** On job exit, the runner spawns a detached `ccq tick` and exits. Consecutive jobs in an open window start within seconds of each other.
- **Cron (the liveness floor).** `*/15 * * * * ccq tick` catches a broken chain, an `--at` coming due, and a `blocked_until` expiring. 15 minutes is fine precisely because it is never the latency path.

Cron, not a systemd timer: no `loginctl enable-linger` requirement, no unit files, works identically on any always-on box.

`ccq tick`:

1. Opens the DB, `BEGIN IMMEDIATE` (this is the dispatch lock — §6).
2. Reaps: for each `running` job, check pid liveness and `result.json`.
3. Evaluates the queue; dispatches up to `max_parallel` jobs.
4. Commits and exits. It never blocks on a running job.

Runs are spawned detached (own process group, output to the job dir), tracked by pid + start time. Concurrent ticks serialise on `BEGIN IMMEDIATE`; the loser sees the winner's state and no-ops. Chain-spawned ticks must be `Popen`-and-exit, never recursive in-process, or a fast-failing job becomes a fork bomb.

**`max_parallel` defaults to 1.** Concurrent runs drain the window faster and fight over the working tree. Opt-in, and warn on it.

### 3.2 The invocation

```
cd <cwd> && claude -p \
  --output-format stream-json --verbose \
  [--resume <conversation_id>] \
  <pass-through directives...> \
  < body
```

The body goes on **stdin**, never argv. Bodies will exceed `ARG_MAX` and will contain everything a shell hates.

**The result envelope is confirmed** (observed 2026-07-17, `--output-format json`):

```json
{"type":"result","subtype":"success","is_error":false,"duration_ms":537,
 "duration_api_ms":0,"num_turns":0,"result":"<text>","stop_reason":null,
 "session_id":"795f82fe-...","total_cost_usd":0,
 "usage":{"input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0,...},
 "modelUsage":{},"permission_denials":[],"uuid":"..."}
```

- `session_id` → `jobs.conversation_id`. `is_error` and `subtype` → run outcome. `num_turns`, `duration_ms`, `usage` → `ccq report`.
- `stream-json` emits incrementally: write straight to `stream.jsonl` so a killed run still leaves a usable partial, and derive the result from the last event.
- **Do not trust `total_cost_usd`.** The observed `0` came from a zero-turn slash command, so it is uninformative; whether it is populated for a real run on a subscription is untested (§9). Use the Δ window % (§4.3) as the cost metric regardless — it is the unit that actually binds.
- Confirm whether `--model` takes the full API string (`claude-opus-4-8`) or aliases only (`opus`). Normalise in config; reject neither.

### 3.3 Safety for unattended runs

Unattended means no permission prompts, so blast radius must be bounded by construction:

- Default to an explicit `--allowedTools` allowlist from config, **not** blanket permission bypass. A job wanting bypass must say so in its own header.
- `--isolate worktree` → `git worktree add ~/.ccq/worktrees/<id>-<name> -b ccq/<name>`, run there, leave the branch for morning review. Default for anything that writes code.
- Refuse to dispatch into a dirty tree unless `--allow-dirty`.
- Enforce `--timeout` with SIGTERM to the process group, then SIGKILL after 30s. Record the partial transcript.
- Never remove a worktree automatically. `ccq gc` only, on demand.

---

## 4. `/usage` as the window oracle

`claude -p "/usage"` returns ground truth. Both forms below are verbatim fixtures — **put them in `tests/fixtures/` before writing the parser.**

**Open window:**

```
You are currently using your subscription to power your Claude Code usage

Current session: 15% used · resets Jul 17, 7:09pm (Europe/London)
Current week (all models): 1% used · resets Jul 24, 5:59am (Europe/London)
Current week (Fable): 0% used

What's contributing to your limits usage?
Approximate, based on local sessions on this machine — does not include other devices or claude.ai. Behaviors are independent characteristics, not a breakdown.

Last 24h · 116 requests · 6 sessions
  61% of your usage was at >150k context
  Top skills: /schedule 2%
```

**Closed window** (observed 02:00 and 03:00, 2026-07-17):

```
You are currently using your subscription to power your Claude Code usage

Current session: 0% used
Current week (all models): 15% used · resets Jul 17, 5:59am (Europe/London)
Current week (Fable): 0% used
```

Notes:

- **Line 1's "session" is the window.** Its `resets` timestamp is the only fact the scheduler needs.
- **The trailing block is machine-local** — "does not include other devices or claude.ai" — and counts *conversations*, not windows. Never an input to a dispatch decision. Fine to surface in `ccq report`.
- **The separate weekly bucket is Fable, not Opus.** Support docs still say Opus-only; the client is ground truth. Read the bucket name from the output; do not hardcode a model.

### 4.1 Parsing

`--output-format json` wraps this string in an envelope (§3.2) but does **not** structure it. The text must be parsed. Treat it as hostile:

- Keep every regex in one module-level `USAGE_PATTERNS` constant. This string will change.
- **The closed state is `Current session: 0% used` with no `· resets` clause.** The line is present, so absence of the *line* is not the signal.
  - **`reset_at` is the sole discriminator.** Reset clause present ⇒ window open until it passes. Absent ⇒ closed.
  - **The percentage is display-only. Never infer state from it.** A window opened by one tiny request reads `0% used · resets ...`, so 0% is not evidence of anything.
  - **Never default a missing `reset_at` to `now + 5h`.** That reads the closed state as a window live until 07:00: `--window fresh` jobs never dispatch, and the cache reports live so nothing re-probes to correct it. Missing ⇒ `NULL` ⇒ closed.
- **No year in the timestamp.** Infer by choosing the candidate year (`now.year`, `±1`) that puts the reset nearest to now and in the future. Test 31 Dec explicitly.
- The timezone is parenthesised — resolve via `zoneinfo`, store UTC, display local.
- `ccq doctor` asserts the parse still works against live output and fails loudly if not.

### 4.2 The probe rule

**Probing does not open a window** (verified 2026-07-17: probes at 02:00 and 03:00 into an expired window both returned the closed state; had the 02:00 probe anchored, 03:00 would have shown a window live to ~07:00). The local request counter did not move either. `/usage` reads metadata; it is not an inference call — `num_turns: 0`, `duration_api_ms: 0`.

**And the reading is account-global.** Same night, local counters frozen to the digit (140/6, 165/8 across both probes) while `Current week` moved 13% → 15% — usage from another surface, invisible to the local block, visible on line 1. Lines 1–3 are server-side account truth.

Free and global together mean the oracle can simply be consulted:

- **Probe when a dispatch decision depends on window state** and the cached `reset_at` is absent or has passed. Common case; costs nothing.
- **Probe before and after each run** (§4.3).
- **Cache anyway**, in the `window` table (§6). Not for correctness — for latency and manners. `ccq stat` reads cache and never probes; `ccq usage --probe` is the explicit override. Rate-limit to 1 probe per 10s.
- **Do not probe on every cron tick regardless.** The tick usually has no dispatch candidate and therefore no question to ask.

This is a behavioural finding on one client version, not a documented guarantee. If `/usage` ever starts costing a window, the failure is silent and expensive — `ccq doctor` re-runs the two-probe check (§8.2).

Note: each probe returns a `session_id` and may leave a conversation record on disk. It does not increment the local conversation counter and is otherwise harmless.

`--window fresh` therefore verifies against global truth: probe first, and if a reset clause comes back, some other surface holds a live window — do not dispatch.

### 4.3 Δ window % as the cost metric

Probe immediately before and after each run; store both in `runs`. The delta is what the job cost in the only unit that binds.

- Approximate: another surface may move the number concurrently. Acceptable — single user, mostly asleep.
- `ccq report` shows Δ% per job. This is the number that tells you a job is too expensive to keep queueing.
- Sanity anchor from the target account: **one large prompt ≈ 15% of a window**, at >150k context. A job showing 40% is not a job, it is a project.

---

## 5. Windows, anchors, accounting, limits

### 5.1 `--window` policies

| Policy | Dispatch when |
|---|---|
| `share` (default) | Any time. Small follow-ups |
| `fresh` | A probe (§4.2) reports no live window. For the big overnight job that should own a whole window |
| `offpeak` | Now is outside `config.window.peak_band` |

### 5.2 Anchors are lazy, and mostly implicit

An anchor does not create capacity. It moves a boundary. **A window opened at 01:30 and slept through is not harvested capacity — it is a window spent for nothing.**

- **Any dispatched job is its own anchor.** If a job runs at 01:30, it opens the window. Firing a Haiku ping first to open a window the job would have opened anyway is pure waste. There is no anchor logic on the dispatch path at all.
- **A scheduled anchor is only worth it when no job needs the window but you want the boundary moved.** The realistic instance is one morning anchor: a 07:00 ping puts your working-day boundary at 12:00 instead of wherever a 09:00 start would drift it.
- **`--anchor` jobs fire conditionally**: probe first (free, §4.2), and if any window is open — including one opened from your phone — the job **no-ops to `skipped`**. Fixed cron times are fragile: answer one message at 23:20 and you have a window running to 04:20, so a 01:30 anchor would land inside it, open nothing, and silently break the day's alignment. Conditional firing is what makes an anchor safe to schedule.
- **At most one scheduled anchor exists.** `ccq doctor` warns if more than one non-terminal `--anchor` job is queued.

```toml
[anchor]
enabled = true
time    = "07:00"
model   = "claude-haiku-4-5"
body    = "Reply with the single word: ok"
```

Haiku both for cost and because it touches neither weekly bucket.

### 5.3 Window accounting (the ceiling from §1)

Every dispatch made while no window was open inserts a row into `window`. That table *is* the accounting.

- `windows_this_month = SELECT count(*) FROM window WHERE opened_at > now - 30d`
- Above `config.queue.max_sessions_per_month` (default **60**): **refuse to dispatch anchors**, warn loudly on real jobs, never silently block real work. An anchor is discretionary; a job you queued is not.
- `ccq report` and `ccq stat` show the trailing-30d count. Surface it before it becomes a surprise.

### 5.4 Rate-limit handling

Unattended runs must treat "limit reached" as a **schedulable event, not a failure**.

- Detect from the run's stream and exit status. Keep match patterns in one config-visible constant; they will change. Shape is unknown until observed (§9).
- On detection: probe `/usage` (free) for the true `reset_at`. Fall back to the error's own timestamp, then `now + 5h`.
- Set `sched.blocked_until`. `tick` dispatches nothing until it passes.
- The interrupted job returns to `ready` with its **retry count untouched** and its `conversation_id` recorded. On requeue it is re-dispatched as `--resume <conversation_id>` with body `Continue where you left off.`, unless `--no-resume-on-limit`.

**But treat this as a safety net, not a plan.** Resuming replays context, and context is the cost: at >150k, a single turn ran 15% of a window. A job routinely resumed across three windows is a job scoped wrong — it will cost more than three separate jobs doing a third each, because each resume re-pays for the whole conversation. The README should say this in the job-writing guidance: **scope a job to finish inside one window.** Resume-on-limit exists for the job that overran by 10 minutes, not for the job that was always going to take 12 hours.

---

## 6. State: SQLite

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

### 6.1 Identifiers

Three namespaces; do not add a fourth.

- **`id`** — monotonic integer, never reused (even after `gc`). This is what you type: `ccq del 42`. SGE muscle memory is the point.
- **`name`** — deliberately *not* globally unique. Resubmit `nightly-lint` nightly and 42, 57, 88 all carry it. Unique only among non-terminal jobs, which is what makes `--hold-jid` and `--resume-from` resolvable.
- **`conversation_id`** — a UUID, but not ours. Claude emits it; it is opaque; it is the only identifier in the system we never invent.

Resolution for `--hold-jid` / `--resume-from`: parses as `int` ⇒ id lookup; otherwise ⇒ name, resolved to the most recent match, erroring if two non-terminal jobs share it.

### 6.2 Filesystem layout

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

---

## 7. Job lifecycle

```
queued ──(--hold)──> held ──(release)──┐
   │                                   │
   └──(deps met, --at reached,     ready ──> running ──┬──> done
       window policy satisfied)──>   ▲                 ├──> failed    (retries exhausted)
                                     │                 ├──> expired   (--before passed)
                                     │                 ├──> skipped   (anchor, window already open)
                                     └─(rate limit /   └──> cancelled
                                        retry)
```

`done`, `failed`, `expired`, `skipped`, `cancelled` are terminal. Terminal jobs keep their job dir until `ccq gc`.

If the machine was down through a job's `--at`, run it late — **unless** `--before` was set and has passed ⇒ `expired`, notify, do not silently run stale work at breakfast.

---

## 8. Interface

### 8.1 CLI

| Command | Behaviour |
|---|---|
| `ccq sub <file>` | Submit. `-` reads stdin. Prints job id. `--now` bypasses `--at` |
| `ccq sub -e` | `$EDITOR` on a template header; submit on save |
| `ccq stat` | id, name, state, model, when, cwd, elapsed. Plus window state and 30d count |
| `ccq stat -a` | Include terminal jobs from the last 7 days |
| `ccq show <id>` | Directives, resolved argv, state, conversation id, Δ%, transcript tail |
| `ccq log <id> [-f]` | Stream the transcript; `-f` follows a running job |
| `ccq del <id>` | Cancel; SIGTERM the process group if running |
| `ccq hold <id>` / `ccq release <id>` | Toggle held |
| `ccq resume <id>` | `exec` into `claude --resume <conversation_id>` in the job's cwd. **The morning-after path. Make it the smoothest thing in the tool.** |
| `ccq report [--since 24h]` | Digest: what ran, Δ%, duration, state, branch created, one line each |
| `ccq usage` | Cached window state. `--probe` forces a fresh read |
| `ccq tick` | Internal; cron and chain entry point |
| `ccq gc [--older-than 30d]` | Remove terminal job dirs and worktrees. Never automatic |
| `ccq doctor` | §8.2 |

`ccq report` is the actual product at 08:00. Readable in five seconds; every line links to `ccq resume <id>`.

### 8.2 `ccq doctor`

In rough order of how likely each is to be the thing that's broken:

1. `claude` on PATH, authenticated, and working under `env -i HOME=$HOME PATH=$PATH` (verified 2026-07-17 via `at`; re-check, because credential handling is exactly what an update changes).
2. Cron entry installed and firing (check `logs/ccq.log` recency).
3. `/usage` parses against live output; every `USAGE_PATTERNS` regex matches — **including the closed state** (§4.1).
4. Probes are still free (§4.2): two probes into a closed window; warn loudly if the second reports a live one.
5. Rate-limit patterns still plausible.
6. Window state sane (no `reset_at` in the past marked live; no orphaned pids).
7. ≤1 non-terminal anchor job queued.
8. 30d window count vs `max_sessions_per_month`.
9. Notification channel reachable (ntfy POST, `sendmail` present and executable).

### 8.3 Config

`~/.ccq/config.toml`:

```toml
[queue]
max_parallel            = 1
default_model           = "claude-opus-4-8"
default_timeout         = "4h"
max_sessions_per_month  = 60      # §5.3; benchmark is 50, this is deliberately near it

[window]
length    = "5h"
peak_band = { days = "mon-fri", start = "13:00", end = "19:00", tz = "Europe/London" }

[anchor]
enabled = true
time    = "07:00"
model   = "claude-haiku-4-5"
body    = "Reply with the single word: ok"

[safety]
allowed_tools   = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
default_isolate = "worktree"
allow_dirty     = false

[notify]
default = "email"

[notify.ntfy]
url   = "https://ntfy.sh"
topic = "..."

[notify.email]
to       = "you@example.org"
from     = "noreply@example.org"
sendmail = "/usr/sbin/sendmail"
```

`peak_band` is config, not code: it is Anthropic policy and has already changed more than once.

### 8.4 Notifications

**ntfy** (POST to `url/topic`; title = job name + state; body = one-line result, Δ%, and `ccq resume <id>`) and **email** via `sendmail -t` from the configured noreply address. Nothing else. A failed notification is logged and never fails the job.

---

## 9. Findings and open questions

### Settled 2026-07-17

| Question | Answer | Consequence |
|---|---|---|
| Does auth survive a non-interactive job with no TTY? | **Yes.** `at` + `env -i HOME=$HOME PATH=$PATH claude -p "/usage"` → `exit=0` | The project is buildable. Was the one that could have killed it |
| What does `/usage` print with no window open? | `Current session: 0% used`, **no `· resets` clause** | §4.1 — the line is present; the reset clause is the discriminator |
| Does the probe anchor a window? | **No.** Probes at 02:00 and 03:00 into an expired window both read closed; local request counter unmoved; `num_turns: 0` | §4.2 — probes are free |
| Are the `Current session` / `Current week` lines account-global? | **Yes.** Local counters frozen (140/6, 165/8) while weekly moved 13% → 15% from another surface | §4.2 — the oracle sees your phone; `--window fresh` is verifiable |
| Does `--output-format json` structure the `/usage` output? | **No.** It wraps the same human string in `result` | §4.1 stands; but the envelope confirms §3.2's field names |

Evidence is one machine, one client version, one night. These are behavioural findings, not documented guarantees; `ccq doctor` re-checks the first four (§8.2).

### Still open

- **Rate-limit signal shape:** exit code, a subtype in the JSON, or only a human string? Decides how brittle §5.4 is. Only learnable by hitting a real one — which M1 will do within a week of use.
- **Is `total_cost_usd` populated for a real run on a subscription?** The observed `0` was a zero-turn slash command and proves nothing. Low stakes: §4.3 uses Δ% regardless.
- **How much does `--resume` actually cost** at high context? §5.4's guidance assumes it is expensive, on the basis that one >150k-context turn cost 15% of a window. Now measurable: probe, resume a long conversation with a trivial turn, probe again. Worth doing early — it is the difference between resume-on-limit being a safety net and being a trap.

---

## 10. Milestones

**M1 — solves the problem tonight.**
`jobfile`, `db`, `config`, `runner`, `scheduler`, `cli`, README. Cron + `tick`, chain dispatch, serial runs, `--at`, pass-through argv, stdin body, job dir, conversation-id capture, `resume.sh`, `ccq resume`. No `/usage`, no anchors, no retries. Already worth using.

**M2 — survives the night.**
`usage` oracle + probe rule + window table + Δ%. Rate-limit detection → `blocked_until` → requeue-with-resume. `--window`, `--retry`, `--timeout`, `--hold-jid`, `--resume-from`. `ccq report`, notifications.

**M3 — comfort and guardrails.**
Anchors + conditional fire + `max_sessions_per_month`. Worktree isolation. `ccq doctor`, `ccq gc`, `sub -e`, shell completion.

Ship M1 and use it for a night before starting M2.

---

## 11. Acceptance criteria

1. A job submitted at 18:00 with `--at 02:00 --model claude-opus-4-8` runs at ~02:00 unattended and leaves a `done` job dir. ✔
2. `ccq resume <id>` at 08:00 drops into the finished conversation, right cwd, full context. ✔
3. A job that hits the 5-hour limit mid-run does not fail: requeued, resumed from its own conversation id after the window resets, completes. ✔
4. An unknown directive (`#$ --some-new-flag x`) reaches `claude` untouched; `ccq` needs no change. ✔
5. Two jobs chained with `--resume-from` share one conversation across two windows. ✔
6. Six queued jobs in one open window run back-to-back with seconds between them, not 15 minutes. ✔
7. A `--anchor` job scheduled for 07:00 no-ops to `skipped` when a window opened at 04:00 is still live — **including when that window was opened from another device**. ✔
8. Fed the closed-state fixture (§4), the parser reports closed and dispatches a `--window fresh` job. Fixture, not live. ✔
9. Over a week of normal use, `ccq stat` shows a trailing-30d window count within `max_sessions_per_month`. ✔
10. `kill -9` on a running job leaves a readable partial transcript and a `failed` job, not a corrupt DB. ✔
11. `ccq report` after a night of four jobs fits on one screen, and each line shows Δ%. ✔

---

## 12. Prior art

- `gruckion/claude-scheduler` — one-shot scheduling with conversation-id capture and click-to-resume. Closest existing thing; steal the resume UX.
- `jshchnz/claude-code-scheduler` — cron JSON configs, git worktree isolation. Steal the worktree pattern.
- Claude Code docs: `scheduled-tasks` (`/loop` + Cron* tools, session-scoped), `desktop-scheduled-tasks` (SKILL.md + YAML frontmatter — Anthropic's own take on a job file), `cli-reference` (`-p`, `--resume`, `--output-format`, `--max-turns`).
- Anthropic support: [About Claude's Max Plan Usage](https://support.anthropic.com/en/articles/11014257-about-claude-max-plan-usage) — window definition and the 50-sessions/month benchmark. Re-read before changing §1 or §5.3.