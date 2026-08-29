#!/usr/bin/env ccsub
#
# Pipeline, part 2 of 3. This is the file that shows what --hold-jid
# actually does.
#
# --hold-jid names jobs -- by `--name`, or by the numeric id `ccsub`
# printed -- comma-separated. A job with a hold is submitted `queued` like
# any other and is promoted to `ready` only when BOTH conditions hold: its
# --at is due, and every named dependency has reached `done`.
#
# How a hold is released: it isn't, by anyone. There is no release
# command for this and nothing sends a signal. Every `windowsill tick`
# re-evaluates every queued job's dependencies from the current state of
# the queue, so the hold simply stops being true. Cron runs a tick every
# fifteen minutes as a liveness floor, but you rarely wait that long: a
# finishing job kicks a tick itself, so the next stage usually starts
# seconds after the previous one finished.
#
# What happens when the dependency does not reach `done`: if it ends in
# any other terminal state -- failed, cancelled, expired, skipped -- the
# precondition can never be met, so this job goes to `skipped` too rather
# than sitting `queued` forever. A pipeline whose head fails does not
# quietly wait until you look at `ccstat` a week later. Note the flip
# side: a job that ran, achieved nothing and still exited cleanly reads
# `done`, and the hold releases. Holds sequence work; they do not judge
# it.
#
# Fan-out is just several files naming the same dependency. Add a
# `04-pipeline-2b-lint.md` that also holds on `pipeline-build` and both
# become ready together. With the default max_parallel of 1 that means
# both become *eligible*, not both run at once -- they run one after the
# other, in --priority then id order. Fan-in is the comma list, which
# part 3 uses.
#
# Note --at is absent here, deliberately. An --at on a held job is a
# floor, not a start, and setting one here could only delay a stage that
# is already gated on something better. Let the dependency do the timing.
#
# The related directive is --resume-from: it is a dependency in exactly
# this sense *and* a resume source, so `#$ --resume-from pipeline-build`
# would wait for the build in the same way and then continue its
# conversation rather than starting a fresh one. Use --hold-jid when the
# next stage is separate work (as here -- a test run does not want the
# build's context) and --resume-from when it is genuinely the same
# thought continued.
#
#$ --name pipeline-test
#$ --hold-jid pipeline-build
#$ --cwd ~/code/myapp
#$ --model opus
#$ --log transcripts/
#$ --permission-mode auto
#$ --timeout 2h

Install the wheel the build job left in `dist/` into a throwaway
environment and run the test suite against the installed package, not
against the source tree: `uv run --isolated --with dist/*.whl pytest -q`.

The point of testing the artifact rather than the checkout is packaging
bugs -- a module that is not in the wheel, a missing entry point, data
files that were never included. Those pass in the source tree and fail
for anyone who installs.

Fix what you find and commit each fix separately. Finish with the number
of tests run, the failures, and specifically whether any failure was a
packaging problem rather than a code problem.
