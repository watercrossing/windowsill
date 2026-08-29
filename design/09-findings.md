# 9. Findings and open questions

## Settled 2026-07-17

| Question | Answer | Consequence |
|---|---|---|
| Does auth survive a non-interactive job with no TTY? | **Yes.** `at` + `env -i HOME=$HOME PATH=$PATH claude -p "/usage"` → `exit=0` | The project is buildable. Was the one that could have killed it |
| What does `/usage` print with no window open? | `Current session: 0% used`, **no `· resets` clause** | [§4.1](04-usage-oracle.md) — the line is present; the reset clause is the discriminator |
| Does the probe anchor a window? | **No.** Probes at 02:00 and 03:00 into an expired window both read closed; local request counter unmoved; `num_turns: 0` | [§4.2](04-usage-oracle.md) — probes are free |
| Are the `Current session` / `Current week` lines account-global? | **Yes.** Local counters frozen (140/6, 165/8) while weekly moved 13% → 15% from another surface | [§4.2](04-usage-oracle.md) — the oracle sees your phone; `--window fresh` is verifiable |
| Does `--output-format json` structure the `/usage` output? | **No.** It wraps the same human string in `result` | [§4.1](04-usage-oracle.md) stands; but the envelope confirms [§3.2](03-execution-model.md)'s field names |

Evidence is one machine, one client version, one night. These are behavioural findings, not documented guarantees; `windowsill doctor` re-checks the first four ([§8.2](08-interface.md)).

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
| Does a denied permission fail the run? | **No.** Two `Write` calls were refused under `--permission-mode auto`; the envelope still returned `subtype: success`, `is_error: false`, with both refusals listed in `permission_denials` | A job that was blocked from doing anything reads `done` in `windowsill stat`. That is the overnight failure this project exists to catch, and nothing in [§7](07-lifecycle.md) currently looks at the field. Count it per run ([§6](06-state.md)) and show it ([§8.1](08-interface.md)) |
| Is `total_cost_usd` populated for a real run on a subscription? | **Yes.** `0.0259` for the Haiku run, `0.0873` for the Opus one | Answers the open question below it. [§4.3](04-usage-oracle.md)'s Δ% is still the unit that binds — a dollar figure on a subscription is a shadow price, not a budget |
| Is `--permission-mode auto` enough for an unattended writer? | **No, and not per-model.** Sonnet's writes went through in the same directory where Haiku's were refused | Do not read one job's success as proof the mode is sufficient. [§3.3](03-execution-model.md)'s allowlist is the mechanism; `auto` is a coin toss |

## Settled 2026-07-30

Two jobs run overnight against the live queue by the repo owner, who had configured nothing about notifications.

| Question | Answer | Consequence |
|---|---|---|
| What does a shipped default recipient do? | **It gets mailed.** The `you@example.org` in [§8.3](08-interface.md)'s example was the effective default, `[notify].default` was `email`, and both runs mailed it. The postmaster refused it: *"an MX or SRV record indicated no SMTP service"* | Notifications are opt-in and no destination is ever defaulted or guessed ([§8.4](08-interface.md)). The same applies to `[notify.ntfy].topic`: a shipped topic is a *public* one |

## Settled 2026-07-31

`safety.allowed_tools` ([§3.3](03-execution-model.md), [§8.3](08-interface.md)) had just been wired up as `claude`'s `--allowed-tools` flag, on the plausible assumption that it bounds the tool surface. Two commands against `claude` 2.1.218, with no ambient permission rules anywhere (`~/.claude/settings.json` has no `permissions` key; no project `.claude/settings*.json` exists), say otherwise:

| Question | Answer | Consequence |
|---|---|---|
| Does `claude -p "run bash echo ..." --allowed-tools Read` block Bash? | **No.** Bash ran anyway, `permission_denials: []`, exit success | `--allowed-tools` is an *additive* auto-approve list, not a boundary — [§3.3](03-execution-model.md)'s allowlist bullet was rewritten to stop implying otherwise |
| Does `claude -p "run bash echo ..." --disallowed-tools Bash` block Bash? | **Yes.** The model reported Bash was not present in its tool set at all | `--disallowed-tools` is the flag that actually narrows the surface. Not implemented, and **closed 2026-08-04**: the owner draws the boundary with `--permission-mode`, so windowsill will not grow a competing `safety.disallowed_tools` ([§3.3](03-execution-model.md)) |

Consequence for the default itself: because `--allowed-tools` never restricted anything, the pre-existing default list's omission of `Task` was harmless by accident, not by design — and it was a loaded gun for the day permission prompting actually engages (a `--permission-mode` change, a future CLI default, a sandboxed environment). Widened to add `Task`, `TodoWrite`, `WebFetch`, `WebSearch`, `NotebookEdit` ([§8.3](08-interface.md)).

## Settled 2026-08-27

The first live run of the weekly Ente rebase job (a job file outside this repo) died in one turn, at $0, on `#$ --model claude-opus`. Nine spellings then probed directly against `claude` 2.1.247:

| Question | Answer | Consequence |
|---|---|---|
| Does `--model` take a full API string, an alias, or both? | **Both, and only those two shapes.** `opus`/`sonnet`/`haiku` work; `claude-opus-5`/`claude-opus-4-8`/`claude-haiku-4-5` work. `claude-opus`, `claude-sonnet` and `opus-5` all fail | Answers the open question in [§3.2](03-execution-model.md). The `claude-` prefix is mandatory *with* a version and forbidden *without* one — the two failures are the hybrids, not the prefix itself |
| Does the CLI reject an unknown model locally? | **No.** It prints an `unrecognized_model` warning about its context-window assumption, then sends the string anyway; the 404 comes back from the API | A bad `--model` is not catchable at submit time by anything windowsill could reasonably do — it costs a dispatch and, on a job with `--at`, a night. The mitigation is documentation, not validation ([§0.4](00-instructions.md) "pass-through beats validation") |

Consequence for the shipped defaults: every example, the README and `[queue].default_model` pinned `claude-opus-4-8`, which works but names a version the reader did not choose. Switched to `opus`, and `[anchor].model` to `haiku` ([§8.3](08-interface.md)). The test suite deliberately keeps `claude-opus-4-8` in its pass-through fixtures — those assert that an arbitrary string reaches argv unaltered, and a full id is the better witness for that.

`[queue].default_model` is read by nothing in `src/` — it is documented in [§8.3](08-interface.md) and merged by `load_config`, but no caller consults it. Still open below.

## Settled 2026-08-28

The second live run of the weekly Ente rebase job (job 2) mailed `failed (killed, 2 still running)` over a run that had succeeded. Its `stream.jsonl` is the evidence: line 240 is `subtype: "success"`, `is_error: false`, `terminal_reason: "completed"`, and the process exited 0 after 6m13s.

| Question | Answer | Consequence |
|---|---|---|
| Is the `result` event the last line of `stream.jsonl`? | **No.** Six `system` events followed it — `background_tasks_changed`, `task_updated` (`status: "killed"`), `task_notification` (`status: "stopped"`), twice, one triple per background task being torn down at session exit | `runner.parse_result_event` implemented [§3.2](03-execution-model.md)'s "last event" literally and returned `None`, which [§6](06-state.md)'s outcome mapping reads as "died before emitting a result" → `killed` → job `failed`. Fixed to scan for the last `result` event; §3.2's wording corrected with it |
| Does the misread cost anything beyond the label? | **Yes, three things.** `conversation_id` is read off that event, so the job row kept none and the mail said "No conversation to resume" while `cb87555e-…` sat in the stream; no `result.json` and no `resume.sh` were written; `num_turns` / `duration_ms` were left null in `runs` | Everything downstream of the envelope is downstream of this one function. Job 1's dir has `result.json` and `resume.sh`; job 2's has neither, which is the cheapest on-disk tell that this happened |
| What was the run's true outcome? | **`incomplete`, not `done`.** Two backgrounded builds (`byfox3ke2` "Build both images", `bm0hdqzjx` "ente web image build completion/errors") were live when the main thread stopped, which is exactly what `outcome_from_result` downgrades `done` to `incomplete` for | Replaying the real capture through the fixed code gives `incomplete` → job state `blocked` ([§7](07-lifecycle.md)) → the mail that names the tasks and tells the reader to `ccresume 2`. The machinery for the right answer was already there; the parse never reached it |

The word "killed" in those events is about the *tasks*, not the run — a vocabulary collision with [§6](06-state.md)'s `runs.outcome`, and the reason the mail read as a plausible report of a real kill rather than as nonsense.

A second, unrelated defect surfaced in the same mail: it announced "the queue is blocked until 2026-08-21 05:00" on the 28th. `sched.blocked_until` is never cleared when it lapses — the scheduler stops honouring it by comparison (`blocked_until > now`, [§3.1](03-execution-model.md)), and `ccstat` does the same ([§8.1](08-interface.md)) — but `mail` reads no clock by contract and tested `bool(blocked_until)`. The comparison now happens in `runner._run_facts`, against the run's own `finished_at`, and `RunFacts.blocked_until` means "held now".

**The standing lesson for the next reader of a job's mail:** the mail is derived, and the stream is the record. `windowsill show <id>` and the job dir's `stream.jsonl` outrank the subject line.

## Still open

- **Rate-limit signal shape:** exit code, a subtype in the JSON, or only a human string? Decides how brittle [§5.4](05-windows.md) is. Only learnable by hitting a real one — which M1 will do within a week of use.
- **`[queue].default_model` is dead config.** Defined, documented and tested, but nothing reads it — a job with no `--model` gets whatever `claude` itself defaults to, not this. Either wire it into `build_argv` as an injected default (with the same yield rule the system prompt and allowlist use, [§3.2](03-execution-model.md)) or delete it from [§8.3](08-interface.md). Shipping a setting that silently does nothing is the worse of the three.
- **How much does `--resume` actually cost** at high context? [§5.4](05-windows.md)'s guidance assumes it is expensive, on the basis that one >150k-context turn cost 15% of a window. Now measurable: probe, resume a long conversation with a trivial turn, probe again. Worth doing early — it is the difference between resume-on-limit being a safety net and being a trap.
