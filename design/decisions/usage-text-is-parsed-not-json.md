# `/usage` output is parsed as text; `--output-format json` does not structure it

**Settled:** 2026-07-17

## Context

If `--output-format json` gave `/usage` a structured shape, the fragile regex parse could be avoided.

## Decision

**It does not.**
It wraps the same human-readable string in the envelope's `result` field.
The text still has to be parsed, and it is treated as hostile: every pattern lives in `usage.USAGE_PATTERNS`, because this string will change.

## Consequence

The regex parser stays.
The envelope did confirm the result-event field names (`session_id`, `is_error`, `subtype`, `num_turns`, `duration_ms`, `usage`) that `runner` reads from a real run.
