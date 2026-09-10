# A denied permission does not fail the run, so windowsill counts denials itself

**Settled:** 2026-07-28

## Context

Three jobs (one Haiku 4.5, one Sonnet 5 spawning a subagent, one Opus 4.8 held on the second) were submitted to a throwaway `--home` and dispatched by ticks run by hand.
Two `Write` calls were refused under `--permission-mode auto`.

## Decision

**A denied permission does not fail the run.**
The envelope still returned `subtype: success` and `is_error: false`, with both refusals listed in `permission_denials`.

## Consequence

A job that was blocked from doing anything reads `done` in `windowsill stat`.
That is the overnight failure this project exists to catch, and the lifecycle's outcome mapping did not look at the field.
So the length of `permission_denials` is recorded per run in `runs.permission_denials`, and it is shown by `windowsill show`, by `windowsill report` (`denied=N`) and in the notification.
It is the only trace of a run that was refused every write.
