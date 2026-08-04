#!/usr/bin/env ccsub
#
# The anchor: a job whose only purpose is the clock. Nothing it produces
# matters. If the idea of that seems mad, the window model is the part to
# read first.
#
# ---------------------------------------------------------------------
# The 5-hour window
# ---------------------------------------------------------------------
#
# A Claude subscription meters in windows. A window starts at your FIRST
# request and lasts five hours; you get a fixed allowance inside it, and
# whatever is left when it ends is gone -- not banked, not carried. Your
# usage is therefore chopped into five-hour blocks whose boundaries you
# set by accident, with the first thing you happen to type.
#
# Say you answer a message at 09:15. That opens 09:15-14:15. The next
# window cannot start until 14:15, so a serious afternoon session at
# 14:30 gets 14:30-19:30 and your evening straddles a boundary you set
# with a one-line reply five hours earlier. Boundaries land where they
# land, and it is usually the wrong place.
#
# An anchor moves the boundary. Fire a trivial request at 07:00 and the
# window is 07:00-12:00: it resets over lunch, and the afternoon gets a
# clean 12:00-17:00. Same allowance, better alignment. Nothing is
# created; the seam is just somewhere you chose.
#
# ---------------------------------------------------------------------
# Why this is a directive and not just an early job
# ---------------------------------------------------------------------
#
# An anchor DOES NOT CREATE CAPACITY. A window opened at 01:30 and slept
# through is not harvested capacity, it is a window spent for nothing.
# Two consequences windowsill takes seriously:
#
#   - Any dispatched job is already its own anchor. A real job at 01:30
#     opens the window by running. Firing a ping first to open a window
#     the job would have opened anyway is pure waste, so there is no
#     anchor logic anywhere on the ordinary dispatch path. If you have
#     work to run, run the work; you do not need this file.
#
#   - A scheduled anchor earns its place only when NO job wants that
#     window and you still want the boundary moved. The realistic case
#     is exactly one: a morning anchor, like the 07:00 below.
#
# So why not a cron line at 07:00 that pipes a word into claude? Because
# a fixed time is fragile in the one way that matters. Answer a message
# at 23:20 and you have a window running to 04:20; a 01:30 cron ping
# lands inside it, opens nothing, and silently breaks the alignment you
# scheduled it for -- while still counting as usage.
#
# `--anchor` is what fixes that. windowsill probes /usage first (free,
# and it answers before dispatch rather than after), and if ANY window is
# already open -- including one you opened from your phone -- the job
# no-ops to `skipped`. A skipped anchor in `ccstat -a` is the anchor
# working, not failing: there was no boundary left to move. Conditional
# firing is the whole reason an anchor is safe to schedule at all.
#
# Anchors are also the first thing given up when the 30-day window count
# is at the configured ceiling: an anchor is discretionary, a job you
# queued is not. It skips with the reason recorded rather than eating
# your last window of the month.
#
# ---------------------------------------------------------------------
# The directives
# ---------------------------------------------------------------------
#
#   --anchor   makes it conditional, as above. It also suppresses
#              windowsill's appended system prompt and journal entirely,
#              which is right: injecting 250 tokens of unattended-
#              operation instructions into a one-word ping is absurd.
#
#   --model    is set explicitly here on purpose. The README says
#              `--anchor` implies claude-haiku-4-5 unless overridden;
#              that implication is documented but not implemented, so a
#              model you do not name is whatever `claude` defaults to.
#              Name it. Haiku both for cost and because it does not touch
#              the weekly buckets.
#
#   --timeout  a ping that has not answered in a minute is not going to.
#
#   --notify none  because an anchor has nothing to report and the
#              config default may well be email. Do not get mailed at
#              07:00 every day to be told the word "ok".
#
# There is no recurrence directive: a job file is submitted once and runs
# once. A daily anchor is a cron line that submits this file, and the
# --at above only matters if you submit it the night before:
#
#     50 6 * * *  ccsub ~/jobs/anchor.md
#
# (There is an `[anchor]` table in ~/.windowsill/config.toml with
# `enabled`, `time`, `model` and `body`. Nothing reads it yet -- the
# automatic daily anchor is designed, not built. Submit the file.)
#
# Keep at most one scheduled anchor alive at a time. Two of them race for
# the same boundary and the second one skips, which is harmless but means
# one of the two never does anything.
#
# Submit:  ccsub examples/06-anchor.md
# Check:   ccstat -a         (look for `done` -- or `skipped`, which is
#                             also the right answer)
#
#$ --name morning-anchor
#$ --at 2026-08-05T07:00
#$ --anchor
#$ --model claude-haiku-4-5
#$ --timeout 1m
#$ --notify none

Reply with the single word: ok
