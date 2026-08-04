# Overnight run notes — 2026-08-04 (todo-4)

Things you should know at breakfast without reading the transcript.
Nothing here is a blocker unless it says **BLOCKED**.

---

## **BLOCKED** — the repo rename broke your PyPI trusted publisher

Do this before you tag 0.2.0, or the publish will fail at the upload step.

You registered the publisher against `watercrossing/**ccq**`. The repo is now
`watercrossing/**windowsill**`. A trusted publisher matches the OIDC token's
`repository` claim **literally** — GitHub's rename redirect does not rewrite
the claim. So the token will say `windowsill`, the publisher will say `ccq`,
and PyPI will reject the upload.

This is exactly the trap the last run's §6 step 2 warned about; the warning
just arrived one release too late to be free.

**Fix (two minutes, only you can do it):** PyPI → your account → Publishing →
the `windowsill` project → delete the old `ccq` publisher and add one with:

- Owner → `watercrossing`
- Repository name → `windowsill`
- Workflow name → `pypi.yaml`
- Environment name → **leave blank**

Everything else about the release path is unchanged and still correct.

## §1 The rename is done

- `~/.ssh/gh-ccq` → `~/.ssh/gh-windowsill` (and `.pub`).
- `~/.ssh/config`: `Host gh-ccq` → `Host gh-windowsill`, `IdentityFile`
  updated. Backup at `~/.ssh/config.bak-20260804`.
- Remote renamed `gh-ccq` → `gh-windowsill`, URL now
  `gh-windowsill:watercrossing/windowsill.git`. `main` still tracks it.
- `pyproject.toml` URLs now point at `/windowsill`, plus a new `Changelog` URL.

Verified: `ssh -T gh-windowsill` → "Hi watercrossing/windowsill!", and
`git fetch` succeeds. The deploy key still works against the renamed repo.

## §4 Why you never got a blocked email — no bug, a category error

**The two todo-3 stalls were rate limits, not `blocked`.** Those are different
states and only one of them notifies.

The event log for job 20 settles it:

```
21:21  rate_limited   rate_limit_event; blocked until 2026-08-01T01:49
02:34  rate_limited   rate_limit_event; blocked until 2026-08-01T07:00
07:03  done
07:03  notified       email
```

Two rate limits, no notification; one completion, one email. That is the code
working as designed. `runner.py:1170` requeues a rate-limited run, and only the
terminal branch at `:1203` calls `_notify_job` — *"Only terminal states notify
— a requeued job hasn't finished"* (`:967`). A requeued job is back at `ready`
and will run again, so nothing terminal has happened to report.

The confusion is that **two unrelated things are both called "blocked"**:

| | what it means | notifies? |
|---|---|---|
| `sched.blocked_until` | the *queue* is rate-limited and paused | no — by design |
| job state `blocked` (§7) | a *run* ended with background work still live | yes |

You hit the first one twice. The second has **never happened** — the distinct
job states in the database are `done`, `cancelled`, `failed`, `running`,
`ready`. That is still true today.

### Correcting the last run's note

The previous BLOCKED.md called the blocked email "the least-tested branch in
`mail.py`". That was overstated. It is unit-tested at both levels —
`tests/test_mail.py:184-223` composes and asserts on blocked-state mail, and
`tests/test_runner.py:976` asserts a run with live background work comes back
`outcome=incomplete, job_state=blocked`. And the wiring is sound: `incomplete`
is excluded from both the requeue and the retry branch, so it falls through to
the terminal `else` and *does* send. What is untested is not the code, it is
reality — no real job has ever gone incomplete.

### The real gap this exposes (not a defect — a decision for you)

A job that hits a rate limit at 21:21 and finishes at 07:03 tells you
**nothing for ten hours**. Silence is indistinguishable from a crashed tick.

I did not change this, because "notify on requeue" is a design change with a
spam failure mode, and the last run's §2 lesson was that this kind of thing
should not land unattended. If you want it, the shape I would build is a
default-off `[notify] on_rate_limit` that sends the one line the queue already
knows: *"paused, resuming 02:00"*. Say the word and it is a small change.

## §3 CI — added, and here is how it meets your ten-commit habit

`.github/workflows/ci.yaml`: the suite on Python **3.13 and 3.14**, plus
`ruff check` and `ruff format --check`, on every push to `main` and every PR.
This is the obviously-right one — you were shipping a `Programming Language ::
Python :: 3.14` classifier that nothing had ever tested, and "the tests pass"
meant "on Ingolf's machine". Baseline right now: **545 pass, lint clean**.

**On your ten-commits-then-push workflow.** It integrates fine, with one honest
caveat. A `push` event triggers **one** CI run, against the tip of what you
pushed — not one per commit. So:

- Good: ten commits cost you one CI run, not ten. No queue backup, no cost.
- Bad: a red build means "somewhere in those ten", and finding where is a local
  `git bisect`. CI cannot tell you which commit broke it because it never saw
  the middle eight.

So the workflow is the smaller half of the answer. The bigger half is
`.githooks/pre-push`, which runs those same three checks **before** the batch
leaves the machine — while the commits are still cheap to amend or reorder.
It is opt-in and I did not enable it for you:

```
git config core.hooksPath .githooks     # enable
git push --no-verify                    # skip once
```

I'd turn it on. It costs ~10s per push and it is the only thing that keeps a
red main from being a bisect.

Also: `concurrency` cancels superseded runs on branches but **never on `main`**,
so every batch you push to main keeps its own permanent green/red record.

## §5 `disallowed_tools` — closed, per your call

You said the permission mode is what defines the boundary for you, so the
question is now settled rather than open. Recorded in three places so it does
not get re-litigated: `design/03-execution-model.md` §3.3,
`design/09-findings.md` (the findings table), and the comment on
`safety.allowed_tools` in `config.py`. The reasoning written down is that a
config key would be a *second* mechanism overlapping `--permission-mode`, and
the two could only ever disagree.

Nothing was implemented and nothing was removed — `safety.allowed_tools` still
does what it did (auto-approve, not a sandbox).

## §6 0.2.0 is staged

Version bumped, `CHANGELOG.md` written (0.1.0 reconstructed from the tag,
0.2.0 covering this run), `Changelog` URL added. `v0.1.0` is tagged at the
previous HEAD, so everything in this run is exactly the 0.2.0 delta.

Verified locally: **545 tests pass**, ruff clean, `uv build` produces both
artifacts, `twine check --strict` **PASSED** on both.

Release path, once the publisher above is fixed: push, then create the GitHub
release with tag `v0.2.0`. The workflow still guards tag-vs-version and fails
before upload on a mismatch.

Two things worth knowing:

- `pypi.yaml` has now **run once for real** (0.1.0 shipped), so it is no longer
  the untested-on-the-release-that-matters risk it was last week.
- The 0.1.0 metadata live on PyPI still shows the `ccq` URLs. That cannot be
  edited — a released version's metadata is immutable. 0.2.0 fixes it going
  forward; the old page will keep pointing at a redirect.
