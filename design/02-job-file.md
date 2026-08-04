# 2. The job file

A job is a UTF-8 text file: a directive header, a blank line, then the prompt body (Markdown, free-form, any length).

```
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
```

## 2.1 Parsing rules

1. Directives are lines matching `^#\$\s+(.*)$`, appearing **before** the first non-directive, non-blank, non-shebang line. A header terminated by prose cannot resume later in the file.
2. A leading `#!` line is ignored, so job files can be `chmod +x` and self-submit.
3. Everything from the first blank line after the header to EOF is the body, **verbatim**. No templating, no substitution, no escaping. What you type is what the model gets.
4. `#$ #...` and bare `#` lines inside the header are comments.
5. A trailing `\` continues a directive onto the next line.
6. Tokenise directive arguments with `shlex.split`, never a bare `.split()`.

## 2.2 Directive namespace — the central design rule

Two kinds of directive, distinguished by lookup rather than syntax:

- **Scheduler directives** are consumed by `windowsill` and never reach the CLI ([§2.3](02-job-file.md)).
- **Everything else is passed through verbatim to `claude`**, in order, as argv.

`#$ --model claude-opus-4-8` becomes `claude --model claude-opus-4-8`. `#$ --max-turns 60` becomes `--max-turns 60`. `windowsill` never needs to know what those mean, so a new Claude Code flag works on day one with no parser change.

Non-negotiable consequences:

- `windowsill` MUST NOT validate or reject unknown directives. Pass them through; let `claude` complain.
- Pass-through args are argv elements, never a concatenated shell string. No `shell=True`. Ever.
- Where a scheduler directive shadows a real `claude` flag, the scheduler wins and it must be documented. That set is currently exactly one: `--resume` ([§2.3](02-job-file.md)), which needs late resolution.
- `--model` is pass-through, but is *also* mirrored into the `jobs.model` column for display and anchor defaults. Mirror, don't intercept.

## 2.3 Scheduler directives (complete set)

| Directive | Default | Meaning |
|---|---|---|
| `--name <slug>` | filename stem | Human label. Unique only among non-terminal jobs |
| `--at <when>` | ASAP | Earliest start. ISO-8601 only today; the relative forms (`tomorrow 02:00`, `+3h`) are unimplemented and fail the submit |
| `--before <time>` | none | Do not start after this; job becomes `expired` if missed. Compared as a string against UTC, so write it in UTC |
| `--window <policy>` | `share` | `fresh` \| `share` \| `offpeak` ([§5.1](05-windows.md)) |
| `--anchor` | false | This is an anchor job ([§5.2](05-windows.md)). Does **not** imply a model today — `[anchor].model` is read only when the scheduler spawns a warmer ([§5.5](05-windows.md)) |
| `--cwd <path>` | submit-time cwd | Working directory. `~` expanded at submit |
| `--log <path>` | job dir only | Also write the rendered `transcript.md` here. A path ending in `/`, or an existing directory, gets `<id>-<name>.md` inside it; anything else is the file itself. `~` expanded at submit, relative paths resolved against `--cwd` at write time |
| `--resume <conversation_id>` | — | Continue an explicit conversation |
| `--resume-from <job>` | — | Continue the conversation produced by a completed job (resolved at dispatch) |
| `--hold-jid <job>[,<job>]` | — | Wait for those jobs to reach `done` |
| `--retry <n>` | `0` | Retries on non-rate-limit failure. Rate-limit requeues are free and unbounded ([§5.4](05-windows.md)) |
| `--priority <-20..19>` | `0` | Tie-break within a window; lower runs first |
| `--isolate <mode>` | config | `none` \| `worktree` |
| `--allow-dirty` | false | Permit running against a dirty tree |
| `--notify <channel>[:<dest>]` | config (`none`) | `none` \| `ntfy` \| `email`, optionally carrying this job's own recipient/topic — `email:me@example.com` ([§8.4](08-interface.md)) |
| `--hold` | false | Submit `held`; needs `windowsill release` |
| `--timeout <dur>` | `4h` | Wall-clock kill |
| `--no-resume-on-limit` | false | On rate-limit requeue, restart from the original body instead of resuming ([§5.4](05-windows.md)) |
| `--warm` | false | Open the window at `--at`, but hold the job until near the window's end ([§5.5](05-windows.md)) |
| `--warm-lead <dur>` | config (`30m`) | Per-job override of `[warm].lead` ([§5.5](05-windows.md)) |

`--resume` and `--resume-from` are mutually exclusive. Both produce `claude --resume <id>` at dispatch.
