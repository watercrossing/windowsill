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
