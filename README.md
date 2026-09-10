# windowsill

A local batch queue for Claude Code.

Where you leave things overnight, in the window, to be dealt with in the morning.

## The problem

The binding constraint on a Claude Code subscription usually isn't the weekly cap — it's the 5-hour usage **window**.
A window starts on your first message and lasts 5 hours; whatever's left when you stop working is gone, not banked.
Most of that headroom evaporates overnight while you sleep.

windowsill is a cron-driven queue that submits Markdown job files — prompt bodies with a small directive header — and dispatches them into windows you'd otherwise waste, up to a monthly ceiling you configure.
You write jobs before bed; you run `ccresume <id>` in the morning to pick a conversation back up.

A quick note on words this project is careful about, since Claude Code overloads "session":

| Concept | Called | Where you meet it |
|---|---|---|
| The 5-hour billing segment | **window** | `/usage`'s "Current session: N% used"; the monthly benchmark |
| A Claude Code conversation with a `session_id` | **conversation** | `--resume`, `~/.claude/projects/` |
| One execution attempt of a queued job | **run** | windowsill internals only |

## Install

```
uv tool install windowsill
```

That puts five commands on your PATH: the four short verbs `ccsub`, `ccstat`, `ccdel`, `ccresume`,
and `windowsill`, the dispatcher that carries all of them plus the rest — `windowsill show`, `windowsill report`, `windowsill tick`.
`windowsill --help` is the one place that lists everything.

From a checkout of this repo, run it straight from the project environment instead:

```
uv run ccstat
```

`uv run` uses the project's own venv with windowsill installed editable,
so your source edits take effect immediately — prefer it while working in the repo.
`uvx --from . ccstat` runs it too, but builds the package into a separate, ephemeral tool
environment each time and runs that snapshot rather than your live source:
handy for a one-off try, redundant once the code is already local.

### Shell completion

```
windowsill completions install
```

Detects your shell from `$SHELL` (or take `--shell bash|zsh|fish` to skip that) and writes
completion files for all five entry points — `windowsill`, `ccsub`, `ccstat`, `ccdel`, `ccresume` —
straight into the directory that shell already scans on startup, so there's no rc file to edit
and nothing to go stale: the script just re-invokes the program on each TAB press, so a CLI change
shows up the next time you press TAB, not the next time you reinstall completions.

- bash and fish autoload by filename, so install lands one real file — holding all five entry
  points' scripts — plus a symlink per remaining name in
  `~/.local/share/bash-completion/completions/` and `~/.config/fish/completions/` respectively.
- zsh has no such fixed directory, so it gets five real files instead, under `~/.zsh/completions`;
  if that isn't already on your `$fpath`, `install` prints the `fpath+=(...)` line to add.

Prefer an `eval` line in your rc over installed files? `windowsill completions <bash|zsh|fish>`
emits the `windowsill` script alone to stdout:

```
eval "$(windowsill completions bash)"
```

Either way, a TAB press costs about 70-90ms now — imperceptible; it was ~220ms before commit
`6daf5c3` trimmed `cli.py`'s import-time cost.

## Running on a schedule (cron)

windowsill dispatches jobs two ways, and the split follows from one asymmetry.
Idle time inside an open window is lost for good; idle time with no window open costs nothing, because the next window only starts at the next message.
So latency matters only between jobs in a live window.
The fast path is the *chain*: when a job finishes it kicks the next tick itself, so back-to-back jobs in one open window start seconds apart.
Cron is the *liveness floor* underneath that — it catches a broken chain, an `--at` coming due, and a rate-limit `blocked_until` expiring.
Fifteen minutes is fine precisely because it is never the latency path.
Cron, not a systemd timer: no `loginctl enable-linger`, no unit files, and it works the same on any always-on box.

Add one line to your crontab (`crontab -e`):

```cron
PATH=/home/you/.local/bin:/usr/bin:/bin
*/15 * * * * windowsill tick -q
```

- **`-q`** suppresses `tick`'s one-line summary, so cron has no output to mail you every 15 minutes.
- **`PATH`** matters because cron runs with a nearly empty environment.
  Both `windowsill` *and* `claude` have to be findable on it — a tick with a due job spawns `windowsill run`, which spawns `claude`.
  `uv tool install windowsill` puts `windowsill` in `~/.local/bin`, where `claude` usually already lives; confirm with `command -v windowsill claude` and set the `PATH=` line to match.
  (If you run from a checkout instead of installing, use `uv run --project /path/to/windowsill windowsill tick -q` and make sure `uv` is on that `PATH` too.)
- **`HOME`** is set by cron automatically, which is all `claude` needs to find its credentials — a non-interactive job with no TTY authenticates fine (verified in [`design/decisions/auth-survives-cron.md`](design/decisions/auth-survives-cron.md)).

To keep a heartbeat and capture any errors, redirect to a log instead:

```cron
*/15 * * * * windowsill tick >> "$HOME/.windowsill/logs/tick.log" 2>&1
```

Drop the `-q` when you redirect — the summary line becomes a useful "it's alive" trace in the log rather than mail in your inbox.
windowsill creates `~/.windowsill/logs/` for you on first run.

## The job file

A job is a UTF-8 text file: an optional shebang, a header of `#$` directives, a blank line, then the prompt body — verbatim, no templating, any length.

**[`examples/`](examples/) has runnable blueprints** — from a three-line "does my install work" job up to held pipelines, isolated worktrees and anchors. Each one is mostly comment, explaining why it is shaped the way it is. Start at [`examples/01-hello.md`](examples/01-hello.md).

```
#!/usr/bin/env ccsub
#$ --name refactor-auth
#$ --at 2026-07-18T02:00
#$ --model opus
#$ --cwd ~/code/myapp
#$ --resume-from audit-auth
#$ --window fresh
#$ --isolate worktree
#$ --max-turns 60
#$ --retry 2
#$ --notify email

Continue the auth refactor from where the audit job left off.

Done means: every call site of `legacy_verify()` migrated to `verify_token()`,
`pytest tests/auth` green, and a single squashed commit on the branch.
If you hit a blocker, do not stop — write what you tried and what you would
need to `BLOCKED.md`, then continue with the next call site.
```

Parsing rules that matter:

- The `#!` line, if present, is ignored — job files can be `chmod +x` and self-submitted.
  `ccsub` takes the file as its only argument, so `#!/usr/bin/env ccsub` makes `./refactor-auth.md` submit itself.
- Directives are lines matching `^#\$\s+(.*)$`, and must appear before the first non-directive, non-blank, non-shebang line.
  Once prose starts, the header is closed — a `#$` line can't resume later in the file.
- The first blank line after the header ends the header.
  Everything after it, to EOF, is the body: verbatim, unmodified.
- Bare `#` lines and `#$ #...` inside the header are comments.
- A trailing `\` continues a directive onto the next line.
- Directive arguments are tokenised with shell-like quoting rules (so `--name "has spaces"` works as you'd expect).

**Two kinds of directive.**
windowsill recognises a fixed set of *scheduler* directives (below); everything else is passed through verbatim, in order, to `claude` as argv.
`#$ --model opus` becomes `claude --model opus`.
This means a new `claude` flag works the day it ships, with no change here — and windowsill never validates or rejects a directive it doesn't recognise; unknown ones flow straight through and `claude` complains if they're wrong.
That cuts both ways, and `--model` is where it bites: the name is checked by the API at dispatch, not by `ccsub` at submit, so a bad one is a 404 hours later rather than an error you see when you type it.
`claude` takes either a **bare family alias** — `opus`, `sonnet`, `haiku` — or a **full versioned id** — `claude-opus-5`, `claude-haiku-4-5`. The halves do not mix: `claude-opus` (prefix, no version) and `opus-5` (version, no prefix) are both rejected. Use the alias unless you specifically want a version pinned.

### Scheduler directives

| Directive | Default | Meaning |
|---|---|---|
| `--name <slug>` | filename stem | Human label. Unique only among non-terminal jobs |
| `--at <when>` | ASAP | Earliest start. ISO-8601 |
| `--before <time>` | none | Do not start after this; job becomes `expired` if missed. Compared as a plain string against the current UTC time, so write it in UTC |
| `--window <policy>` | `share` | `fresh` \| `share` \| `offpeak` |
| `--anchor` | false | This is an anchor job: a trivial prompt whose only purpose is to open a window. Does not choose a model — name one with `--model`; `[anchor].model` applies only to the anchors the scheduler spawns for `--warm` |
| `--warm` | false | Open the window at `--at`, but run the job near the window's end. See below |
| `--warm-lead <dur>` | config (`30m`) | How long before the window resets a `--warm` job goes |
| `--cwd <path>` | submit-time cwd | Working directory. `~` expanded at submit |
| `--log <path>` | job dir only | Also write the rendered transcript here. A trailing `/` (or an existing directory) means "in there", as `<id>-<name>.md`; otherwise it is the file. Relative to `--cwd` |
| `--resume <conversation_id>` | — | Continue an explicit conversation |
| `--resume-from <job>` | — | Continue the conversation produced by a completed job (resolved at dispatch) |
| `--hold-jid <job>[,<job>]` | — | Wait for those jobs to reach `done` |
| `--retry <n>` | `0` | Retries on non-rate-limit failure. Rate-limit requeues are free and unbounded |
| `--priority <-20..19>` | `0` | Tie-break within a window; lower runs first |
| `--isolate <mode>` | config | `none` \| `worktree` |
| `--allow-dirty` | false | Permit running against a dirty tree |
| `--notify <channel>[:<dest>]` | config (`none`) | `none` \| `ntfy` \| `email`. May carry this job's own recipient or topic: `email:me@example.com` |
| `--hold` | false | Submit `held`; needs `windowsill release` |
| `--timeout <dur>` | `4h` | Wall-clock kill |
| `--no-resume-on-limit` | false | On rate-limit requeue, restart from the original body instead of resuming |

`--resume` and `--resume-from` are mutually exclusive; both end up as `claude --resume <id>` at dispatch.
`--resume` is the one case where a scheduler directive shadows a real `claude` flag.

Every directive above is parsed *and* acted on today, with two exceptions: `--isolate worktree` is stored but not yet honoured (runs happen in `--cwd` regardless), and `--allow-dirty` is not yet checked.
When it lands, `worktree` runs the job in `~/.windowsill/worktrees/<id>-<name>` on a new branch `windowsill/<name>`, left for morning review; worktrees are never removed automatically, only by `windowsill gc`.

### `--warm`: spend the tail of one window and roll into the next

A window is five hours long and starts at your **first** request, so a job dispatched at 22:00 owns 22:00–03:00 and gets one window's quota. `--warm` splits those two events apart:

```
        22:00                                   02:30      03:00              08:00
without   |============ job runs, one window ======|          |
with      A---------------- idle -----------------[== job ==>|=== job ====== ...
          ^ anchor opens window 1                             ^ window 2 opens free
```

At `--at`, the scheduler spawns a throwaway anchor that opens the window and stops. The job itself waits until `[warm].lead` before the window resets, then runs — spending what is left of window one, hitting the boundary, and continuing into window two. One scheduled time, close to two windows of quota.

```toml
[warm]
lead         = "30m"   # how much of window one to spend first, expressed as time
min_pct_free = 20      # below this, defer rather than start a run that cannot finish
```

`lead` is the only number worth tuning, and it is a **quota budget** that happens to be measured in minutes. On a Pro plan this queue's own ledger puts the median burn at 2.76 %/min, i.e. about **36 minutes** to spend a whole window — so 30m spends most of one. The errors are not symmetric: too long only costs waiting (a rate-limit requeue consumes no `--retry` and resumes the same conversation), while too short wastes quota permanently. Err long. A Max plan buys more tokens per window at the same wall-clock length, so it needs a **longer** lead, not a shorter one.

`--warm` cannot be combined with `--window fresh` — warming waits for the tail of an open window and `fresh` waits for there being no window at all, so the pair would defer on every tick forever. `ccsub` refuses it at submit. A warm job has no use for `fresh` regardless: the window it crosses into is empty by construction.

Every warming refusal is a *defer*, never a terminal state, so a warm job that never gets its window sits `ready` and quiet rather than failing. `windowsill show <id>` prints the `warm` events explaining which gate held it. Give the job a `--before` if being late makes it worthless.

`design/05-windows.md` §5.5 has the full reasoning and the measurements.
`examples/07-warm.md` is a commented, runnable job file.

## Commands

Three commands you'll use constantly, each its own executable, after Grid Engine's `qsub`/`qstat`/`qdel`:

- **`ccsub <file>`** — submit a job.
  Use `-` to read from stdin.
  Prints the job id.
- **`ccstat`** — see what's queued, running, held, or recently finished: id, name, state, model, when, cwd, elapsed.
- **`ccresume <id>`** — the morning-after command.
  `exec`s into `claude --resume <conversation_id>` in the job's working directory, so you drop straight back into last night's conversation.

Working today (milestones M1 and M2). Every row is also reachable as a subcommand of `windowsill`,
which is the form cron and the job chain use:

| Command | Also | Behaviour |
|---|---|---|
| `ccsub <file>` | `windowsill sub` | Submit. `-` reads stdin. Prints job id. `--now` bypasses `--at` |
| `ccstat` | `windowsill stat` | id, name, state, model, when, cwd, elapsed, plus window state and 30d count |
| `ccstat -a` | `windowsill stat -a` | Include terminal jobs from the last 7 days |
| `ccdel <id>` | `windowsill del` | Cancel; SIGTERM the process group if running |
| `ccresume <id>` | `windowsill resume` | `exec` into `claude --resume <conversation_id>` in the job's cwd |
| — | `windowsill show <id>` | Directives, resolved argv, state, conversation id, Δ%, permission denials, transcript tail |
| — | `windowsill log <id> [-f]` | Stream the transcript; `-f` follows a running job |
| — | `windowsill hold <id>` / `windowsill release <id>` | Toggle held |
| — | `windowsill usage [--probe]` | Cached window state; `--probe` reads `/usage` fresh (max once per 10s) |
| — | `windowsill report [--since 24h]` | Digest of what ran: state, duration, Δ%, and a `ccresume` line each |
| — | `windowsill tick` | Internal; cron and chain entry point |

Times are printed in your machine's own local time with no zone attached — `2026-07-28 02:00`.
Pass `--with-timezone` to any of these for the offset-bearing ISO form instead.

Designed but not implemented yet — do not rely on these:

| Command | Intended behaviour |
|---|---|
| `windowsill sub -e` | `$EDITOR` on a template header; submit on save |
| `windowsill gc [--older-than 30d]` | Remove terminal job dirs and worktrees |
| `windowsill doctor` | Environment/config sanity check |

Correspondingly, the two things those cover — `--isolate worktree` and the dirty-tree refusal — are part of the design but not yet functional.
Everything else is live: window policies decide dispatch from a `/usage` probe, rate limits requeue instead of failing, `--retry` retries, and finished jobs notify.

## Notifications

Off until you ask for them, and windowsill will not invent an address to send to.

```toml
# ~/.windowsill/config.toml
[notify]
default       = "email"    # or "ntfy", or "none" (the default)
on_rate_limit = false      # ping when a run is paused at the window ceiling

[notify.email]
to       = "you@your-real-domain"
sendmail = "/usr/sbin/sendmail"

[notify.ntfy]
url   = "https://ntfy.sh"
topic = "your-private-topic"
```

There is no default recipient and no default ntfy topic.
A channel that is on with nowhere to send to sends nothing and records a `notify_skipped` event — it will not fall back to a placeholder, and it will not guess from `$USER` and your hostname.
A single job can carry its own destination with `#$ --notify email:me@example.com`, which is the whole configuration if you only want mail from one job.
`windowsill show <id>` prints the address a job would actually reach.

The mail arrives when the job finishes, and it covers **every** attempt, not just the last one.
A job that hit the window ceiling twice before finishing gets `(3 runs)` in the subject and an Attempts section listing each run — when it started, how long it ran, how it ended, and what it said as it stopped — over a totals line: run count, wall clock from the first start to the last finish (the ten hours, most of which was waiting), time actually running, turns, and window % spent.
That percentage is the only cost windowsill records — there are no token counts and no dollar figures anywhere in it — and because the window resets between attempts, the total is a floor rather than a sum. The mail says so.

`on_rate_limit = true` adds one short notification when a run is paused at the ceiling: when the queue will resume, the usage block, and the fact that the pause cost no retry.
It is off by default because a job with `--retry 3` that keeps hitting the ceiling sends one per attempt.

## Similar tools

Running Claude Code unattended is a small, crowded field.
What separates these tools isn't the queue — it's what *triggers* a dispatch.

- **[`claude-code-queue`](https://pypi.org/project/claude-code-queue/)** — *triggered by hitting a limit.*
  Runs, gets rate-limited, waits out the reset, retries.
  Markdown files with YAML frontmatter, priorities, a prompt bank, and a `/queue` skill it installs into Claude Code so the assistant can suggest queueing on its own.
  The closest neighbour by far; see below.
- **[`jshchnz/claude-code-scheduler`](https://github.com/jshchnz/claude-code-scheduler)** — *triggered by the wall clock.*
  A Claude Code plugin rather than a standalone CLI: you install it from the marketplace and schedule in natural language ("every weekday at 9am review yesterday's code").
  Optional git worktree mode commits to a branch so your working tree stays clean — the pattern `--isolate worktree` follows.
  The most established of these by some margin.
- **[`dnvriend/claude-code-scheduler`](https://github.com/dnvriend/claude-code-scheduler)** — *triggered by the wall clock,* plus interval and file-watch.
  Unrelated to the above despite the identical name.
  A PyQt6 desktop app with a REST API on :5679, a Job→Task→Run hierarchy, and profiles for AWS Bedrock and Z.AI.
- **[`gruckion/claude-scheduler`](https://github.com/gruckion/claude-scheduler)** — *triggered by the wall clock,* one-shot.
  Small, but it captures the conversation id and hands you click-to-resume notifications — the ergonomics `ccresume` is aiming at.
- **Claude Code's own scheduling** — *triggered by the wall clock.*
  `/loop` with the `Cron*` tools schedules within a session; the desktop app's scheduled tasks use a `SKILL.md` with YAML frontmatter, which is Anthropic's own take on a job file and worth reading before changing the job-file format.

Every one of these except `claude-code-queue` dispatches on a clock.
A clock knows what time it is; it doesn't know whether you have a window open.

`claude-code-queue` is the closest neighbour, and the overlap is real: Markdown job files with a directive header, a queue, retries, and an awareness that the 5-hour window is the thing that actually hurts.
The difference is which direction the tool faces.
`claude-code-queue` is **reactive** — it exists so that hitting a limit doesn't block you, and it waits a window out.
windowsill is **opportunistic** — it exists to spend windows that would otherwise expire unused, and it treats capacity as a budget rather than as free-until-blocked.

Three things follow from that, none of which have a counterpart above:

- **Window state is read, not inferred.**
  A `/usage` probe ([`design/04-usage-oracle.md`](design/04-usage-oracle.md)) is free and answers *before* dispatch, which is what makes `--window fresh` and `--window offpeak` expressible at all.
  Learning a window's state by crashing into it can only ever produce retry logic.
- **Anchors.**
  An anchor doesn't create capacity, it moves a boundary — and it fires conditionally, no-opping if a window is already open.
  See [`design/05-windows.md`](design/05-windows.md) §5.2 for why a fixed cron time can't do this.
- **A ceiling.**
  30-day rolling window accounting against a configured monthly limit.

See `design/index.md` for the full design, including the execution model, window accounting, and SQLite schema.
