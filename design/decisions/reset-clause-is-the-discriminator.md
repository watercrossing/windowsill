# The reset clause, not the line or the percentage, says whether a window is open

**Settled:** 2026-07-17

## Context

The obvious assumption was that `/usage` would omit the `Current session` line when no window is open.
Probes into an expired window at 02:00 and 03:00 showed otherwise.

## Decision

With no window open, `/usage` prints `Current session: 0% used` with **no `· resets` clause**.
The line is present, so the absence of the *line* is not the signal.

- The reset clause is the only thing that discriminates: clause present means open until it passes, clause absent means closed.
- The percentage is display only.
  A window opened by one tiny request reads `0% used · resets ...`, so 0% tells you nothing.
- A missing reset is never defaulted to `now + 5h`.
  Doing so would read the closed state as a window live until morning.
  `--window fresh` jobs would never dispatch, and because the cache would report live, nothing would re-probe to correct it.
  Missing means `NULL`, and `NULL` means closed.

## Consequence

`usage.parse_usage` derives `is_open` from `reset_at` alone, and a `window` row with a `NULL` `reset_at` is closed.
The verbatim open and closed outputs are the fixtures in `tests/fixtures/`.
A parser tested only against live output only ever meets the open branch, which is the branch that works.

See also [reset-minutes-are-optional.md](reset-minutes-are-optional.md): a pattern that is too strict about the clause fails the same way.
