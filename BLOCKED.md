# Overnight run notes — 2026-08-05 (todo-5)

**Nothing is blocked.** All five asks are done, committed, and verified.
The todo-4 blocker that used to head this file — the PyPI trusted publisher
still pointing at `watercrossing/ccq` — is **resolved**: you fixed it and 0.2.0
uploaded on 2026-08-04. Nothing on the release path needs you now.

**Seven** commits on `main`, **not pushed**. 613 tests pass, ruff clean, and
the pre-push hook's three checks pass, so the batch is ready when you are.

---

## Added on the second attempt (2026-08-05)

The first attempt of this job ran out of window *after* finishing all five
asks, so a second one picked up the single deliverable it had left behind:
you asked for warming to be added to `examples/`, and it was not, because
warming was still uncommitted when the examples were written.
`examples/07-warm.md` is now there (`276b7d8`).

**Writing it found a bug worth knowing about, now fixed.** `--warm` and
`--window fresh` were a permanent, silent deadlock: warming only dispatches
while a window is *open* (it is waiting to spend that window's tail), `fresh`
only dispatches while a window is *closed*, and the tick applies warming
first. Since no warming refusal is terminal, such a job would have sat `ready`
and quiet forever rather than failing. `ccsub` now refuses the pair at submit.
Job 28 does not use `--window`, so it was never affected.

One consequence for tomorrow: job 28 was queued *before* this, so its copy of
`tasks/windowsill-todo-6.md` still asks for the warm example as its §4. The
repo copy of that file now says so at the top of §4; the job will check,
find it done, and move on.

---

## The one thing to look at first

**Job 28 (`windowsill-todo-6`, queued 2026-08-05 21:00) is the first real use
of `--warm`.** The 21:00 in its header is no longer when it runs — it is when a
throwaway Haiku anchor opens the window. The job itself goes ~30 minutes before
that window resets, around 01:30, so it spends the tail of window one and rolls
straight into window two.

If warming misbehaves the job **sits `ready` rather than running** — it has no
`--before` to expire it, so the failure would be quiet. If you find in the
morning that it never went: `ccdel 28`, delete the `--warm` line, resubmit.

The gate was exercised against a real database before shipping, not just mocks:
no window ⇒ exactly one `warm-nightly` warmer on Haiku with a 5m timeout; no
duplicate on a second tick; holds at 00:29 and dispatches at 00:31 for a 01:00
reset; refuses when only 5% of the window is free. Every decision writes a
plain-English `warm` event, so the audit trail will say what it chose and why.

## Your "is 30 minutes enough?" question — answered from your own ledger

Twelve completed runs carrying both usage probes, on Pro:

| | %/min | 100% of a window takes |
|---|---|---|
| fastest | 5.10 | 20 min |
| **median** | **2.76** | **36 min** |
| slowest | 0.61 | 164 min |

So 30 minutes spends most of a window, not quite all of it. **I left the
default at your 30m** rather than quietly raising it, because the real answer
is that the errors are asymmetric and you should choose knowing that:

- **Lead too long** — the job exhausts window one early and waits for the
  reset. Costs waiting only: a rate-limit requeue consumes no `--retry` and
  resumes the same conversation.
- **Lead too short** — quota you already paid for expires unspent, forever.

Err long. If you want the median covered, set `lead = "40m"` in `[warm]`. And
note that **Max needs a longer lead, not a shorter one** — more tokens per
window at the same five hours means a lower %/min.

## What changed for you, day to day

- **The summary mail now covers the whole job.** todo-3 would have arrived as
  `(done, 3 runs)`, carrying both rate-limited attempts' own closing words and
  `10h 11m wall / 1h 07m running` — the pair of numbers that makes a ten-hour
  stall visible. Budget is reported in window %, because that is the only cost
  unit in the database; the mail says outright that it is a floor rather than
  pretending to be a token count.
- **`on_rate_limit = true` is now set in `~/.windowsill/config.toml`.** A
  paused run sends one short line: when it resumes, the usage numbers, and the
  fact that the pause cost no retry. That is todo-3's ten-hour silence, closed.
  It is global config rather than a per-job directive, since it is a statement
  about how *you* want to be told; if you want per-job control, a
  `#$ --notify-on-limit` override is a small addition.
- **`examples/`** — eight runnable blueprints, each mostly comment. All were
  submitted to a throwaway `--home` to prove they parse, queue and produce the
  argv they claim.

## Three things the docs promised that the code never did

Found by writing `examples/` against `jobfile.py` instead of against the prose.
The docs now tell the truth; the code fixes are queued in todo-6.

1. **`--before` is a real bug.** `--at` is normalised to UTC; `--before` is
   stored verbatim and then compared as a *string* against UTC. A `--before`
   written in local time expires at the wrong moment.
2. **`--at tomorrow 02:00` and `--at +3h`** were documented in two places and
   fail the submit — `_normalize_at` is bare `fromisoformat`.
3. **`--anchor` never implied Haiku.** `[anchor].model` had no reader at all
   until window warming gave it one.

Also dead config, now written down rather than shrugged at: `[anchor].enabled`
and `[anchor].time` (nothing schedules a daily anchor — that is a cron line),
and `[queue].default_model` (never read).

## Ready when you are

`CHANGELOG.md` has a written `Unreleased` section covering all of the above.
0.3.0 is a version bump, a date and a tag whenever you want it — todo-6 asks
for it once warming has proven itself for a night.
