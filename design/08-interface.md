# 8. Interface

## 8.1 CLI

| Command | Behaviour |
|---|---|
| `windowsill sub <file>` | Submit. `-` reads stdin. Prints job id. `--now` bypasses `--at` |
| `windowsill sub -e` | `$EDITOR` on a template header; submit on save |
| `windowsill stat` | id, name, state, model, when, cwd, elapsed. Plus window state and 30d count |
| `windowsill stat -a` | Include terminal jobs from the last 7 days |
| `windowsill show <id>` | Directives, resolved argv, state, conversation id, Δ%, permission denials on the last run, transcript tail |
| `windowsill log <id> [-f]` | Stream the transcript; `-f` follows a running job |
| `windowsill del <id>` | Cancel; SIGTERM the process group if running |
| `windowsill hold <id>` / `windowsill release <id>` | Toggle held |
| `windowsill resume <id>` | `exec` into `claude --resume <conversation_id>` in the job's cwd. **The morning-after path. Make it the smoothest thing in the tool.** |
| `windowsill report [--since 24h]` | Digest: what ran, Δ%, duration, state, branch created, one line each. `denied=N` appended when a run was refused a permission |
| `windowsill usage` | Cached window state. `--probe` forces a fresh read |
| `windowsill tick` | Internal; cron and chain entry point |
| `windowsill gc [--older-than 30d]` | Remove terminal job dirs and worktrees. Never automatic |
| `windowsill doctor` | [§8.2](08-interface.md) |

`windowsill report` is the actual product at 08:00. Readable in five seconds; every line links to `windowsill resume <id>`.

**Timestamps are stored UTC and displayed as bare system local time** — `2026-07-28 02:00`, no offset, no zone name.
This is a single-user tool on one machine: the only clock the reader owns is the one on the wall, and printing `+01:00` after a time only invites arithmetic that has already been done.
`--with-timezone` on any command that prints a time restores the full offset-bearing ISO form, for when you *are* comparing against another machine's clock.

## 8.2 `windowsill doctor`

In rough order of how likely each is to be the thing that's broken:

1. `claude` on PATH, authenticated, and working under `env -i HOME=$HOME PATH=$PATH` (verified 2026-07-17 via `at`; re-check, because credential handling is exactly what an update changes).
2. Cron entry installed and firing (check `logs/windowsill.log` recency).
3. `/usage` parses against live output; every `USAGE_PATTERNS` regex matches — **including the closed state** ([§4.1](04-usage-oracle.md)).
4. Probes are still free ([§4.2](04-usage-oracle.md)): two probes into a closed window; warn loudly if the second reports a live one.
5. Rate-limit patterns still plausible.
6. Window state sane (no `reset_at` in the past marked live; no orphaned pids).
7. ≤1 non-terminal anchor job queued.
8. 30d window count vs `max_sessions_per_month`.
9. Notification channel reachable (ntfy POST, `sendmail` present and executable).

## 8.3 Config

`~/.windowsill/config.toml`:

```toml
[queue]
max_parallel            = 1
default_model           = "claude-opus-4-8"
default_timeout         = "4h"
max_sessions_per_month  = 60      # §5.3; benchmark is 50, this is deliberately near it

[window]
length    = "5h"
peak_band = { days = "mon-fri", start = "13:00", end = "19:00", tz = "Europe/London" }

[anchor]
enabled = true
time    = "07:00"
model   = "claude-haiku-4-5"
body    = "Reply with the single word: ok"

[safety]
allowed_tools   = ["Read", "Edit", "Write", "Grep", "Glob", "Bash", "Task", "TodoWrite", "WebFetch", "WebSearch", "NotebookEdit"]
default_isolate = "worktree"
allow_dirty     = false

[prompt]
enabled = true                    # §3.2; append windowsill's system prompt to every non-anchor job
journal = true                    # ask the job to keep <job dir>/journal.md; adds --add-dir <job dir>
text    = ""                      # replace the built-in prompt wholesale; "{journal}" is substituted

[notify]
default = "none"                  # opt-in; see §8.4

[notify.ntfy]
url   = "https://ntfy.sh"
# topic = "..."                   # no default: an unset topic sends nothing

[notify.email]
# to     = "you@your-real-domain"     # no default: an unset recipient sends nothing
# from   = "noreply@your-real-domain" # unset ⇒ sendmail stamps the invoking user
sendmail = "/usr/sbin/sendmail"
```

`peak_band` is config, not code: it is Anthropic policy and has already changed more than once.

## 8.4 Notifications

**ntfy** (POST to `url/topic`; title = job name + state; body = one-line result, Δ%, and `windowsill resume <id>`) and **email** via `sendmail -t` from the configured noreply address. Nothing else. A failed notification is logged and never fails the job.

**Opt-in, and never guessed.** `[notify].default` is `none`, and neither channel has a default destination. A channel that is on with nowhere to send to sends nothing — no placeholder recipient, no topic, nothing derived from `$USER`/hostname — and records a `notify_skipped` event, because the alternative is a queue that quietly mails a stranger. (It did: the shipped `you@example.org` was mailed for real on 2026-07-30 and refused by the postmaster, "an MX or SRV record indicated no SMTP service".) `--notify <channel>:<destination>` ([§2.3](02-job-file.md)) carries a per-job address/topic for the case where the global config names nobody; `windowsill show` prints the destination a job would actually reach, never just the channel.
