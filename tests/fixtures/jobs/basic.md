#!/usr/bin/env windowsill
#$ --name refactor-auth
#$ --at 2026-07-18T02:00
#$ --model claude-opus-4-8
#$ --cwd ~/code/myapp
#$ --resume-from audit-auth
#$ --window fresh
#$ --isolate worktree
#$ --max-turns 60
#$ --retry 2
#$ --notify email

Continue the auth refactor from where the audit job left off.

Done means: every call site of `legacy_verify()` migrated to `verify_token()`,
`pytest tests/auth` green, and a single squashed commit on the branch.
If you hit a blocker, do not stop — write what you tried and what you would
need to `BLOCKED.md`, then continue with the next call site.
