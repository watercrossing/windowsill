#!/usr/bin/env ccsub
#
# The second job everybody writes: something useful, at a time you are
# asleep, in a directory that is not wherever your shell happened to be.
#
# Why each directive:
#
#   --at    is the earliest start, not the start. The job goes to `queued`
#           and only becomes `ready` at a tick on or after this time; it
#           then dispatches when a window policy allows it. Write it as
#           ISO-8601 -- `2026-08-05T02:00`. windowsill parses `--at` with
#           `datetime.fromisoformat` and normalises it to UTC at submit, so
#           a bare local wall-clock is read in your machine's zone, which
#           is what you meant. (The README also lists `tomorrow 02:00` and
#           `+3h`. Those do not parse today and will fail the submit -- use
#           ISO.) An --at already in the past is simply "as soon as
#           possible", so a stale example file still runs.
#
#   --cwd   is where `claude` is started. Without it, the job runs wherever
#           your shell was when you typed `ccsub`, which for a job that
#           runs six hours later is a coin toss. `~` is expanded at submit.
#           This is also the directory `ccresume <id>` puts you back in.
#
#   --log   copies the rendered transcript out of the job dir and into your
#           project. A trailing `/` means "in that directory", written as
#           `<id>-<name>.md`; anything else is taken as the filename. A
#           relative path resolves against --cwd, not against windowsill's
#           cwd -- cron leaves that somewhere unhelpful.
#
#   --notify names a channel. It fires once, on a terminal state only: a
#           job that was rate-limited and requeued has not finished and
#           does not mail you. Notifications are off until configured and
#           windowsill will not invent an address: `--notify email` with no
#           `[notify.email].to` in ~/.windowsill/config.toml sends nothing
#           and records a `notify_skipped` event rather than mailing a
#           guess. To carry the address in the job instead, write
#           `#$ --notify email:you@your-real-domain`.
#           `windowsill show <id>` prints the address it would actually
#           reach, which is the thing to check before trusting this.
#
#   --permission-mode is pass-through to `claude`, not a windowsill
#           directive -- but windowsill does read it. A job that names any
#           permission flag of its own gets it untouched, and windowsill
#           does not add its `safety.allowed_tools` default on top. Your
#           flag always wins. Note that `auto` still refuses some writes;
#           a job that was blocked from doing anything still reads `done`,
#           so check the run, not just the state.
#
# "Nightly" is your cron's job, not windowsill's. There is no repeat
# directive: a job file is submitted once and runs once. For a real
# nightly, have cron submit the file and let --at be a time later the same
# night, or drop --at entirely and submit at the hour you want it queued:
#
#     5 22 * * *  ccsub ~/code/myapp/jobs/nightly-tests.md
#
# Submit:  ccsub examples/02-nightly-tests.md
# Watch:   ccstat -a         (terminal jobs from the last 7 days)
#          windowsill log <id> -f
#
#$ --name nightly-tests
#$ --at 2026-08-05T02:00
#$ --cwd ~/code/myapp
#$ --model claude-opus-4-8
#$ --log transcripts/
#$ --permission-mode auto
#$ --notify email

Run the full test suite (`uv run pytest -q`) and deal with what it finds.

For every failure: work out whether the test is wrong or the code is
wrong, fix whichever it is, and commit that fix on its own. One commit per
failure, message explaining the cause rather than the symptom.

Do not touch a test to make it pass unless you can say, in the commit
message, why the assertion was wrong.

Finish with a summary of what failed, what you changed, and anything you
decided not to fix and why. If the suite was already green, say so and
stop -- do not go looking for work.
