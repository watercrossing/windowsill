# Changelog

## Unreleased

### Added

- **Window warming** (`#$ --warm`, `[warm]` config). A throwaway anchor opens
  the usage window at the job's `--at` time and the job itself waits until
  `[warm].lead` before the window resets, so the run spends the tail of one
  window and rolls straight into the next — close to two windows of quota from
  one scheduled time. Gated on `[warm].min_pct_free` (default 20%) so it never
  starts a job into a window with nothing left in it. Off unless asked for.
  See `design/05-windows.md` §5.5, which includes the measured burn rate this
  queue's own ledger gives for a Pro plan (median 2.76 %/min ⇒ ~36 minutes to
  spend a whole window) and how to re-derive the lead for a Max plan.
- **The completion email now covers every attempt, not just the last one.** A
  job that was rate-limited twice and finished on the third try used to send a
  mail describing a three-minute run. It now reports the run count, each
  attempt's outcome and its own last words, the total wall time from first
  start to last finish, and the window percentage spent. Each attempt's summary
  is kept in `runs.summary`, because the job directory's `stream.jsonl` is
  overwritten by the next attempt.
- **`[notify] on_rate_limit`** (default off). Sends one short line when a run
  is rate-limited and requeued — when it resumes, the usage numbers, and the
  retry budget. A requeued job is not terminal and so sends no completion mail
  by design; without this, a job that stalls at 21:21 and finishes at 07:03 is
  silent for ten hours, and silence looks exactly like a crashed tick.
- **`examples/`** — a graded set of runnable job files, from the three-line
  "does my install work" case up to held pipelines and warmed windows.

### Fixed

- **A successful run whose agent left background tasks running was reported as
  `killed`.** The result envelope is not the last line of `stream.jsonl`: the
  CLI emits a teardown tail after it — one `background_tasks_changed` /
  `task_updated` / `task_notification` triple per background task it kills off
  at session exit. `parse_result_event` took §3.2's "derive the result from the
  last event" literally, found a `system` event, and returned `None`, which the
  outcome mapping reads as "the process died before emitting a result". It now
  scans for the last `result` event. The label was the smallest part of the
  damage: `conversation_id` comes off that same event, so the job kept none,
  no `result.json` or `resume.sh` was written, and the mail said "No
  conversation to resume" with the session id sitting in the stream. The true
  outcome in the case that surfaced this was `incomplete` (job state `blocked`)
  — a mail that names the tasks left hanging and tells you to `ccresume`.
  Measured against the real capture and recorded in `design/09-findings.md`
  (settled 2026-08-28), which also corrects the §3.2 wording the bug was
  faithfully implementing.
- **The completion email announced expired queue holds as current.**
  `sched.blocked_until` is never cleared when it lapses — the scheduler and
  `ccstat` both just stop honouring it by comparison — but `mail` reads no
  clock by contract and tested only that the field was set, so a mail sent on
  the 28th reported the queue blocked until the 21st. `runner._run_facts` now
  does the comparison, against the run's own `finished_at`, and passes the hold
  on only while it is live.

- `ccsub` now rejects `--warm` together with `--window fresh` instead of
  accepting a job that can never run. Warming holds a job until the tail of an
  *open* window; `fresh` holds it until *no* window is open; the tick applies
  warming first, so the pair deferred on every tick forever. Because a deferred
  warm job is never terminal, the job would have sat `ready` and silent rather
  than failing.

### Changed

- GitHub Actions bumped to the Node 24 majors (`checkout` v4→v7, `setup-uv`
  v5→v10.0.1, `setup-python` v5→v7). Every run was warning that Node 20 was
  deprecated and that it was being force-upgraded, which is not a supported
  configuration. `setup-uv` is an exact tag rather than a major because it
  stopped publishing floating major tags after v7 — `@v8`, `@v9` and `@v10`
  are unresolvable refs, not old versions.
- **Nothing windowsill ships pins a model version any more.** Every example,
  the README and `[queue].default_model` named `claude-opus-4-8`, and
  `[anchor].model` named `claude-haiku-4-5`; they now use the bare family
  aliases `opus` and `haiku`. Both spellings work — `claude` accepts a full
  versioned id or a bare alias — but a shipped default should not choose a
  version on the reader's behalf. The pass-through fixtures in `tests/` keep
  the full id on purpose: they assert that an arbitrary string reaches argv
  unaltered, and a versioned id is the better witness for that.
- `README.md` and `examples/01-hello.md` now say which model spellings are
  valid. The two halves do not mix: `claude-opus` (prefix, no version) and
  `opus-5` (version, no prefix) are both rejected, and because `--model` is
  pass-through the rejection is a 404 at dispatch rather than an error at
  submit — on a job with `--at`, that costs a night. Measured and recorded in
  `design/09-findings.md` (settled 2026-08-27), which also closes the open
  question in `design/03-execution-model.md` §3.2.

## 0.2.0 — 2026-08-04

The repository moved from `watercrossing/ccq` to `watercrossing/windowsill`
and is now public. Nothing in the package's behaviour changed; this release
exists to correct metadata that pointed at the old name and to put the test
suite somewhere other than one laptop.

### Changed

- Project URLs now point at `watercrossing/windowsill`. The 0.1.0 metadata on
  PyPI still names `ccq`; GitHub redirects it, but the URLs were wrong.
- Added a `Changelog` project URL (this file).

### Added

- **CI** (`.github/workflows/ci.yaml`): the suite on Python 3.13 and 3.14,
  plus `ruff check` and `ruff format --check`, on every push to `main` and
  every pull request. Until now "the tests pass" meant they passed locally,
  and the 3.14 classifier was an unverified assertion.
- An opt-in `pre-push` hook (`.githooks/pre-push`) running the same three
  checks. Enable with `git config core.hooksPath .githooks`.

### Decided

- `safety.disallowed_tools` **will not be built**. `--permission-mode` is the
  boundary; a second overlapping mechanism in config could only disagree with
  it. See `design/03-execution-model.md` §3.3.

## 0.1.0 — 2026-08-01

First release. A local batch queue for Claude Code: SGE-style job files, a
cron tick, 5-hour window accounting, rate-limit requeue, and HTML completion
mail.
