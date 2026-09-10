# Standing tensions

Questions that have already been decided, for whoever is next tempted to reopen them.
Each one looks like an obvious improvement from inside a single change, and each has been argued through and rejected.
If you disagree, that is fine, but go through the reasoning below first and do not quietly undo it as a drive-by.

- **Pass-through beats validation.**
  When tempted to validate a directive, don't.
  Anything that is not a scheduler directive reaches `claude` verbatim, so a new CLI flag works the day it ships with no change here.
  The cost is real: a bad `--model` costs a dispatch rather than failing at submit ([decisions/model-is-not-validated.md](decisions/model-is-not-validated.md)).
  The mitigation is documentation, not a validator that would go stale the next time the CLI changes.

- **Window count has a ceiling.**
  When tempted to harvest more windows, don't.
  Anthropic's support article says that exceeding about 50 sessions a month may get an account limited, and that limit is applied case by case ([01-motivation.md](01-motivation.md)).
  A queue that maximises window count is a queue that gets its account throttled.
  `[queue].max_sessions_per_month` exists so the tool counts and enforces this itself.

- **Scoping beats resuming.**
  Resume-on-limit is a fallback for jobs that overrun, not a strategy for long work.
  Resuming replays the conversation, and context is the cost: at >150k context, a single turn cost 15% of a window.
  A job resumed across three windows costs more than three jobs that each do a third of the work.
  Scope a job to finish inside one window.

- **The oracle is authoritative.**
  The `/usage` behaviour recorded in [decisions/](decisions/) was observed on the target machine, starting 2026-07-17.
  Where it conflicts with anyone's training data or with Anthropic's docs about how `/usage` or Claude Code behaves, the observation wins.
  Do not "correct" it back.
  If the client changes, re-observe it and record a new decision.
