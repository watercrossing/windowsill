# 9. Findings and open questions

## Settled 2026-07-17

| Question | Answer | Consequence |
|---|---|---|
| Does auth survive a non-interactive job with no TTY? | **Yes.** `at` + `env -i HOME=$HOME PATH=$PATH claude -p "/usage"` → `exit=0` | The project is buildable. Was the one that could have killed it |
| What does `/usage` print with no window open? | `Current session: 0% used`, **no `· resets` clause** | [§4.1](04-usage-oracle.md) — the line is present; the reset clause is the discriminator |
| Does the probe anchor a window? | **No.** Probes at 02:00 and 03:00 into an expired window both read closed; local request counter unmoved; `num_turns: 0` | [§4.2](04-usage-oracle.md) — probes are free |
| Are the `Current session` / `Current week` lines account-global? | **Yes.** Local counters frozen (140/6, 165/8) while weekly moved 13% → 15% from another surface | [§4.2](04-usage-oracle.md) — the oracle sees your phone; `--window fresh` is verifiable |
| Does `--output-format json` structure the `/usage` output? | **No.** It wraps the same human string in `result` | [§4.1](04-usage-oracle.md) stands; but the envelope confirms [§3.2](03-execution-model.md)'s field names |

Evidence is one machine, one client version, one night. These are behavioural findings, not documented guarantees; `ccq doctor` re-checks the first four ([§8.2](08-interface.md)).

## Still open

- **Rate-limit signal shape:** exit code, a subtype in the JSON, or only a human string? Decides how brittle [§5.4](05-windows.md) is. Only learnable by hitting a real one — which M1 will do within a week of use.
- **Is `total_cost_usd` populated for a real run on a subscription?** The observed `0` was a zero-turn slash command and proves nothing. Low stakes: [§4.3](04-usage-oracle.md) uses Δ% regardless.
- **How much does `--resume` actually cost** at high context? [§5.4](05-windows.md)'s guidance assumes it is expensive, on the basis that one >150k-context turn cost 15% of a window. Now measurable: probe, resume a long conversation with a trivial turn, probe again. Worth doing early — it is the difference between resume-on-limit being a safety net and being a trap.
