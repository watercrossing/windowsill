# Overnight run notes — 2026-07-31

Things you should know at breakfast without reading the transcript.
Nothing here is a blocker unless it says **BLOCKED**.

## Your crontab was changed (§1)

The `ccq` → `windowsill` rename deletes `.venv/bin/ccq` on the next `uv run`, which
would have made the 15-minute tick fail into your inbox every quarter hour. So the
crontab was updated in the same step:

- Backup: `~/.windowsill/crontab.bak.20260731` (the pre-change file, 8 lines).
- Exactly one line changed, `uv run ccq tick -q` → `uv run windowsill tick -q`.
  `MAILTO`, the bbcweather entry and the comment block are untouched — verified by
  `diff` against the backup, which reported a single-line change.
- Verified afterwards under a cron-like minimal environment:
  `env -i PATH=/home/ibecker/.local/bin:/usr/local/bin:/usr/bin:/bin HOME=/home/ibecker
  uv run windowsill tick -q` exits 0.

To roll back: `crontab ~/.windowsill/crontab.bak.20260731`.

## §2 turned up a real finding: `--allowed-tools` does not restrict anything

§3.3 assumed an `--allowed-tools` allowlist bounds a job's tool surface. It does not.
Established by experiment here on 2026-07-31 against `claude` 2.1.218, with no ambient
permission rules anywhere on this machine:

- `claude -p "run bash echo ..." --allowed-tools Read` → **Bash ran anyway**, exit
  success, `permission_denials: []`. It is an additive *auto-approve* list.
- `claude -p "run bash echo ..." --disallowed-tools Bash` → Bash was **absent from the
  session's tool set entirely**. That is the flag that actually bounds.

So `safety.allowed_tools` is now implemented and no longer dead config, but it buys
pre-approval, not a sandbox. `safety.disallowed_tools` would be the real lever and is
deliberately **not** built — that is a decision for you, not something to land unattended
overnight. The docs no longer claim the boundary exists.

Related: the shipped default omitted `Task`. Harmless today precisely because the flag
does not restrict, but it would have silently broken every subagent-spawning job the
moment permission prompting engaged. Default widened, with a comment saying why `Task` is
load-bearing.

## Note for later: the GitHub remote is still `watercrossing/ccq`

`pyproject.toml`'s `Repository` URL points at `https://github.com/watercrossing/ccq`,
which is the real remote and so was left alone by the rename. Renaming the GitHub repo
is a thing only you can do, and it matters for §6 (PyPI release metadata). Not urgent —
GitHub redirects old URLs — but the release checklist assumes you may want to.
