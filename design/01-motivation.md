# 1. Motivation and objective

The binding constraint on a Claude Code subscription is **not** the weekly cap — it is the 5-hour window. Observed: the 5-hour limit is hit two or more times per working day while the weekly allowance sits at ~13% consumed. Weekly headroom is enormous; per-window headroom during waking hours is exhausted daily.

A window starts with your first message and lasts 5 hours ([support docs](https://support.anthropic.com/en/articles/11014257-about-claude-max-plan-usage)). Windows are not banked: one you sleep through is gone. Windows are account-global — Claude Code, the app, and claude.ai all draw from the same one.

**Objective: harvest the overnight windows that would otherwise expire unused, up to a configured monthly ceiling.**

The ceiling is not decoration. The same support article states that exceeding **50 sessions per month** may result in Anthropic limiting access — a flexible, case-by-case benchmark rather than a hard cut-off, but enforced by judgement about what looks excessive. A week holds ~33 windows; using all of them is ~143/month, roughly 3x the benchmark. Current organic usage is already ~60–90/month. So the realistic target is **+1 to +2 windows per night** (~+10 to +14/month), and the tool must count and enforce this itself ([§5.3](05-windows.md)). A queue that maximises window count is a queue that gets its account throttled.

Secondary benefit: weekday peak hours reportedly drain the window faster (5–11am PT ≈ 13:00–19:00 Europe/London). Work deferred out of that band is cheaper per unit. Reported behaviour, not documented policy — keep it in config, never in code.

## Non-goals

- Cloud execution — Routines does that, without local file access.
- Recurring cadences — Desktop scheduled tasks do that well.
- Sleep/wake management. Target is an always-on VM.
- Multi-machine scheduling. Job records are local and stay local.
- A web UI.

## Nomenclature — read this before naming anything

Three distinct things are called "session". Mixing them will cause real bugs:

| Concept | This spec calls it | Where you meet it |
|---|---|---|
| The 5-hour billing segment | **window** | `/usage` line 1 ("Current session: 15% used"); the 50/month benchmark |
| A Claude Code conversation with a `session_id` | **conversation** | `--resume`, `~/.claude/projects/`, `/usage`'s "6 sessions" |
| One execution attempt of a queued job | **run** | This tool only |

The identifier `session` MUST NOT appear in the codebase. `/usage` uses the word for two of the three, in one screen of output.
