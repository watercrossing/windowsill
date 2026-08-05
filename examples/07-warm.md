#!/usr/bin/env ccsub
#
# The job that wants more than one window's worth of quota, and gets most
# of two out of a single scheduled time. Read `06-anchor.md` first if the
# five-hour window model is not already familiar -- this file assumes it.
#
# ---------------------------------------------------------------------
# The waste that warming removes
# ---------------------------------------------------------------------
#
# A window starts at your FIRST request and runs five hours. Dispatch a
# job at 22:00 and it owns 22:00-03:00: it opens the window by running,
# and it is the only thing in there. If the work is bigger than one
# window's allowance, it hits a rate limit somewhere around 22:40, and
# then waits -- idle, holding the queue -- until 03:00 for the next one.
#
# Nothing was wrong with that. It is just that the window's clock and the
# job's clock were welded together, and they did not have to be. `--warm`
# unwelds them:
#
#         22:00                                 02:30      03:00        08:00
#   plain   |========= job runs ====|--- rate-limited, waiting ---|== job ==>
#   warm    A---------- idle -------------------[== job ==>|=== job ====== ...
#           ^ warmer opens window 1              ^ tail      ^ window 2, free
#
# Same one `--at`. The warmer opens window one at 22:00 and stops; the
# job sleeps through the body of that window and starts 30 minutes before
# it resets, spending what is left, crossing the boundary, and continuing
# into a whole fresh window two. Close to two windows of quota, one
# scheduled time, and the waiting happens while you are asleep instead of
# in the middle of the run.
#
# The trade is real and worth stating: you deliberately throw away the
# 22:00-02:30 body of window one. That is the point -- the alternative is
# not "use it too", it is "start at 02:30 with nothing behind you".
#
# ---------------------------------------------------------------------
# What actually happens, tick by tick
# ---------------------------------------------------------------------
#
# 1. At `--at`, if NO window is open, the scheduler inserts an ordinary
#    anchor job called `warm-<this job's name>` -- so `warm-nightly-audit`
#    below -- and defers this one. The warmer is a real queued job you can
#    see in `ccstat -a`; it uses `[anchor].model` and `[anchor].body` from
#    ~/.windowsill/config.toml and gets a hard 5-minute timeout. Because
#    it is a queued job rather than an inline call, the window opens on
#    the NEXT tick: up to one cron floor (15 minutes) after `--at`. Aim
#    `--at` early rather than exactly.
#
#    If a window is ALREADY open at `--at` -- you were working at 21:40 --
#    no warmer is spawned at all. There is nothing to open. The job simply
#    aims at the tail of the window you already have, which is the same
#    outcome by a shorter route.
#
# 2. Every tick after that, the job is deferred with a reason recorded as
#    a `warm` event, until `now >= reset_at - lead`. `windowsill show <id>`
#    prints them in plain English ("waiting for the window tail at ...").
#    Long silences in the log here are the feature working.
#
# 3. At the tail, one more check: a LIVE /usage probe, not a cached one.
#    If less than `[warm].min_pct_free` (20%) of the window is left --
#    something else ate it while the job waited -- it defers again rather
#    than starting work that gets thirty seconds before a rate limit.
#
# 4. Then it dispatches, and from there it is an ordinary run. Crossing
#    the reset is a rate-limit requeue like any other: free, unbounded,
#    no `--retry` consumed, and resumed into the same conversation. That
#    mechanism is `03-long-refactor.md`; warming just arranges to hit it
#    on purpose and at a chosen moment.
#
# ---------------------------------------------------------------------
# NOTHING HERE IS TERMINAL, which is the failure mode to know about
# ---------------------------------------------------------------------
#
# Every refusal above is a defer. A warm job that never gets its window
# does not fail, does not skip, and does not mail you -- it sits `ready`
# and quiet. That is the correct behaviour (tomorrow's window is a fine
# time to run) but it means a broken warm job looks exactly like a
# patient one. Two habits:
#
#   - `ccstat` in the morning, and `windowsill show <id>` if it is still
#     `ready`. The `warm` events say which gate held it and why.
#   - Give it a `--before` if the work is worthless once it is late, so
#     it expires loudly instead of waiting forever. Write it in UTC with
#     the `+00:00` offset -- see `03-long-refactor.md` on why.
#
# `--warm` and `--window fresh` CANNOT be combined: warming waits for an
# open window to spend the tail of, `fresh` waits for no window at all,
# so the pair would defer forever. `ccsub` rejects it at submit rather
# than letting you find out overnight. A warm job does not need `fresh`
# anyway -- window two arrives empty by construction.
#
# ---------------------------------------------------------------------
# Choosing the lead, which is the only number worth tuning
# ---------------------------------------------------------------------
#
# `lead` is written as a time but it is really a QUOTA BUDGET: "how much
# of window one do I want to spend before the reset". Minutes are only
# the unit it is expressed in, and the exchange rate is your plan and
# your workload.
#
# On the Pro plan this queue's own ledger puts the median burn at
# 2.76 %/min -- about 36 minutes to spend a whole window, with the
# fastest run measured at 5.10 %/min (20 minutes) and the slowest at
# 0.61. So the default 30m spends most of a window, not all of it.
#
# The errors are not symmetric, so err LONG:
#
#   - too long  -> the job hits the limit early and waits for the reset.
#                  Costs nothing but wall-clock, and no retry.
#   - too short -> the window resets with quota still in it. That quota
#                  is gone permanently; windows are not banked.
#
# A Max plan buys more tokens per window at the same five-hour length, so
# it needs a LONGER lead, not a shorter one. Raise `[warm].lead` in
# config for every job, or set `--warm-lead` on the one job that differs:
#
#     [warm]
#     lead         = "30m"
#     min_pct_free = 20
#
# design/05-windows.md §5.5 has the reasoning and the measurements.
#
# ---------------------------------------------------------------------
# When NOT to use this
# ---------------------------------------------------------------------
#
# Warming spends two windows to run one job. If the work fits in one
# window, `--warm` makes it finish later and costs you a window you could
# have used tomorrow -- use a plain `--at`. It earns its place only for
# work that was going to hit a rate limit anyway, and it pairs with a
# body written to survive the crossing: a journal to read, and a
# definition of done the job can check rather than remember.
#
# It also spends a 30-day window against the ceiling for the warmer's
# sake. At the ceiling, warming declines (the warmer is an anchor, and
# anchors are discretionary) and the job defers -- see `06-anchor.md`.
#
# Submit:  ccsub examples/07-warm.md
# Watch:   ccstat -a                (the `warm-nightly-audit` warmer too)
#          windowsill show <id>     (the `warm` events, in order)
#
#$ --name nightly-audit
#$ --at 2026-08-05T22:00
#$ --cwd ~/code/myapp
#$ --model claude-opus-4-8
#$ --log transcripts/
#$ --warm
#$ # --warm-lead 45m
#$ --timeout 6h
#$ --retry 1
#$ --permission-mode auto
#$ --notify email

Audit every module under `src/` for functions that take a raw `dict` of
options where a typed dataclass already exists, and convert them.

Work on the branch `chore/typed-options`, creating it from `main` if it
does not exist. One commit per module, with `uv run pytest` green before
each. Do not convert a call site whose dict is built from user input at
runtime -- note those in the journal instead and move on.

This job is expected to cross a usage-window boundary partway through and
be resumed. Keep the journal current enough that a restart with none of
this conversation could pick up the next module without redoing one, and
never rewrite or force-push commits an earlier attempt made.
