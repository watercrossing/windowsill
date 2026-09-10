# `--model` takes a full id or a bare alias, and nothing in between

**Settled:** 2026-08-27

## Context

The first live run of the weekly Ente rebase job (a job file outside this repo) died in one turn, at $0, on `#$ --model claude-opus`.
Nine spellings were then probed directly against `claude` 2.1.247.

## Decision

**Both shapes work, and only those two.**
`opus`, `sonnet` and `haiku` work.
`claude-opus-5`, `claude-opus-4-8` and `claude-haiku-4-5` work.
`claude-opus`, `claude-sonnet` and `opus-5` all fail.
The `claude-` prefix is required *with* a version and forbidden *without* one.
The two failure shapes are hybrids of the two valid shapes; the prefix alone is not the problem.

## Consequence

Every example, the README and `[queue].default_model` used to pin `claude-opus-4-8`.
That works, but it names a version the reader did not choose.
They were switched to `opus`, and `[anchor].model` to `haiku`, so nothing windowsill *suggests* pins a version.

The test suite deliberately keeps `claude-opus-4-8` in its pass-through fixtures.
Those tests assert that an arbitrary string reaches argv unaltered, and a full id is a better witness for that.

The same investigation found that `[queue].default_model` is read by nothing in `src/` ([open.md](open.md)).
