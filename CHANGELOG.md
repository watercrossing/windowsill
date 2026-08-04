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

### Changed

- GitHub Actions bumped to the Node 24 majors (`checkout` v4→v7, `setup-uv`
  v5→v9, `setup-python` v5→v7). Every run was warning that Node 20 was
  deprecated and that it was being force-upgraded, which is not a supported
  configuration.

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
