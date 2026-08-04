#!/usr/bin/env ccsub
#
# Pipeline, part 3 of 3: the fan-in.
#
# --hold-jid takes a comma-separated list and waits for all of them. This
# job names both earlier stages even though the dependency on the build is
# already implied transitively by the test job. Two reasons that is worth
# the extra word:
#
#   - It is a statement of what this job reads. It reads `dist/` (the
#     build) and the test job's transcript. Naming both makes the file
#     say so.
#   - It survives editing. Take the hold off the test job tomorrow and
#     this one still will not run before the build has finished.
#
# Transitive holds are not free either way -- windowsill evaluates only
# what a job names, not the closure -- so if you want the guarantee, name
# it.
#
# This is also the job to hang --notify on. The two stages before it are
# machinery; this is the one whose result you want in the morning, and it
# is the only one that can see the whole run. Notifications fire on
# terminal states only, so a stage that gets rate-limited and requeued
# stays quiet until it actually finishes.
#
# --priority 5 (higher than the default 0, so it runs later among equals)
# is belt and braces: the hold already sequences it, and priority only
# breaks ties between jobs that are all ready at the same moment. It costs
# nothing and it means an unrelated job that becomes ready at the same
# instant is not stuck behind the report.
#
#$ --name pipeline-report
#$ --hold-jid pipeline-build,pipeline-test
#$ --cwd ~/code/myapp
#$ --model claude-haiku-4-5
#$ --log transcripts/
#$ --priority 5
#$ --permission-mode auto
#$ --timeout 30m
#$ --notify email

Write `RELEASE-NOTES.md` for the artifacts now sitting in `dist/`.

Source material, in order of authority: `git log` since the last tag, the
diff of `CHANGELOG.md`, and the transcripts the build and test jobs left
in `transcripts/`.

Three sections, no more: what changed and why anyone should care, what
broke and was fixed, and anything a user has to do by hand when
upgrading. Skip a section rather than pad it.

Commit the file. Do not tag and do not publish anything -- this job
reports, it does not release.
