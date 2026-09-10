# `--allowed-tools` is an additive auto-approve list, not a boundary

**Settled:** 2026-07-31

## Context

`safety.allowed_tools` had just been wired up as `claude`'s `--allowed-tools` flag, on the plausible assumption that it bounds the tool surface.
Two commands were run against `claude` 2.1.218 with no ambient permission rules anywhere: `~/.claude/settings.json` had no `permissions` key, and no project `.claude/settings*.json` existed.

| Question | Answer |
|---|---|
| Does `claude -p "run bash echo ..." --allowed-tools Read` block Bash? | **No.** Bash ran anyway, `permission_denials: []`, exit success |
| Does `claude -p "run bash echo ..." --disallowed-tools Bash` block Bash? | **Yes.** The model reported that Bash was not in its tool set at all |

## Decision

`--allowed-tools` **pre-approves; it is not a sandbox.**
A tool named in it skips its permission prompt.
A tool left out of it is not blocked, not denied, and not even missing from the model's tool set.
`--disallowed-tools` is the flag that actually narrows the surface, and windowsill does not use it ([permission-mode-is-the-boundary.md](permission-mode-is-the-boundary.md)).

## Consequence

The code and docs describing `safety.allowed_tools` were rewritten so they no longer imply a restriction (`runner.build_argv`, `config.DEFAULTS`).

It matters for the default list too.
Because `--allowed-tools` never restricted anything, the old default's omission of `Task` was harmless by accident, not by design.
It was a loaded gun for the day permission prompting actually engages: a `--permission-mode` change, a new CLI default, or a sandboxed environment.
The default was widened to add `Task`, `TodoWrite`, `WebFetch`, `WebSearch` and `NotebookEdit`.
