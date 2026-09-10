# Auth survives a non-interactive job with no TTY

**Settled:** 2026-07-17

## Context

Everything windowsill does depends on `claude` authenticating from cron or `at`, where there is no TTY and a near-empty environment.
If it could not, the project was not buildable at all.

## Decision

**Yes, it does.**
`at` + `env -i HOME=$HOME PATH=$PATH claude -p "/usage"` → `exit=0`.
`claude` needs only `HOME` (to find its credentials) and a `PATH` that reaches it.

## Consequence

The project is buildable; this was the one finding that could have killed it.
The README's cron section relies on it: cron sets `HOME` itself, and the crontab only has to supply `PATH`.

The evidence is one machine, one client version, one night.
This is a behavioural finding, not a documented guarantee, and credential handling is exactly the kind of thing a client update changes.
The design intended `windowsill doctor` to re-check it; `doctor` is not implemented.
