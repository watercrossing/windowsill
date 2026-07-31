# 7. Job lifecycle

```
queued ──(--hold)──> held ──(release)──┐
   │                                   │
   └──(deps met, --at reached,     ready ──> running ──┬──> done
       window policy satisfied)──>   ▲                 ├──> failed    (retries exhausted)
                                     │                 ├──> expired   (--before passed)
                                     │                 ├──> skipped   (anchor, window already open)
                                     │                 ├──> cancelled
                                     └─(rate limit /   └──> blocked   (needs a human)
                                        retry)
```

`done`, `failed`, `expired`, `skipped`, `cancelled` are terminal. Terminal jobs keep their job dir until `windowsill gc`.

**`blocked` means "needs a human", and is deliberately not terminal.** A run lands here when its main thread ended while work it had dispatched was still live — a `Task` subagent or a backgrounded `Bash`. The result envelope cannot tell you this: it reads `subtype: "success"`, `is_error: false`, `stop_reason: "end_turn"`, identical to a clean finish, while the run's own closing text says the work is still going. The discriminator is the stream's `system` events — a `task_started` (or a `background_tasks_changed` entry) with no matching `task_notification` carrying `status: "completed"` before the stream ends. Note that `status: "stopped"` is *not* a completion; it is what a subagent gets when the orchestrator quits out from under it.

Not terminal, because every consequence of the terminal set is one a blocked job wants the other side of: `ccstat` without `-a` hides terminal jobs, and a blocked job is precisely the one you need to see at breakfast; `windowsill del` should still cancel it; a name should still resolve to it so `ccresume <name>` works. It cannot loop, because the scheduler dispatches by explicit state (`queued`, `ready`), never by "not terminal". A blocked job also never consumes a retry — re-running the body would start a second copy of work whose first copy may still be live.

There is **no** signal for "the agent stopped to ask a question", and `blocked` is not set from a guess at one: `stop_reason` takes only `end_turn`/`tool_use`, and inferring from trailing punctuation would misfire on every run that ends on a rhetorical question. The state exists so a real signal can set it when one appears; today only the live-background-task evidence does.

If the machine was down through a job's `--at`, run it late — **unless** `--before` was set and has passed ⇒ `expired`, notify, do not silently run stale work at breakfast.
