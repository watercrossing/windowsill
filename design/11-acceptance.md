# 11. Acceptance criteria

1. A job submitted at 18:00 with `--at 02:00 --model claude-opus-4-8` runs at ~02:00 unattended and leaves a `done` job dir. ✔
2. `ccq resume <id>` at 08:00 drops into the finished conversation, right cwd, full context. ✔
3. A job that hits the 5-hour limit mid-run does not fail: requeued, resumed from its own conversation id after the window resets, completes. ✔
4. An unknown directive (`#$ --some-new-flag x`) reaches `claude` untouched; `ccq` needs no change. ✔
5. Two jobs chained with `--resume-from` share one conversation across two windows. ✔
6. Six queued jobs in one open window run back-to-back with seconds between them, not 15 minutes. ✔
7. A `--anchor` job scheduled for 07:00 no-ops to `skipped` when a window opened at 04:00 is still live — **including when that window was opened from another device**. ✔
8. Fed the closed-state fixture ([§4](04-usage-oracle.md)), the parser reports closed and dispatches a `--window fresh` job. Fixture, not live. ✔
9. Over a week of normal use, `ccq stat` shows a trailing-30d window count within `max_sessions_per_month`. ✔
10. `kill -9` on a running job leaves a readable partial transcript and a `failed` job, not a corrupt DB. ✔
11. `ccq report` after a night of four jobs fits on one screen, and each line shows Δ%. ✔
