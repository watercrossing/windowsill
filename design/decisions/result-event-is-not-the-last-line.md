# The run's outcome is the last `result` event, not the last line of the stream

**Settled:** 2026-08-28

## Context

The second live run of the weekly Ente rebase job (job 2) mailed `failed (killed, 2 still running)` about a run that had succeeded.
Its `stream.jsonl` is the evidence.
Line 240 is `subtype: "success"`, `is_error: false`, `terminal_reason: "completed"`, and the process exited 0 after 6m13s.

## Decision

| Question | Answer |
|---|---|
| Is the `result` event the last line of `stream.jsonl`? | **No.** Six `system` events followed it: `background_tasks_changed`, `task_updated` (`status: "killed"`), `task_notification` (`status: "stopped"`), twice. That is one triple per background task, torn down as the session exited |
| Does the misread cost anything beyond the label? | **Yes, three things.** `conversation_id` is read from that event, so the job row kept none, and the mail said "No conversation to resume" while `cb87555e-…` sat in the stream. No `result.json` and no `resume.sh` were written. `num_turns` / `duration_ms` were left null in `runs` |

`runner.parse_result_event` scans for the last event with `type: "result"`.
A stream can also carry more than one `result` event, and the run's own outcome is the last of them.

## Consequence

The spec had said "derive the result from the last event", and the code implemented that literally.
It returned `None`, which the outcome mapping reads as "died before emitting a result", which becomes `killed`, which makes the job `failed`.
Everything downstream of the envelope depends on this one function.
Job 1's dir has `result.json` and `resume.sh` and job 2's has neither, which is the cheapest on-disk tell that this happened.
With the fix, the true outcome is `incomplete` ([blocked-not-failed.md](blocked-not-failed.md)).

The word "killed" in those events is about the *tasks*, not the run.
It collides with `runs.outcome`'s vocabulary, which is why the mail read as a plausible report of a real kill rather than as nonsense.

**The standing lesson for the next reader of a job's mail:** the mail is derived, and the stream is the record.
`windowsill show <id>` and the job dir's `stream.jsonl` outrank the subject line.
