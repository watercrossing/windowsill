# windowsill — design notes

How windowsill behaves is documented where it is implemented.
That means the module and function docstrings in `src/windowsill/`, the tests, [`README.md`](../README.md), and the commented job files in [`examples/`](../examples/).
This directory keeps only what the code can't say for itself.

- [01-motivation.md](01-motivation.md): why the tool exists, the monthly window ceiling, the non-goals, and the window / conversation / run vocabulary.
- [tensions.md](tensions.md): questions that have already been decided. Read it before reopening one.
- [decisions/](decisions/): one record per empirical finding that contradicted a plausible assumption, each named for what was decided.
- [decisions/open.md](decisions/open.md): what is still unsettled.

## Decisions

The `/usage` oracle:

- [reset-clause-is-the-discriminator.md](decisions/reset-clause-is-the-discriminator.md): closed means no `· resets` clause; the percentage is display only
- [reset-minutes-are-optional.md](decisions/reset-minutes-are-optional.md): on the hour, the clause has no `:00`
- [extra-weekly-bucket-is-optional.md](decisions/extra-weekly-bucket-is-optional.md): read the bucket's name from the output; it may be absent
- [probes-are-free.md](decisions/probes-are-free.md): a probe does not open a window
- [usage-is-account-global.md](decisions/usage-is-account-global.md): the window and weekly lines see every surface
- [usage-text-is-parsed-not-json.md](decisions/usage-text-is-parsed-not-json.md): `--output-format json` doesn't structure it

Running `claude` unattended:

- [auth-survives-cron.md](decisions/auth-survives-cron.md): no TTY, near-empty environment, still authenticated
- [permission-denials-are-counted.md](decisions/permission-denials-are-counted.md): a denied write still reports success
- [permission-mode-auto-is-not-sufficient.md](decisions/permission-mode-auto-is-not-sufficient.md): `auto` behaves differently from one model to the next
- [allowed-tools-is-additive.md](decisions/allowed-tools-is-additive.md): `--allowed-tools` pre-approves; it doesn't bound
- [permission-mode-is-the-boundary.md](decisions/permission-mode-is-the-boundary.md): no `safety.disallowed_tools`, by the owner's decision
- [model-id-or-alias.md](decisions/model-id-or-alias.md): `opus` or `claude-opus-5`, never `claude-opus` or `opus-5`
- [model-is-not-validated.md](decisions/model-is-not-validated.md): a bad model is a 404 at dispatch, and that is accepted

Reading a run's outcome:

- [result-event-is-not-the-last-line.md](decisions/result-event-is-not-the-last-line.md): scan for the last `result` event
- [blocked-not-failed.md](decisions/blocked-not-failed.md): work left live means `blocked`, which is deliberately not terminal
- [blocked-until-is-compared-not-cleared.md](decisions/blocked-until-is-compared-not-cleared.md): a non-null `blocked_until` isn't a block
- [window-percent-not-dollars.md](decisions/window-percent-not-dollars.md): Δ window % is the only cost unit

Notifications:

- [notifications-are-opt-in.md](decisions/notifications-are-opt-in.md): no destination is ever defaulted or guessed

A new finding gets its own file here.
Name it for what was decided, and give it the date it was settled, a short Context, the Decision and the Consequence.
When an open question is settled, move it out of `open.md` into its own record.

## History

Until 2026-09 this directory held the build spec for v0, one file per section (§0–§12), written to brief the agent that implemented it.
That job is done, and `git log -- design/` keeps the spec.
What was still true and not obvious from the code was folded into docstrings and the README before the spec was deleted.
