# 3. Execution model

## 3.1 Dispatch: chain-driven, cron-floored

An asymmetry determines this design. **Idle time inside an open window is destroyed forever. Idle time while no window is open costs nothing**, because the next window anchors on the next message — blocked until 04:37 and dispatching at 04:45 loses 8 minutes of wall clock and zero minutes of allowance.

So latency only matters between jobs inside a live window, and polling is the wrong tool for it:

- **Chain dispatch (the latency path).** On job exit, the runner spawns a detached `windowsill tick` and exits. Consecutive jobs in an open window start within seconds of each other.
- **Cron (the liveness floor).** `*/15 * * * * windowsill tick` catches a broken chain, an `--at` coming due, and a `blocked_until` expiring. 15 minutes is fine precisely because it is never the latency path.

Cron, not a systemd timer: no `loginctl enable-linger` requirement, no unit files, works identically on any always-on box.

`windowsill tick`:

1. Opens the DB, `BEGIN IMMEDIATE` (this is the dispatch lock — [§6](06-state.md)).
2. Reaps: for each `running` job, check pid liveness and `result.json`.
3. Evaluates the queue; dispatches up to `max_parallel` jobs.
4. Commits and exits. It never blocks on a running job.

Runs are spawned detached (own process group, output to the job dir), tracked by pid + start time. Concurrent ticks serialise on `BEGIN IMMEDIATE`; the loser sees the winner's state and no-ops. Chain-spawned ticks must be `Popen`-and-exit, never recursive in-process, or a fast-failing job becomes a fork bomb.

**`max_parallel` defaults to 1.** Concurrent runs drain the window faster and fight over the working tree. Opt-in, and warn on it.

## 3.2 The invocation

```
cd <cwd> && claude -p \
  --output-format stream-json --verbose \
  [--resume <conversation_id>] \
  [--allowed-tools <comma-separated tools>] \
  [--append-system-prompt <windowsill's prompt>] \
  [--add-dir <job dir>] \
  <pass-through directives...> \
  < body
```

The body goes on **stdin**, never argv. Bodies will exceed `ARG_MAX` and will contain everything a shell hates.

**The result envelope is confirmed** (observed 2026-07-17, `--output-format json`):

```json
{"type":"result","subtype":"success","is_error":false,"duration_ms":537,
 "duration_api_ms":0,"num_turns":0,"result":"<text>","stop_reason":null,
 "session_id":"795f82fe-...","total_cost_usd":0,
 "usage":{"input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0,...},
 "modelUsage":{},"permission_denials":[],"uuid":"..."}
```

- `session_id` → `jobs.conversation_id`. `is_error` and `subtype` → run outcome. `num_turns`, `duration_ms`, `usage` → `windowsill report`.
- `stream-json` emits incrementally: write straight to `stream.jsonl` so a killed run still leaves a usable partial, and derive the result from the last **`result`** event — not the last event. The two are not the same line: a run that leaves background tasks behind emits its result envelope and *then* a teardown tail, one `background_tasks_changed` / `task_updated` / `task_notification` triple per task, as the CLI kills each one off ([§9](09-findings.md), settled 2026-08-28). Reading the final line and requiring `type: "result"` reported a successful run as `killed`. A stream can also carry more than one `result` event, and the run's own outcome is the last of them.
- **Do not trust `total_cost_usd`.** The observed `0` came from a zero-turn slash command, so it is uninformative; whether it is populated for a real run on a subscription is untested ([§9](09-findings.md)). Use the Δ window % ([§4.3](04-usage-oracle.md)) as the cost metric regardless — it is the unit that actually binds.
- Confirm whether `--model` takes the full API string (`claude-opus-4-8`) or aliases only (`opus`). Normalise in config; reject neither.

**The appended system prompt** (`[prompt]`, §8.3) is a *default*, not a new capability — `#$ --append-system-prompt "..."` has always passed straight through. It tells the job the one thing its own body cannot know (nobody is watching, so a question ends the run) and asks it to keep a status journal at `<job dir>/journal.md`, reached by the injected `--add-dir`. The journal lives in the job dir and never the working tree: it must not pollute the branch left for morning review, and being keyed on job id is what makes it survive a re-dispatch. `--append-system-prompt`, not `--system-prompt`: the latter *replaces* Claude Code's own prompt and strips the harness instructions (its help for `--exclude-dynamic-system-prompt-sections` says so outright). Injection is skipped entirely — prompt *and* `--add-dir`, logged as a `system_prompt_yielded` event — for anchor jobs (§5.2), and when the job's own pass-through names any of `--append-system-prompt`, `--append-system-prompt-file`, `--system-prompt`, `--system-prompt-file` (`runner.SYSTEM_PROMPT_FLAGS`). That yield rule is also the free per-job opt-out (`#$ --append-system-prompt ""`), so there is no new scheduler directive. It is a mirror, not an intercept: nothing is validated, rejected, reordered or removed (§2.2). The re-execution line is injected only when windowsill *knows* it is true — a prior run exists **and** this dispatch carries no `resume_id`, i.e. the `--retry` / `--no-resume-on-limit` shape, which re-dispatches a fresh conversation with the original body. A resumed agent already holds that history and is never told to go read it.
- `--add-dir` takes `<directories...>` and is **variadic** on the installed CLI (2.1.218), not the single-argument flag it looks like: it keeps consuming tokens until one that looks like a flag. `build_argv` therefore emits it as the *last* injected token, directly abutting pass-through, so the swallow stops at the first `#$` directive. `--append-system-prompt-file` and `--system-prompt-file` exist but are `.hideHelp()`'d, and each is mutually exclusive with its inline sibling — pairing them is a hard error that kills the run, which is the second reason the yield rule is total.

## 3.3 Safety for unattended runs

Unattended means no permission prompts, so blast radius must be bounded by construction:

- `safety.allowed_tools` (§8.3) is rendered as `claude`'s `--allowed-tools` flag in `runner.build_argv`: the configured list becomes one `--allowed-tools` argv token holding a comma-separated tool list (confirmed spelling and argument shape against the installed CLI, `claude --help`, v2.1.218 — the CLI also accepts `--allowedTools`, kept as an alias only). **This pre-approves; it is not a sandbox.** [§9](09-findings.md) "Settled 2026-07-31": `--allowed-tools` was found by experiment to be *additive* — a tool named in it skips its permission prompt, but a tool left out of it is not blocked, denied, or even absent from the model's tool set. `--disallowed-tools` is the flag that actually narrows what the model can call at all (confirmed by the same experiment: a `Bash`-disallowed run reported Bash not present in its tools), and it is **not implemented and will not be** — `windowsill` sets no `safety.disallowed_tools` and has no config key for one. **Settled 2026-08-04, by the owner:** the tool surface is not this project's boundary to draw. `--permission-mode` is what bounds an unattended job, it is already reachable per-job through pass-through directives, and a second overlapping mechanism in config would only invite the two to disagree. So an unattended job's blast radius is bounded by its permission mode and `claude`'s own defaults for an unlisted tool — deliberately, not for want of a feature. An empty/unset `safety.allowed_tools` injects nothing (the same "no restriction, no auto-approval" state as before this feature existed). Injection is also skipped — silently but visibly, via a logged `allowed_tools_yielded` event — when the job's own pass-through directives already name a tool-permission flag of their own: `--allowed-tools`/`--allowedTools`, `--disallowed-tools`/`--disallowedTools`, `--permission-mode`, `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, or `--tools` (`runner.TOOL_PERMISSION_FLAGS`) — the job's own flag always wins.
- `--isolate worktree` → `git worktree add ~/.windowsill/worktrees/<id>-<name> -b windowsill/<name>`, run there, leave the branch for morning review. Default for anything that writes code.
- Refuse to dispatch into a dirty tree unless `--allow-dirty`.
- Enforce `--timeout` with SIGTERM to the process group, then SIGKILL after 30s. Record the partial transcript.
- Never remove a worktree automatically. `windowsill gc` only, on demand.
