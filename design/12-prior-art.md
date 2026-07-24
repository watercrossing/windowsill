# 12. Prior art

- `gruckion/claude-scheduler` — one-shot scheduling with conversation-id capture and click-to-resume. Closest existing thing; steal the resume UX.
- `jshchnz/claude-code-scheduler` — cron JSON configs, git worktree isolation. Steal the worktree pattern.
- Claude Code docs: `scheduled-tasks` (`/loop` + Cron* tools, session-scoped), `desktop-scheduled-tasks` (SKILL.md + YAML frontmatter — Anthropic's own take on a job file), `cli-reference` (`-p`, `--resume`, `--output-format`, `--max-turns`).
- Anthropic support: [About Claude's Max Plan Usage](https://support.anthropic.com/en/articles/11014257-about-claude-max-plan-usage) — window definition and the 50-sessions/month benchmark. Re-read before changing [§1](01-motivation.md) or [§5.3](05-windows.md).
