# 5. Windows, anchors, accounting, limits

## 5.1 `--window` policies

| Policy | Dispatch when |
|---|---|
| `share` (default) | Any time. Small follow-ups |
| `fresh` | A probe ([§4.2](04-usage-oracle.md)) reports no live window. For the big overnight job that should own a whole window |
| `offpeak` | Now is outside `config.window.peak_band` |

## 5.2 Anchors are lazy, and mostly implicit

An anchor does not create capacity. It moves a boundary. **A window opened at 01:30 and slept through is not harvested capacity — it is a window spent for nothing.**

- **Any dispatched job is its own anchor.** If a job runs at 01:30, it opens the window. Firing a Haiku ping first to open a window the job would have opened anyway is pure waste. There is no anchor logic on the dispatch path at all.
- **A scheduled anchor is only worth it when no job needs the window but you want the boundary moved.** The realistic instance is one morning anchor: a 07:00 ping puts your working-day boundary at 12:00 instead of wherever a 09:00 start would drift it.
- **`--anchor` jobs fire conditionally**: probe first (free, [§4.2](04-usage-oracle.md)), and if any window is open — including one opened from your phone — the job **no-ops to `skipped`**. Fixed cron times are fragile: answer one message at 23:20 and you have a window running to 04:20, so a 01:30 anchor would land inside it, open nothing, and silently break the day's alignment. Conditional firing is what makes an anchor safe to schedule.
- **At most one scheduled anchor exists.** `windowsill doctor` warns if more than one non-terminal `--anchor` job is queued.

```toml
[anchor]
enabled = true
time    = "07:00"
model   = "claude-haiku-4-5"
body    = "Reply with the single word: ok"
```

Haiku both for cost and because it touches neither weekly bucket.

## 5.3 Window accounting (the ceiling from [§1](01-motivation.md))

Every dispatch made while no window was open inserts a row into `window`. That table *is* the accounting.

- `windows_this_month = SELECT count(*) FROM window WHERE opened_at > now - 30d`
- Above `config.queue.max_sessions_per_month` (default **60**): **refuse to dispatch anchors**, warn loudly on real jobs, never silently block real work. An anchor is discretionary; a job you queued is not.
- `windowsill report` and `windowsill stat` show the trailing-30d count. Surface it before it becomes a surprise.

## 5.4 Rate-limit handling

Unattended runs must treat "limit reached" as a **schedulable event, not a failure**.

- Detect from the run's stream and exit status. Keep match patterns in one config-visible constant; they will change. Shape is unknown until observed ([§9](09-findings.md)).
- On detection: probe `/usage` (free) for the true `reset_at`. Fall back to the error's own timestamp, then `now + 5h`.
- Set `sched.blocked_until`. `tick` dispatches nothing until it passes.
- The interrupted job returns to `ready` with its **retry count untouched** and its `conversation_id` recorded. On requeue it is re-dispatched as `--resume <conversation_id>` with body `Continue where you left off.`, unless `--no-resume-on-limit`.

**But treat this as a safety net, not a plan.** Resuming replays context, and context is the cost: at >150k, a single turn ran 15% of a window. A job routinely resumed across three windows is a job scoped wrong — it will cost more than three separate jobs doing a third each, because each resume re-pays for the whole conversation. The README should say this in the job-writing guidance: **scope a job to finish inside one window.** Resume-on-limit exists for the job that overran by 10 minutes, not for the job that was always going to take 12 hours.

## 5.5 Window warming (`--warm`)

A window is five hours long and starts at your **first** request, so a job dispatched at 22:00 owns 22:00–03:00 and gets one window's quota. Warming decouples the two events: a throwaway anchor opens the window at 22:00, the real job waits until shortly before the reset, and the run then spends the tail of window one, hits the boundary, and rolls straight into window two. One `--at` time, close to two windows of quota, back to back.

```
        22:00                                   02:30      03:00              08:00
without   |============ job runs, one window ======|          |
with      A---------------- idle -----------------[== job ==>|=== job ====== ...
          ^ anchor opens window 1                             ^ window 2 opens free
```

### This is not the anchor that §5.2 rejects

[§5.2](#52-anchors-are-lazy-and-mostly-implicit) says firing a ping to open a window the job would have opened anyway is pure waste, and that is still true. The argument assumes the job starts **immediately**, in which case the ping buys nothing. Warming is the case that assumption excludes: the job deliberately does *not* start at the anchor's time, and the gap between the two is the entire point. §5.2's other claim — "a window opened at 01:30 and slept through is a window spent for nothing" — is exactly what warming avoids, because the sleep ends inside the same window it opened.

### The `lead` is a quota budget expressed in minutes

`[warm].lead` (default `30m`) is how long before `reset_at` the real job goes. It is not a duration estimate for the job; it is *how much of window one you intend to spend first*, and the unit only happens to be time.

Measured from this queue's own ledger — 12 completed runs with both usage probes, on a **Pro** subscription:

| | %/min | 100% of a window takes |
|---|---|---|
| fastest run | 5.10 | 20 min |
| median | 2.76 | **36 min** |
| slowest | 0.61 | 164 min |

So on Pro, a 30-minute lead spends roughly 80% of a remaining window at the median rate, and a full one at the fast end. The errors are **not symmetric**:

- **Lead too long** — the job exhausts window one early and sits rate-limited until the reset. This costs waiting only: the requeue does not consume a `--retry` ([§5.4](#54-rate-limit-handling)) and the job resumes on its own conversation.
- **Lead too short** — quota that was already paid for expires unspent, permanently.

Err long. A Max plan buys more tokens per window at the same wall-clock length, so its burn rate in %/min is lower and its lead must be **longer**, not shorter — this is why the number is configuration and not a constant. Divide 100 by your own observed %/min and use that.

### `min_pct_free` is the abort

At dispatch time the warmed window may have been eaten by something else — your phone, another job, a window that was already half spent when the anchor no-op'd into it. Starting a job with 5% left buys one minute of work and a rate limit. `[warm].min_pct_free` (default **20**) is the floor; below it the job **defers** rather than fails, because the right answer is to wait for a window with room in it, not to give up.

### Constraints

- Warming is an additional gate, never a bypass: `blocked_until`, `max_parallel`, the 30-day ceiling, `--hold-jid` and `--window` all still apply first.
- A warm job forces a probe, because its decision depends on live `pct_used` and on `reset_at` — the ledger's real window end, not `opened_at + [window].length`.
- One warmer per job (`jobs.warmed_by`). A second one would open a second window and spend the ceiling for nothing.
- Off by default and per-job. A job without `--warm` takes the same path through `tick` it always did.
