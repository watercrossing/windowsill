#!/usr/bin/env ccsub
#
# Pipeline, part 1 of 3: build -> test -> report.
#
# This file has no dependencies -- it is the head of the chain, and the
# only thing worth noticing about it is that it is submitted FIRST. The
# other two name it by `--name`, and a name that has never existed
# resolves to nothing, which windowsill reads as "this dependency can
# never be met" and marks the dependent `skipped` on the spot. Submit in
# order:
#
#     ccsub examples/04-pipeline-1-build.md
#     ccsub examples/04-pipeline-2-test.md
#     ccsub examples/04-pipeline-3-report.md
#
# A second thing that follows from names: a name only resolves uniquely
# among jobs that have not finished. Submit this pipeline twice while the
# first copy is still live and `pipeline-build` is ambiguous -- windowsill
# refuses to guess, and the dependents are skipped. Run one pipeline at a
# time, or use the numeric ids `ccsub` prints.
#
# --priority is here because dispatch order among jobs that are all ready
# at once is (priority, id), lowest first, and the default max_parallel is
# 1 -- one job runs at a time. Giving the head of a pipeline priority -5
# means an unrelated job submitted in the meantime does not get in front
# of it and stall everything downstream.
#
#$ --name pipeline-build
#$ --at 2026-08-05T02:00
#$ --cwd ~/code/myapp
#$ --model claude-opus-4-8
#$ --log transcripts/
#$ --priority -5
#$ --permission-mode auto
#$ --timeout 1h

Build the project from a clean tree: `uv sync --frozen` then
`uv run python -m build`.

If the build fails, fix what is broken rather than working around it, and
commit each fix separately. A dependency that has to be pinned or
unpinned counts as a fix; say why in the commit message.

Leave the built artifacts in `dist/` -- the two jobs that follow this one
read them. Finish with the version you built and the filenames in
`dist/`.
