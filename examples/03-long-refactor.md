#!/usr/bin/env ccsub
#
# The job that might not finish in one sitting, and what windowsill does
# about it. This is the example with the subtle part in it, so the comment
# is long; the directives are only six.
#
# ---------------------------------------------------------------------
# Two different endings, two different mechanisms
# ---------------------------------------------------------------------
#
# A run can stop for two unrelated reasons, and windowsill treats them as
# unrelated:
#
#   1. It FAILED -- non-zero exit, killed, hit --timeout. That is what
#      --retry counts. On a retry the job goes back to `ready` with
#      retry_count+1 and is re-dispatched as a BRAND NEW CONVERSATION with
#      the ORIGINAL BODY. Nothing of the first attempt is in its context.
#
#   2. It RAN OUT OF WINDOW -- a rate limit. That is not a failure and
#      never consumes a retry. windowsill puts the job back to `ready`
#      with its retry count untouched, records the conversation it got as
#      far as, and sets a queue-wide `blocked_until` so the next tick does
#      not walk the next job into the same wall. When the block expires,
#      the job is re-dispatched as `claude --resume <conversation>` with
#      the body replaced by "Continue where you left off."
#
# So: --retry 2 does not mean "survives two rate limits". Rate-limit
# requeues are free and unbounded; --retry is for the crash.
#
# There is a third ending, `incomplete` -- the run left background work
# live. That consumes no retry either, deliberately: re-running the body
# from the top would start a second copy of work whose first copy may
# still be running. It lands in `blocked` and wants a human.
#
# ---------------------------------------------------------------------
# --no-resume-on-limit, and why you might want it
# ---------------------------------------------------------------------
#
# Resuming is the default because it is usually right: the conversation
# already knows what it did. But resuming re-pays for the whole
# conversation every time. At >150k context a single turn has been
# measured eating 15% of a window here. A job resumed across three windows
# can cost more than three separate jobs doing a third of the work each.
#
# `#$ --no-resume-on-limit` opts out: on a rate-limit requeue the job
# restarts from the original body in a fresh conversation -- the same
# shape as a retry, but still without consuming a retry. Use it when the
# body is written to be re-runnable ("continue until X is true"), and skip
# it when the work only makes sense as a continuation. It is not set below
# because this body is a "keep going until done" body and *is* re-runnable
# -- see the last paragraph of the prompt. Uncomment it and the two cheap
# fresh restarts replace one expensive resumed one.
#
# ---------------------------------------------------------------------
# The journal is what makes a restart not start over
# ---------------------------------------------------------------------
#
# Both fresh-context paths above -- a --retry and a --no-resume-on-limit
# requeue -- would otherwise throw away an entire window's work. They do
# not, because windowsill appends a system prompt to every non-anchor job
# asking it to keep a short status journal (Goal / Done / Next / Notes) at
#
#     ~/.windowsill/jobs/<id>-<name>/journal.md
#
# It lives in the job dir, not in your working tree, so it never pollutes
# the branch you are reviewing in the morning and cannot be committed by
# accident -- and because the job dir is keyed on job id, it survives
# every re-dispatch of that job.
#
# On a re-execution windowsill *knows* the context is gone (a prior run
# exists and this dispatch carries no resume id) and adds one more line to
# the system prompt: which attempt this is, how the last one ended, and
# "read the journal first and continue from Next. Do not start over."
# That line is never added to a first run, and never to a resumed run --
# a resumed agent already has the history and does not need to be told to
# read a file about it.
#
# The journal costs real context (every update is a Write whose call and
# result stay in the conversation), so the prompt rations updates and
# `[prompt].journal = false` in ~/.windowsill/config.toml turns it off for
# jobs that always finish in one go. Leave it on for this one: it is
# insuring against losing a whole night.
#
# ---------------------------------------------------------------------
# The directives
# ---------------------------------------------------------------------
#
#   --window fresh   holds the job until a /usage probe reports no live
#                    window, so it owns a whole one rather than inheriting
#                    the 40 minutes left on yours. `share` (the default)
#                    dispatches any time; `offpeak` dispatches outside the
#                    configured peak band. If the probe cannot confirm a
#                    closed window, `fresh` fails safe and waits.
#
#   --before         a deadline, not a duration: a job still queued at
#                    this time becomes `expired` rather than starting six
#                    hours of work while you are trying to use the
#                    machine. Write it in UTC with the `+00:00` offset, as
#                    below. Unlike --at, --before is stored exactly as you
#                    type it and compared as a string against UTC
#                    timestamps, so a bare local wall-clock is off by your
#                    UTC offset and an offset other than +00:00 compares
#                    wrong. UTC is the form that behaves.
#
#   --timeout 4h     the wall-clock kill. It is a failure, so it consumes
#                    a retry -- which is the point: the retry restarts
#                    with a journal to read.
#
#   --retry 2        two crashes tolerated. Three attempts total.
#
# And the honest advice, which the design docs put more bluntly than this
# example does: scope a job to finish inside one window. Everything above
# is a safety net for the job that overran by ten minutes, not a plan for
# the job that was always going to take twelve hours.
#
# Submit:  ccsub examples/03-long-refactor.md
# Watch:   windowsill show <id>     (state, attempts, resolved argv)
#          cat ~/.windowsill/jobs/<id>-*/journal.md
#
#$ --name refactor-auth
#$ --at 2026-08-05T01:30
#$ --before 2026-08-05T05:00:00+00:00
#$ --cwd ~/code/myapp
#$ --model opus
#$ --log transcripts/
#$ --window fresh
#$ --timeout 4h
#$ --retry 2
#$ --permission-mode auto
#$ --notify email
#$ # --no-resume-on-limit

Migrate every call site of `legacy_verify()` to `verify_token()`.

Work on the branch `refactor/verify-token`, creating it from `main` if it
does not exist. One commit per call site or per closely related group,
with the test run that covers it named in the commit message.

Done means: `rg 'legacy_verify\(' src/` returns nothing outside
`src/auth/legacy.py` itself, `uv run pytest tests/auth` is green, and the
branch is left unmerged for review.

This job may be restarted with none of this conversation in its context.
Before you touch anything, check the branch and the journal for what a
previous attempt already did, and continue from there rather than
redoing it. Never rewrite or force-push commits an earlier attempt made.
