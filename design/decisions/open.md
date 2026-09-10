# Still open

Questions nobody has settled yet.
When one is settled, move it into its own decision file, named for what was decided.

- **Rate-limit signal shape:** is it an exit code, a subtype in the JSON, or only a human string?
  This decides how brittle rate-limit detection is; the match patterns live in one constant in `runner` for that reason.
  It can only be learnt by hitting a real limit.
- **`[queue].default_model` is dead config.**
  It is defined, documented and tested, but nothing reads it, so a job with no `--model` gets whatever `claude` itself defaults to.
  Either wire it into `build_argv` as an injected default, following the same yield rule as the system prompt and the allowlist, or delete it.
  Shipping a setting that silently does nothing is the worst of the three options.
  Found 2026-08-27 ([model-id-or-alias.md](model-id-or-alias.md)).
- **How much does `--resume` actually cost** at high context?
  The "scope a job to finish inside one window" guidance ([../tensions.md](../tensions.md)) assumes resuming is expensive, because one turn at >150k context cost 15% of a window.
  This can now be measured: probe, resume a long conversation with a trivial turn, then probe again.
  Worth doing early: it is the difference between resume-on-limit being a safety net and being a trap.
