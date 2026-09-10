# A run that stops with work still live is `blocked`, not `done` or `failed`

**Settled:** 2026-08-28 (confirmed on a real capture); the state predates it

## Context

The result envelope cannot tell a clean finish from a main thread that stopped while work it dispatched was still running, such as a `Task` subagent or a backgrounded `Bash`.
Both read `subtype: "success"`, `is_error: false`, `stop_reason: "end_turn"`, while the run's own closing text says the work is still going.
The discriminator is in the stream's `system` events.
A `task_started` (or a `background_tasks_changed` entry) with no matching `task_notification` carrying `status: "completed"` before the stream ends means the work is still live.
`status: "stopped"` is *not* a completion: it is what a subagent gets when the orchestrator quits out from under it.
`transcript.live_background_tasks` implements this.

On 2026-08-28, job 2 of the weekly Ente rebase job had two backgrounded builds still live when its main thread stopped: `byfox3ke2` "Build both images" and `bm0hdqzjx` "ente web image build completion/errors".
That run was misreported as `failed` ([result-event-is-not-the-last-line.md](result-event-is-not-the-last-line.md)).

## Decision

**Such a run's outcome is `incomplete`, and the job's state is `blocked`, meaning "needs a human".**
Replaying the real capture through the fixed code gives `incomplete`, then `blocked`, then the mail that names the tasks and tells the reader to `ccresume 2`.
The machinery for the right answer already existed; the parse never reached it.

`blocked` is **deliberately not terminal**.
Every consequence of the terminal set is one a blocked job needs to avoid:

- `ccstat` without `-a` hides terminal jobs, and a blocked job is exactly the one you need to see at breakfast.
- `windowsill del` should still cancel it.
- A name should still resolve to it, so `ccresume <name>` works.

It cannot loop, because the scheduler dispatches by explicit state (`queued`, `ready`), never by "not terminal".
A blocked job never consumes a retry either: re-running the body would start a second copy of work whose first copy may still be live.

## Consequence

There is **no** signal for "the agent stopped to ask a question", and `blocked` is not set from a guess at one.
`stop_reason` only ever takes `end_turn` or `tool_use`, and inferring a question from trailing punctuation would misfire on every run that ends on a rhetorical one.
The state exists so that a real signal can set it when one appears.
Today, only the live-background-task evidence does.
