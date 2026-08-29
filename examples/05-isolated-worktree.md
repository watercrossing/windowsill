#!/usr/bin/env ccsub
#
# The job that must not touch the tree you work in.
#
# ---------------------------------------------------------------------
# Read this before you rely on it
# ---------------------------------------------------------------------
#
# `--isolate worktree` parses, is stored on the job, and is printed by
# `windowsill show <id>`. It is NOT honoured yet: the run happens in
# --cwd regardless, and no worktree is created for you. `--allow-dirty`
# is stored and never checked -- nothing refuses to run against a dirty
# tree today, so the flag currently protects nothing and blocks nothing.
#
# That is stated up front because a directive that reads like a
# safeguard and is not one is worse than no directive. Note also that
# `[safety].default_isolate` defaults to "worktree" in the config, so
# every job you have ever submitted already records `isolate: worktree`.
# Do not read that field in `windowsill show` as evidence anything was
# isolated.
#
# So this example does the isolation by hand, which is the only way that
# works today, and keeps the two directives in the header so the file
# needs no edit on the day they start being honoured.
#
# ---------------------------------------------------------------------
# Doing it by hand
# ---------------------------------------------------------------------
#
# Make the worktree yourself, then point --cwd at it:
#
#     cd ~/code/myapp
#     git worktree add ../myapp-audit -b audit/deps
#     ccsub examples/05-isolated-worktree.md
#
# The job now runs in a checkout of its own. Your main tree keeps its
# uncommitted edits, your branch does not move, and a job that decides to
# `git checkout` something cannot do it to the files you have open. When
# you are finished reviewing:
#
#     git -C ~/code/myapp worktree remove ../myapp-audit
#
# Two things to know about the worktree you made: it shares one `.git`
# with the main checkout, so a commit made in it is immediately visible
# from the main tree, and it does not share ignored files -- `.venv/`,
# `node_modules/`, a `.env` -- so a job that needs those has to build
# them, which is worth a line in the prompt.
#
# ---------------------------------------------------------------------
# Where the output goes
# ---------------------------------------------------------------------
#
# Nothing windowsill writes lands in the working tree. Everything the run
# produces about itself goes to the job directory:
#
#     ~/.windowsill/jobs/<id>-<name>/
#         job.md        the file you submitted, verbatim
#         cmd.json      the exact claude argv, so the run is reproducible
#         stream.jsonl  raw stream-json events
#         transcript.md the rendered conversation
#         result.json   the final result event
#         journal.md    the run's own status notes (see 03)
#         resume.sh     cd <cwd> && claude --resume <conversation>
#
# `--log` below copies transcript.md into the project as well, which is
# the one thing you may actually want in git. Point it somewhere ignored
# if you would rather it stayed out.
#
# The only things the job leaves in the tree are the ones its prompt told
# it to leave: in this case commits on `audit/deps` and a report file.
# `ccresume <id>` re-enters the conversation with the worktree as its
# cwd, so a morning follow-up question is answered in the same place the
# work happened.
#
#$ --name audit-deps
#$ --at 2026-08-05T02:00
#$ --cwd ~/code/myapp-audit
#$ --model opus
#$ --log ~/reports/
#$ --isolate worktree
#$ --allow-dirty
#$ --permission-mode auto
#$ --timeout 2h
#$ --notify email

Audit this project's dependencies and leave the findings on this branch.

You are in a git worktree of your own, on `audit/deps`. It does not
share the main checkout's `.venv` or any other ignored file, so build
whatever you need from scratch.

For every direct dependency in `pyproject.toml`: the version we pin, the
current release, whether the gap crosses a major, and whether anything in
our code actually uses the parts that changed. `uv tree` and the
changelogs are the sources; do not guess from version numbers alone.

Write it to `DEPENDENCIES.md` and commit it. Where an upgrade is safe and
small, make it -- one commit per dependency, with the test run that
covers it named in the message. Where it is not, say what would have to
change and leave the pin alone.

Do not merge, do not rebase, and do not touch any branch other than
`audit/deps`. This branch is for reading in the morning.
