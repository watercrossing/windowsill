# Overnight run notes — 2026-07-31 / 2026-08-01

Things you should know at breakfast without reading the transcript.
Nothing here is a blocker unless it says **BLOCKED**.

---

# Part two — the night of 2026-08-01 (§3–§6)

## §5 The email was sent — but not the variant you asked for

Delivered 08:01 to i.becker@ucl.ac.uk, accepted by smtp.cs.ucl.ac.uk
(`status=sent`). First windowsill email ever to leave the machine.

It is the **completion** variant, reporting **job 18** — every figure read out of
`windowsill.db`, nothing staged, renderer unmodified.

You asked for the **blocked** variant and did not get it. It could not be produced
honestly: **no job in the database has ever reached state `blocked`**, and the
window was closed, so the only ways to get one were to fake a database row or to
spend window on a job invented purely to fail in an interesting way. You
pre-authorised the fallback, so I took it — but the blocked path is **still
unexercised and still owed**. It remains the least-tested branch in `mail.py`.

## §6 PyPI release — the ten-minute checklist

**Verdict: nearly, but do not tag tonight.** The blocker was in the workflow, not
the package, and it is now fixed (`a06a161`). See "Honest opinion" at the end.

Do these in order. Steps 1–3 are the ones only you can do.

1. **Push.** You are **14 commits ahead** of `watercrossing/ccq`. Everything below
   assumes they are pushed — including `27526be`, which adds the LICENSE.
   Tagging a release right now would build a tree with **no LICENSE and no
   release metadata**. `git push gh-ccq main`.
2. **Decide the GitHub repo name, and do it now if you are going to.** The remote
   is still `watercrossing/ccq`. A PyPI trusted publisher binds to the literal
   `owner/repo`; GitHub's redirect does **not** carry the OIDC claim, so renaming
   *after* you register the publisher silently breaks publishing. Rename first,
   or accept `ccq` as the repo name forever. If you rename, update the three URLs
   in `pyproject.toml:39-41` too.
3. **PyPI account + 2FA.** 2FA is **mandatory**, no opt-out. Accepted: a TOTP app,
   a WebAuthn key (YubiKey etc.), plus one-time recovery codes. Do this before
   step 4 — you cannot register a publisher without it.
4. **Register a *pending* publisher** (the project does not exist on PyPI yet, so
   this is the right flow): PyPI → your account → Publishing. Fields, exactly:
   - PyPI Project Name → `windowsill`
   - Owner → `watercrossing`
   - Repository name → `ccq` *(or the new name from step 2)*
   - Workflow name → `pypi.yaml` — with the extension, matched literally
   - Environment name → **leave blank** (the workflow declares no `environment:`;
     if you fill this in, the two must match exactly or the upload is rejected)
5. **Optional but cheap: a TestPyPI dry run.** Separate account, separate 2FA,
   separate pending publisher, and a second job pointed at
   `https://test.pypi.org/legacy/`. Worth it only because **a PyPI version number
   can never be reused** — if `0.1.0` fails halfway you cannot retry it.
6. **Tag and release.** Create the GitHub release with tag `v0.1.0`. The workflow
   guards that the tag matches `pyproject.toml`'s version and fails before upload
   if not. Publishing the release triggers it.

**Name availability: `windowsill` is FREE.** `pypi.org/pypi/windowsill/json` →
404, unregistered and not squatted. Note the old name `ccq` **is taken** — by an
unrelated, actively-maintained project ("Query your own Claude Code agent history
with DuckDB", 3 releases, last June 2026). So the rename was necessary, not
cosmetic. Free fallbacks if ever needed: `windowsill-queue`, `winsill`.
A pending publisher does **not** reserve the name — someone could still take it
between step 4 and step 6.

### What I found and fixed for you

- **The workflow would NOT have worked** (`a06a161`). Its job-level
  `permissions:` block listed only `id-token: write`. A job-level block
  *replaces* the workflow-level one rather than merging, so everything unlisted
  becomes `none` — the top-level `contents: read` was discarded and
  `actions/checkout` ran with no read access. Fatal specifically because **the
  repo is private**. It uses trusted publishing (OIDC), no API token anywhere,
  which is the right choice.
- `dist/` was not in `.gitignore`. Now is.

### pyproject: in good shape, small gaps

`twine check --strict` passes on both artifacts. Licence metadata is the modern
SPDX form and matches the real MIT LICENSE. Artifacts are clean — the wheel has
all 13 modules and all 5 console scripts; the sdist has README, LICENSE, design/
and tests/, and **none** of `tasks/`, `BLOCKED.md`, `.venv`, `*.db` or
`__pycache__`. Still missing, none blocking: no author email, no Documentation
or Changelog URL (no CHANGELOG exists), and `Programming Language :: Python ::
3.14` is asserted but never tested — **there is no CI test workflow at all**,
`pypi.yaml` is the only file in `.github/workflows/`.

### Honest opinion

Ship it, after steps 1–4. The package is genuinely releasable: 545 tests, clean
artifacts, real metadata. Two caveats I would not paper over — the workflow has
still **never executed**, so its first run is on the release that matters (hence
the TestPyPI suggestion), and there is **no CI**, so "the tests pass" currently
means "they passed on Ingolf's machine". Neither is a reason to wait; both are
reasons to expect the first release to need a `0.1.1`.

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
