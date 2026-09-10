# The reset clause's minutes are optional

**Settled:** 2026-07-28

## Context

Every reset clause seen until then carried minutes (`resets Jul 17, 7:09pm`), and the pattern required them.

## Decision

**Minutes are not always present.**
On the hour, the clause reads `resets Jul 28, 7am (Europe/London)`, with no `:00`.
The minutes are optional in `USAGE_PATTERNS`.

## Consequence

Requiring the minutes made the parser read a live window at 74% used as *closed*.
That is the failure that dispatches `--window fresh` into an open window.

It was observed on the target account while a window was live, and caught only because the parser reported `is_open: False` at 74% used.
The percentage is display only ([reset-clause-is-the-discriminator.md](reset-clause-is-the-discriminator.md)), so nothing else would have noticed.
The same output also lacked the extra weekly bucket ([extra-weekly-bucket-is-optional.md](extra-weekly-bucket-is-optional.md)).
