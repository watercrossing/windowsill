# Changelog

## 0.2.0 — unreleased

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
