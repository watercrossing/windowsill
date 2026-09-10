# A bad `--model` is not caught at submit, and windowsill won't try

**Settled:** 2026-08-27

## Context

Same investigation as [model-id-or-alias.md](model-id-or-alias.md).
Having found the failure shapes, the tempting fix was to reject them in `ccsub`.

## Decision

**The CLI does not reject an unknown model locally.**
It prints an `unrecognized_model` warning about its context-window assumption and then sends the string anyway.
The 404 comes back from the API.

So a bad `--model` cannot be caught at submit time by anything windowsill could reasonably do.
It costs a dispatch, and on a job with `--at`, a night.
The mitigation is documentation, not validation ([../tensions.md](../tensions.md): pass-through beats validation).

## Consequence

`--model` stays pure pass-through.
It is mirrored into `jobs.model` for display, and nothing is normalised or checked.
The README's job-file section spells out the two valid shapes where a job writer will see them.
