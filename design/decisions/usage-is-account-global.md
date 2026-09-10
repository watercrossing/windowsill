# `/usage`'s window and weekly lines are account-global

**Settled:** 2026-07-17

## Context

`/usage` mixes two kinds of number.
The trailing block says of itself that it is "based on local sessions on this machine — does not include other devices or claude.ai".
The question was whether the `Current session` and `Current week` lines above it are local too.

## Decision

**They are account-global.**
On the same night as [probes-are-free.md](probes-are-free.md), the local counters stayed frozen to the digit (140/6, then 165/8, across both probes) while `Current week` moved from 13% to 15%.
That was usage from another surface: invisible to the local block, visible in the top lines.
The `Current session` and `Current week` lines are server-side account truth.

## Consequence

The oracle sees your phone.
`--window fresh` can therefore be verified against global truth: probe first, and if a reset clause comes back, some other surface holds a live window, so don't dispatch.
`--anchor` jobs no-op to `skipped` on the same evidence.

The trailing block is machine-local and counts *conversations*, not windows.
It is never an input to a dispatch decision.
