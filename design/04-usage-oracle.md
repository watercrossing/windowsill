# 4. `/usage` as the window oracle

`claude -p "/usage"` returns ground truth. Both forms below are verbatim fixtures — **put them in `tests/fixtures/` before writing the parser.**

**Open window:**

```
You are currently using your subscription to power your Claude Code usage

Current session: 15% used · resets Jul 17, 7:09pm (Europe/London)
Current week (all models): 1% used · resets Jul 24, 5:59am (Europe/London)
Current week (Fable): 0% used

What's contributing to your limits usage?
Approximate, based on local sessions on this machine — does not include other devices or claude.ai. Behaviors are independent characteristics, not a breakdown.

Last 24h · 116 requests · 6 sessions
  61% of your usage was at >150k context
  Top skills: /schedule 2%
```

**Closed window** (observed 02:00 and 03:00, 2026-07-17):

```
You are currently using your subscription to power your Claude Code usage

Current session: 0% used
Current week (all models): 15% used · resets Jul 17, 5:59am (Europe/London)
Current week (Fable): 0% used
```

Notes:

- **Line 1's "session" is the window.** Its `resets` timestamp is the only fact the scheduler needs.
- **The trailing block is machine-local** — "does not include other devices or claude.ai" — and counts *conversations*, not windows. Never an input to a dispatch decision. Fine to surface in `windowsill report`.
- **The separate weekly bucket is Fable, not Opus.** Support docs still say Opus-only; the client is ground truth. Read the bucket name from the output; do not hardcode a model.

## 4.1 Parsing

`--output-format json` wraps this string in an envelope ([§3.2](03-execution-model.md)) but does **not** structure it. The text must be parsed. Treat it as hostile:

- Keep every regex in one module-level `USAGE_PATTERNS` constant. This string will change.
- **The closed state is `Current session: 0% used` with no `· resets` clause.** The line is present, so absence of the *line* is not the signal.
  - **`reset_at` is the sole discriminator.** Reset clause present ⇒ window open until it passes. Absent ⇒ closed.
  - **The percentage is display-only. Never infer state from it.** A window opened by one tiny request reads `0% used · resets ...`, so 0% is not evidence of anything.
  - **Never default a missing `reset_at` to `now + 5h`.** That reads the closed state as a window live until 07:00: `--window fresh` jobs never dispatch, and the cache reports live so nothing re-probes to correct it. Missing ⇒ `NULL` ⇒ closed.
- **No year in the timestamp.** Infer by choosing the candidate year (`now.year`, `±1`) that puts the reset nearest to now and in the future. Test 31 Dec explicitly.
- **The minutes are optional.** On the hour the clause reads `resets Jul 28, 7am (Europe/London)`, with no `:00` ([§9](09-findings.md), observed 2026-07-28). A pattern that requires `:mm` reads a live window as closed.
- The timezone is parenthesised — resolve via `zoneinfo`, store UTC, display local.
- `windowsill doctor` asserts the parse still works against live output and fails loudly if not.

## 4.2 The probe rule

**Probing does not open a window** (verified 2026-07-17: probes at 02:00 and 03:00 into an expired window both returned the closed state; had the 02:00 probe anchored, 03:00 would have shown a window live to ~07:00). The local request counter did not move either. `/usage` reads metadata; it is not an inference call — `num_turns: 0`, `duration_api_ms: 0`.

**And the reading is account-global.** Same night, local counters frozen to the digit (140/6, 165/8 across both probes) while `Current week` moved 13% → 15% — usage from another surface, invisible to the local block, visible on line 1. Lines 1–3 are server-side account truth.

Free and global together mean the oracle can simply be consulted:

- **Probe when a dispatch decision depends on window state** and the cached `reset_at` is absent or has passed. Common case; costs nothing.
- **Probe before and after each run** ([§4.3](04-usage-oracle.md)).
- **Cache anyway**, in the `window` table ([§6](06-state.md)). Not for correctness — for latency and manners. `windowsill stat` reads cache and never probes; `windowsill usage --probe` is the explicit override. Rate-limit to 1 probe per 10s.
- **Do not probe on every cron tick regardless.** The tick usually has no dispatch candidate and therefore no question to ask.

This is a behavioural finding on one client version, not a documented guarantee. If `/usage` ever starts costing a window, the failure is silent and expensive — `windowsill doctor` re-runs the two-probe check ([§8.2](08-interface.md)).

Note: each probe returns a `session_id` and may leave a conversation record on disk. It does not increment the local conversation counter and is otherwise harmless.

`--window fresh` therefore verifies against global truth: probe first, and if a reset clause comes back, some other surface holds a live window — do not dispatch.

## 4.3 Δ window % as the cost metric

Probe immediately before and after each run; store both in `runs`. The delta is what the job cost in the only unit that binds.

- Approximate: another surface may move the number concurrently. Acceptable — single user, mostly asleep.
- `windowsill report` shows Δ% per job. This is the number that tells you a job is too expensive to keep queueing.
- Sanity anchor from the target account: **one large prompt ≈ 15% of a window**, at >150k context. A job showing 40% is not a job, it is a project.
