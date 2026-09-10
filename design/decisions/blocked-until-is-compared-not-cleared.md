# `sched.blocked_until` is compared against a clock, never trusted as a flag

**Settled:** 2026-08-28

## Context

The same mail as [result-event-is-not-the-last-line.md](result-event-is-not-the-last-line.md) announced "the queue is blocked until 2026-08-21 05:00", on the 28th.

## Decision

`sched.blocked_until` is never cleared when it lapses.
The scheduler stops honouring it by comparison (`blocked_until > now`), and `ccstat` does the same.
But `mail` reads no clock by contract, and it tested `bool(blocked_until)`, so a stale value still read as a block.

The comparison now happens in `runner._run_facts`, against the run's own `finished_at`, and `RunFacts.blocked_until` means "held now".

## Consequence

Anything that reads `sched.blocked_until` must compare it with a time.
A non-null value is not evidence that the queue is blocked.
