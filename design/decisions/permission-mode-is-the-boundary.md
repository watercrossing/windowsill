# The job's `--permission-mode` bounds an unattended run; windowsill has no `disallowed_tools`

**Settled:** 2026-07-31 (finding), 2026-08-04 (closed, by the owner)

## Context

[allowed-tools-is-additive.md](allowed-tools-is-additive.md) showed that `--disallowed-tools` is the `claude` flag that actually narrows the tool surface.
The obvious follow-up was a `safety.disallowed_tools` config key rendered the same way `safety.allowed_tools` is.

## Decision

**Not implemented, and it will not be.**
The tool surface is not this project's boundary to draw.
`--permission-mode` is what bounds an unattended job, and it can already be set per job through pass-through directives.
A second, overlapping mechanism in config would only invite the two to disagree.

## Consequence

An unattended job's blast radius is bounded by its permission mode and by `claude`'s own defaults for a tool it doesn't list.
That is deliberate, not a missing feature.

Whenever a job's own pass-through names any tool-permission flag, windowsill steps out of the way entirely: it injects no `--allowed-tools` at all and logs an `allowed_tools_yielded` event.
The flags that trigger this are listed in `runner.TOOL_PERMISSION_FLAGS`.
The job's own flag always wins.
