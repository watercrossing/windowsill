# 7. Job lifecycle

```
queued ──(--hold)──> held ──(release)──┐
   │                                   │
   └──(deps met, --at reached,     ready ──> running ──┬──> done
       window policy satisfied)──>   ▲                 ├──> failed    (retries exhausted)
                                     │                 ├──> expired   (--before passed)
                                     │                 ├──> skipped   (anchor, window already open)
                                     └─(rate limit /   └──> cancelled
                                        retry)
```

`done`, `failed`, `expired`, `skipped`, `cancelled` are terminal. Terminal jobs keep their job dir until `ccq gc`.

If the machine was down through a job's `--at`, run it late — **unless** `--before` was set and has passed ⇒ `expired`, notify, do not silently run stale work at breakfast.
