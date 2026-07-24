# 3. Execution model

## 3.1 Dispatch: chain-driven, cron-floored

An asymmetry determines this design. **Idle time inside an open window is destroyed forever. Idle time while no window is open costs nothing**, because the next window anchors on the next message — blocked until 04:37 and dispatching at 04:45 loses 8 minutes of wall clock and zero minutes of allowance.

So latency only matters between jobs inside a live window, and polling is the wrong tool for it:

- **Chain dispatch (the latency path).** On job exit, the runner spawns a detached `ccq tick` and exits. Consecutive jobs in an open window start within seconds of each other.
- **Cron (the liveness floor).** `*/15 * * * * ccq tick` catches a broken chain, an `--at` coming due, and a `blocked_until` expiring. 15 minutes is fine precisely because it is never the latency path.

Cron, not a systemd timer: no `loginctl enable-linger` requirement, no unit files, works identically on any always-on box.

`ccq tick`:

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

- `session_id` → `jobs.conversation_id`. `is_error` and `subtype` → run outcome. `num_turns`, `duration_ms`, `usage` → `ccq report`.
- `stream-json` emits incrementally: write straight to `stream.jsonl` so a killed run still leaves a usable partial, and derive the result from the last event.
- **Do not trust `total_cost_usd`.** The observed `0` came from a zero-turn slash command, so it is uninformative; whether it is populated for a real run on a subscription is untested ([§9](09-findings.md)). Use the Δ window % ([§4.3](04-usage-oracle.md)) as the cost metric regardless — it is the unit that actually binds.
- Confirm whether `--model` takes the full API string (`claude-opus-4-8`) or aliases only (`opus`). Normalise in config; reject neither.

## 3.3 Safety for unattended runs

Unattended means no permission prompts, so blast radius must be bounded by construction:

- Default to an explicit `--allowedTools` allowlist from config, **not** blanket permission bypass. A job wanting bypass must say so in its own header.
- `--isolate worktree` → `git worktree add ~/.ccq/worktrees/<id>-<name> -b ccq/<name>`, run there, leave the branch for morning review. Default for anything that writes code.
- Refuse to dispatch into a dirty tree unless `--allow-dirty`.
- Enforce `--timeout` with SIGTERM to the process group, then SIGKILL after 30s. Record the partial transcript.
- Never remove a worktree automatically. `ccq gc` only, on demand.
