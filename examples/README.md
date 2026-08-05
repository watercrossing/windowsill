# Example job files

Real, runnable job files. Each one is mostly comment: the `#` lines above
the header explain why it is shaped the way it is, and never reach
`claude`. Copy one, change the `--cwd` and the prompt, submit it.

| File | For you if |
|---|---|
| [`01-hello.md`](01-hello.md) | You just installed windowsill and want to know it works. |
| [`02-nightly-tests.md`](02-nightly-tests.md) | You want something useful to happen at 02:00 in a directory of your choosing, and to hear about it. |
| [`03-long-refactor.md`](03-long-refactor.md) | Your job might not finish in one window — `--retry`, rate-limit resume, and the journal a restarted run reads. |
| [`04-pipeline-1-build.md`](04-pipeline-1-build.md) [`-2-test`](04-pipeline-2-test.md) [`-3-report`](04-pipeline-3-report.md) | You have stages that must happen in order — `--hold-jid`, fan-out and fan-in. |
| [`05-isolated-worktree.md`](05-isolated-worktree.md) | The job must not touch the tree you are working in. Read the caveat at the top: `--isolate` is stored but not yet honoured. |
| [`06-anchor.md`](06-anchor.md) | You want to move a 5-hour window boundary somewhere more useful. Explains the window model. |
| [`07-warm.md`](07-warm.md) | The job needs more than one window's quota — `--warm` opens a window early and runs the job across its reset. Read `06` first. |

**Start here:** `01-hello.md`, then `02-nightly-tests.md`. Those two cover
most of what people write. Read `03` before you queue anything big enough
to hit a rate limit.

## Submitting and checking on one

```
ccsub examples/01-hello.md        # prints the job id
ccstat                            # queued, running, held, recently finished
ccstat -a                         # plus terminal jobs from the last 7 days
windowsill show <id>              # directives, resolved argv, transcript tail
windowsill log <id> -f            # follow a running job
ccresume <id>                     # re-enter the conversation, in its own cwd
```

Every job file starts `#!/usr/bin/env ccsub`, so `chmod +x` one and it
submits itself.

Jobs are dispatched by `windowsill tick`, which cron runs every fifteen
minutes (see the README). Nothing here starts until a tick has run —
`windowsill tick` by hand if you do not want to wait.

Address a job by numeric id, or by its `--name` — but a name only
resolves among jobs that have not finished, so once a job is `done` the
id is the handle.
