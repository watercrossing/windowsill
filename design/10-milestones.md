# 10. Milestones

**M1 — solves the problem tonight.**
`jobfile`, `db`, `config`, `runner`, `scheduler`, `cli`, README. Cron + `tick`, chain dispatch, serial runs, `--at`, pass-through argv, stdin body, job dir, conversation-id capture, `resume.sh`, `ccq resume`. No `/usage`, no anchors, no retries. Already worth using.

**M2 — survives the night.**
`usage` oracle + probe rule + window table + Δ%. Rate-limit detection → `blocked_until` → requeue-with-resume. `--window`, `--retry`, `--timeout`, `--hold-jid`, `--resume-from`. `ccq report`, notifications.

**M3 — comfort and guardrails.**
Anchors + conditional fire + `max_sessions_per_month`. Worktree isolation. `ccq doctor`, `ccq gc`, `sub -e`, shell completion.

Ship M1 and use it for a night before starting M2.
