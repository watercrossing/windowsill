# A `/usage` probe does not open a window

**Settled:** 2026-07-17

## Context

Consulting the oracle is only useful if asking does not itself change the answer.
If `claude -p "/usage"` anchored a window, every probe would spend one of the ~50 a month and move the boundary it was trying to read.

## Decision

**Probing does not open a window.**
Probes at 02:00 and 03:00 into an expired window both read closed.
If the 02:00 probe had anchored a window, the 03:00 one would have shown it live until about 07:00.
The local request counter did not move either.
`/usage` reads metadata and is not an inference call: `num_turns: 0`, `duration_api_ms: 0`.

Each probe does return a `session_id` and may leave a conversation record on disk.
It does not increment the local conversation counter and is otherwise harmless.

## Consequence

The oracle can simply be consulted: probe whenever a dispatch decision depends on window state and the cached `reset_at` is missing or past, and probe before and after each run for the Δ% cost ([window-percent-not-dollars.md](window-percent-not-dollars.md)).
Probes are still cached and rate-limited, for latency and manners rather than correctness, and a cron tick with nothing to dispatch does not probe.

This is a behavioural finding on one client version, not a documented guarantee.
If `/usage` ever starts costing a window, the failure will be silent and expensive.
The design intended `windowsill doctor` to re-run the two-probe check; `doctor` is not implemented.
