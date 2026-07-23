# ccq

A local batch queue for Claude Code.

## The problem

The binding constraint on a Claude Code subscription usually isn't the weekly cap — it's the 5-hour usage **window**.
A window starts on your first message and lasts 5 hours; whatever's left when you stop working is gone, not banked.
Most of that headroom evaporates overnight while you sleep.

`ccq` is a cron-driven queue that submits Markdown job files — prompt bodies with a small directive header — and dispatches them into windows you'd otherwise waste, up to a monthly ceiling you configure.
You write jobs before bed; you run `ccq resume <id>` in the morning to pick a conversation back up.

A quick note on words this project is careful about, since Claude Code overloads "session":

| Concept | Called | Where you meet it |
|---|---|---|
| The 5-hour billing segment | **window** | `/usage`'s "Current session: N% used"; the monthly benchmark |
| A Claude Code conversation with a `session_id` | **conversation** | `--resume`, `~/.claude/projects/` |
| One execution attempt of a queued job | **run** | `ccq` internals only |

## Install

```
uv tool install .
```

Or try it without installing:

```
uvx --from . ccq
```

## The job file

A job is a UTF-8 text file: an optional shebang, a header of `#$` directives, a blank line, then the prompt body — verbatim, no templating, any length.

```
#!/usr/bin/env ccq
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

Parsing rules that matter:

- The `#!` line, if present, is ignored — job files can be `chmod +x` and self-submitted.
- Directives are lines matching `^#\$\s+(.*)$`, and must appear before the first non-directive, non-blank, non-shebang line.
  Once prose starts, the header is closed — a `#$` line can't resume later in the file.
- The first blank line after the header ends the header.
  Everything after it, to EOF, is the body: verbatim, unmodified.
- Bare `#` lines and `#$ #...` inside the header are comments.
- A trailing `\` continues a directive onto the next line.
- Directive arguments are tokenised with shell-like quoting rules (so `--name "has spaces"` works as you'd expect).

**Two kinds of directive.**
`ccq` recognises a fixed set of *scheduler* directives (below); everything else is passed through verbatim, in order, to `claude` as argv.
`#$ --model claude-opus-4-8` becomes `claude --model claude-opus-4-8`.
This means a new `claude` flag works the day it ships, with no change to `ccq` — and `ccq` never validates or rejects a directive it doesn't recognise; unknown ones flow straight through and `claude` complains if they're wrong.

### Scheduler directives

| Directive | Default | Meaning |
|---|---|---|
| `--name <slug>` | filename stem | Human label. Unique only among non-terminal jobs |
| `--at <when>` | ASAP | Earliest start. ISO-8601, `tomorrow 02:00`, or `+3h` |
| `--before <time>` | none | Do not start after this; job becomes `expired` if missed |
| `--window <policy>` | `share` | `fresh` \| `share` \| `offpeak` |
| `--anchor` | false | This is an anchor job. Implies `--model claude-haiku-4-5` unless overridden |
| `--cwd <path>` | submit-time cwd | Working directory. `~` expanded at submit |
| `--resume <conversation_id>` | — | Continue an explicit conversation |
| `--resume-from <job>` | — | Continue the conversation produced by a completed job (resolved at dispatch) |
| `--hold-jid <job>[,<job>]` | — | Wait for those jobs to reach `done` |
| `--retry <n>` | `0` | Retries on non-rate-limit failure. Rate-limit requeues are free and unbounded |
| `--priority <-20..19>` | `0` | Tie-break within a window; lower runs first |
| `--isolate <mode>` | config | `none` \| `worktree` |
| `--allow-dirty` | false | Permit running against a dirty tree |
| `--notify <channel>` | config | `none` \| `ntfy` \| `email` |
| `--hold` | false | Submit `held`; needs `ccq release` |
| `--timeout <dur>` | `4h` | Wall-clock kill |
| `--no-resume-on-limit` | false | On rate-limit requeue, restart from the original body instead of resuming |

`--resume` and `--resume-from` are mutually exclusive; both end up as `claude --resume <id>` at dispatch.
`--resume` is the one case where a scheduler directive shadows a real `claude` flag.

This README describes the full job-file format as designed.
Everything above is parsed today; how much of it is actually *acted on* at dispatch time depends on what's landed so far in this milestone — see below for the commands that currently work end to end.

## Commands

Three commands you'll use constantly:

- **`ccq sub <file>`** — submit a job.
  Use `-` to read from stdin.
  Prints the job id.
- **`ccq stat`** — see what's queued, running, held, or recently finished: id, name, state, model, when, cwd, elapsed.
- **`ccq resume <id>`** — the morning-after command.
  `exec`s into `claude --resume <conversation_id>` in the job's working directory, so you drop straight back into last night's conversation.

Working today (milestone M1):

| Command | Behaviour |
|---|---|
| `ccq sub <file>` | Submit. `-` reads stdin. Prints job id. `--now` bypasses `--at` |
| `ccq stat` | id, name, state, model, when, cwd, elapsed |
| `ccq stat -a` | Include terminal jobs from the last 7 days |
| `ccq show <id>` | Directives, resolved argv, state, conversation id, transcript tail |
| `ccq log <id> [-f]` | Stream the transcript; `-f` follows a running job |
| `ccq del <id>` | Cancel; SIGTERM the process group if running |
| `ccq hold <id>` / `ccq release <id>` | Toggle held |
| `ccq resume <id>` | `exec` into `claude --resume <conversation_id>` in the job's cwd |
| `ccq tick` | Internal; cron and chain entry point |

Designed but not implemented yet — do not rely on these:

| Command | Intended behaviour |
|---|---|
| `ccq sub -e` | `$EDITOR` on a template header; submit on save |
| `ccq report [--since 24h]` | Digest of what ran overnight |
| `ccq usage` | Cached `/usage` window state, `--probe` for a fresh read; also adds window state / 30d count to `ccq stat` |
| `ccq gc [--older-than 30d]` | Remove terminal job dirs and worktrees |
| `ccq doctor` | Environment/config sanity check |

Correspondingly, anything that depends on those — window accounting against a monthly ceiling, anchor-job scheduling, retry-on-failure, and email/ntfy notifications — is part of the design but not yet functional.
Treat directives like `--retry`, `--notify`, and `--anchor` as accepted and stored, not yet enforced.

See `DESIGN.md` for the full design, including the execution model, window accounting, and SQLite schema.
