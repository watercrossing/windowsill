# The extra weekly bucket is optional, and its name is read, not assumed

**Settled:** 2026-07-17 (name), 2026-07-28 (absence)

## Context

On 2026-07-17, `/usage` printed a second weekly line, `Current week (Fable)`.
Anthropic's support docs said the separate weekly bucket was Opus-only.

## Decision

- **The bucket name is read from the output.**
  It was Fable, not Opus: the client is ground truth, not the docs.
  No model is hard-coded; `window.extra_bucket` stores whatever name the line carries.
- **The line may be absent entirely** (2026-07-28, in the same output as [reset-minutes-are-optional.md](reset-minutes-are-optional.md)).
  Read the bucket if it is there.
  Its absence is not a parse failure.

## Consequence

`extra_bucket` and `extra_pct` are nullable, and a `/usage` reading without the line parses cleanly.
