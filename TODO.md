# TODO

Known issues and gaps surfaced by the 2026-09-10 `design/` retirement audit
(`design/retire-instructions-fold-findings`), not fixed there because that
job was documentation-and-citations only.

## Docstrings that disagree with the code

- `runner.reap` docstring says "last event".
- `jobfile._mirrored_model` docstring says the model is also used for anchor
  defaults, but it's display-only.
- `usage.invoke` mentions `doctor`.
- `window.windows_in_last_30d` says "windows *we* opened", though observed
  windows count too.
- `runner.outcome_from_result` says rate limits are "out of scope for this
  module".
- `examples/06-anchor.md` claims the README says `--anchor` implies haiku,
  which it no longer does.

## Behavioural drift

- Dead config keys: `default_model` (already in `design/decisions/open.md`),
  `default_timeout`, `anchor.enabled`/`anchor.time`, `safety.allow_dirty`.
- `expired`, `skipped` and `cancelled` jobs send no notification; the spec
  said `expired` should.
- Name uniqueness is never enforced at submit.
- A relative `--at` gives a traceback rather than a clean error, and two
  job-file test fixtures still use relative forms.
- pid liveness ignores start time, so a recycled pid reads as a live run.
- Nothing warns when `max_parallel` > 1.
- `release` sends a job to `queued`, not `ready`; `hold` accepts `ready`
  jobs.
- Rate limits are detected from the stream only, with the patterns in code
  rather than config.
- Tick probes even when the cache already shows a window open.
- The 10s probe limit applies only to `usage --probe`.
- The warm-lead comment in `config.py` disagrees with the README median.
- `stat` prints "30d sessions:".
- `logs/windowsill.log` is never written.
- The README says `report` prints `ccresume` but it prints `windowsill
  resume`.
- `log` streams raw JSONL, not the rendered transcript.
- The error at `cli.py:540` is garbled.
