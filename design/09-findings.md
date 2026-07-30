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

## Settled 2026-07-28

| Question | Answer | Consequence |
|---|---|---|
| Does the reset clause always carry minutes? | **No.** On the hour it prints `resets Jul 28, 7am (Europe/London)` — no `:00` | [§4.1](04-usage-oracle.md) — minutes are optional in the pattern. Requiring them read a live 74%-used window as *closed*, which is the failure that dispatches `--window fresh` into an open window |
| Is the extra weekly bucket always present? | **No.** The same output had no `Current week (Fable)` line at all | Read the bucket if it's there; its absence is not a parse failure |

Both observed on the target account while a window was live, and both were caught by the parser reporting `is_open: False` at 74% used — the percentage is display-only ([§4.1](04-usage-oracle.md)), so nothing else would have noticed.

## Settled 2026-07-28, second sitting

Three jobs — one Haiku 4.5, one Sonnet 5 spawning a subagent, one Opus 4.8 held on the second — submitted to a throwaway `--home` and dispatched by hand-run ticks.

| Question | Answer | Consequence |
|---|---|---|
| Does a denied permission fail the run? | **No.** Two `Write` calls were refused under `--permission-mode auto`; the envelope still returned `subtype: success`, `is_error: false`, with both refusals listed in `permission_denials` | A job that was blocked from doing anything reads `done` in `ccq stat`. That is the overnight failure this project exists to catch, and nothing in [§7](07-lifecycle.md) currently looks at the field. Count it per run ([§6](06-state.md)) and show it ([§8.1](08-interface.md)) |
| Is `total_cost_usd` populated for a real run on a subscription? | **Yes.** `0.0259` for the Haiku run, `0.0873` for the Opus one | Answers the open question below it. [§4.3](04-usage-oracle.md)'s Δ% is still the unit that binds — a dollar figure on a subscription is a shadow price, not a budget |
| Is `--permission-mode auto` enough for an unattended writer? | **No, and not per-model.** Sonnet's writes went through in the same directory where Haiku's were refused | Do not read one job's success as proof the mode is sufficient. [§3.3](03-execution-model.md)'s allowlist is the mechanism; `auto` is a coin toss |

## Settled 2026-07-30

Two jobs run overnight against the live queue by the repo owner, who had configured nothing about notifications.

| Question | Answer | Consequence |
|---|---|---|
| What does a shipped default recipient do? | **It gets mailed.** The `you@example.org` in [§8.3](08-interface.md)'s example was the effective default, `[notify].default` was `email`, and both runs mailed it. The postmaster refused it: *"an MX or SRV record indicated no SMTP service"* | Notifications are opt-in and no destination is ever defaulted or guessed ([§8.4](08-interface.md)). The same applies to `[notify.ntfy].topic`: a shipped topic is a *public* one |

## Still open

- **Rate-limit signal shape:** exit code, a subtype in the JSON, or only a human string? Decides how brittle [§5.4](05-windows.md) is. Only learnable by hitting a real one — which M1 will do within a week of use.
- **How much does `--resume` actually cost** at high context? [§5.4](05-windows.md)'s guidance assumes it is expensive, on the basis that one >150k-context turn cost 15% of a window. Now measurable: probe, resume a long conversation with a trivial turn, probe again. Worth doing early — it is the difference between resume-on-limit being a safety net and being a trap.
