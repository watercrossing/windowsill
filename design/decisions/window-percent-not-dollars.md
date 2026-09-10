# Δ window % is the cost unit, not `total_cost_usd`

**Settled:** 2026-07-17 (unit), 2026-07-28 (dollar field)

## Context

The result envelope carries `total_cost_usd`.
The first observation of it was `0`, from a zero-turn slash command, which says nothing either way.
It was an open question whether it is populated for a real run on a subscription.

## Decision

**It is populated**: `0.0259` for a Haiku run and `0.0873` for an Opus one (2026-07-28).
It is still not the cost metric.
On a subscription a dollar figure is a shadow price, not a budget.
The unit that binds is the 5-hour window, so a run's cost is the change in window % between a `/usage` probe just before it and one just after it.

## Consequence

`runs.pct_before` / `runs.pct_after` are the only cost columns.
windowsill records no token counts and no dollar cost anywhere, and estimates neither from the percentages.
`windowsill report` and the completion mail show Δ% per run.

The number is approximate, because another surface can move it at the same time.
That is acceptable for a single user who is mostly asleep.
It is also not additive across a rate limit: a run that ends at 100% is followed by one starting at 0% of a fresh window, so a summed Δ% is a floor, not a total.
The mail says so.

For scale: on the target account, one large prompt at >150k context cost about 15% of a window.
A job showing 40% is not a job; it is a project.
