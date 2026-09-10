"""windowsill configuration: ``~/.windowsill/config.toml``.

Loads the user's config file with :mod:`tomllib` and fills in anything
missing — at any nesting level — with the defaults documented in
design/08-interface.md §8.3. If the file doesn't exist at all, the defaults are used
as-is.

``peak_band`` (see design/08-interface.md §8.3) is Anthropic pricing/rate-limit policy
that changes over time, so it is loaded here purely as data — a plain
nested mapping under ``window.peak_band`` — and no code in this module
interprets or hardcodes it.

The rest of the tool consumes configuration through :func:`load_config`,
which returns a :class:`Config` — an ordinary ``dict`` of the merged
settings, plus the path it was read from (or would have been read from).
Both the config file path and the "home" directory it's resolved under
can be overridden, so callers (and tests) never need to touch the real
``~/.windowsill``.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

# The exact defaults shown in the design/08-interface.md §8.3 config example.
DEFAULTS: dict[str, Any] = {
    "queue": {
        "max_parallel": 1,
        "default_model": "opus",
        "default_timeout": "4h",
        # The trailing-30d window ceiling. The benchmark is 50 windows a
        # month; 60 sits deliberately near it, not comfortably above.
        "max_sessions_per_month": 60,
    },
    "window": {
        "length": "5h",
        "peak_band": {
            "days": "mon-fri",
            "start": "13:00",
            "end": "19:00",
            "tz": "Europe/London",
        },
    },
    "anchor": {
        "enabled": True,
        "time": "07:00",
        "model": "haiku",
        "body": "Reply with the single word: ok",
    },
    # §5.5 window warming. A subscription window is five hours long and starts
    # at your *first* request, so a job dispatched at 22:00 owns 22:00-03:00 and
    # nothing else does. Warming splits that: a throwaway prompt opens the
    # window at the job's `--at` time, the real job waits until `lead` before
    # the window resets, and the run then spends the tail of window one and
    # rolls straight into window two -- close to two windows of quota
    # back-to-back instead of one.
    #
    # `lead` is the only number that needs tuning per subscription, and it is
    # *how much quota you want to burn first*, expressed as time. On Pro the
    # run history says a heavy job takes a window from 0% to 100% in about
    # half an hour (runs 10 and 16 in the ledger: 30 and 33 minutes), so 30m
    # spends roughly a full window's worth in the tail. A Max plan buys more
    # tokens per window and therefore needs a longer lead to spend them.
    #
    # `min_pct_free` is the abort: if the warmed window has been eaten by
    # something else in the meantime, there is nothing left to burn and the
    # job is better off waiting for a window of its own than starting one
    # minute of work before a rate limit.
    #
    # The warming prompt itself is not configured here -- it is an anchor, and
    # uses `[anchor].model` and `[anchor].body`.
    "warm": {
        "lead": "30m",
        "min_pct_free": 20,
    },
    "safety": {
        # design/09-findings.md, "Settled 2026-07-31": `--allowed-tools` was
        # found by experiment to be an *additive* auto-approve list -- it does
        # not remove or bound anything (`--disallowed-tools` is the flag that
        # actually narrows the surface; windowsill does not set it and will
        # not -- closed 2026-08-04, the boundary is `--permission-mode`). So
        # naming a tool here costs nothing in restriction, and *omitting* one
        # an unattended orchestrator needs is a loaded gun: it does nothing
        # today, but the day permission prompting actually engages (a
        # `--permission-mode` change, a future CLI default, a sandboxed
        # environment), every job missing it starts getting denied. `Task` is
        # the one that matters most and is not obviously "a tool" to trim as
        # unused -- it's how an orchestrator spawns the subagents this queue
        # exists to run unattended (see: the run that wrote this comment).
        # `TodoWrite`, `WebFetch`, `WebSearch`, `NotebookEdit` fill out the
        # rest of what an unattended orchestrator routinely reaches for.
        "allowed_tools": [
            "Read",
            "Edit",
            "Write",
            "Grep",
            "Glob",
            "Bash",
            "Task",
            "TodoWrite",
            "WebFetch",
            "WebSearch",
            "NotebookEdit",
        ],
        "default_isolate": "worktree",
        "allow_dirty": False,
    },
    # §3.2: windowsill appends a short system prompt to every non-anchor job,
    # telling it the one thing its own body can't know -- that nobody is
    # watching, so a question ends the run -- and asking it to keep a status
    # journal at `<job dir>/journal.md`. This is a *default*, not a new
    # capability: `--append-system-prompt` has always been passable straight
    # through as a `#$` directive, and a job that names any system-prompt flag
    # of its own (`--append-system-prompt`, `--append-system-prompt-file`,
    # `--system-prompt`, `--system-prompt-file`) gets its own, untouched, with
    # nothing of windowsill's added -- which is also the free per-job opt-out:
    # `#$ --append-system-prompt ""`. Anchor jobs (§5.2) are never injected.
    #
    # The two switches are separate because they cost differently. `enabled`
    # buys ~250 tokens once, in the cached prefix: free. `journal` is the
    # expensive half -- every update is an Edit/Write whose call *and* result
    # stay in context for the rest of the run, so ten updates is ~3% of a
    # 150k window, compounding. Default on anyway, because the failure it
    # insures against costs 100%: `--retry` and `--no-resume-on-limit` both
    # re-dispatch with a fresh conversation and the original body, throwing an
    # entire window's work away. Turn it off for jobs that always finish in
    # one window. `text` replaces the built-in prompt wholesale (`{journal}`
    # is substituted with the absolute journal path); empty means "use the
    # built-in", so the default is never something you can only take or leave.
    "prompt": {
        "enabled": True,
        "journal": True,
        "text": "",
    },
    # §8.4: notifications are opt-in, and there is no default destination for
    # either channel. A shipped recipient is an address that bounces -- the
    # placeholder that used to sit here was mailed for real and refused by the
    # postmaster ("an MX or SRV record indicated no SMTP service") -- and a
    # shipped ntfy topic is a *public* one. So: no `to`, no `topic`, and
    # `default = "none"` until the user says otherwise.
    # `on_rate_limit` is the one notification that fires for something other
    # than a finished job. A rate-limited run is requeued, not terminal, so the
    # normal path stays silent by design (§7) -- which means a job that stalls
    # at 21:21 and finishes at 07:03 says nothing for ten hours, and silence is
    # indistinguishable from a crashed tick. Default off because the failure
    # mode is spam: a job with `--retry 3` that keeps hitting the ceiling sends
    # one of these per attempt.
    "notify": {
        "default": "none",
        "on_rate_limit": False,
        "ntfy": {
            "url": "https://ntfy.sh",
        },
        "email": {
            "sendmail": "/usr/sbin/sendmail",
        },
    },
}


class Config(dict):
    """The fully-resolved windowsill configuration.

    A plain ``dict`` of the merged settings (defaults overridden by
    whatever was in the TOML file), so callers use it the obvious way,
    e.g. ``config["queue"]["default_model"]``. ``path`` records the
    config file that was consulted, whether or not it actually existed.
    """

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        super().__init__(data)
        self.path = path


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base``, returning a new dict.

    Nested dicts (TOML tables) are merged key by key; any other value
    (including lists) in ``override`` replaces the corresponding value
    in ``base`` outright.
    """
    merged = dict(base)
    for key, value in override.items():
        base_value = merged.get(key)
        if isinstance(value, dict) and isinstance(base_value, dict):
            merged[key] = _deep_merge(base_value, value)
        else:
            merged[key] = value
    return merged


#: Where state lives under the home directory.
STATE_DIR_NAME = ".windowsill"


def state_dir(home: Path | None = None) -> Path:
    """Return the directory holding the DB, config, logs, and job dirs.

    Always ``<home>/.windowsill``.
    """
    base = home if home is not None else Path.home()
    return base / STATE_DIR_NAME


def default_config_path(home: Path | None = None) -> Path:
    """Return ``<home>/.windowsill/config.toml`` (see :func:`state_dir`).

    Uses the real ``Path.home()`` when ``home`` is not given — callers
    that must not touch the real filesystem (e.g. tests) should always
    pass ``home`` explicitly.
    """
    return state_dir(home) / "config.toml"


def load_config(config_path: Path | None = None, *, home: Path | None = None) -> Config:
    """Load windowsill's configuration, defaults filled in.

    Resolution of the file to read:

    - ``config_path``, if given, is used verbatim.
    - otherwise ``<home>/.windowsill/config.toml`` — using the real home
      directory unless ``home`` is given.

    If the resolved file doesn't exist, the returned :class:`Config` is
    just the defaults. If it exists, its contents are deep-merged over
    the defaults, so a user file that sets only a handful of keys still
    yields a fully-populated config.
    """
    path = config_path if config_path is not None else default_config_path(home)

    defaults = copy.deepcopy(DEFAULTS)

    if not path.exists():
        return Config(defaults, path)

    with path.open("rb") as f:
        user_data = tomllib.load(f)

    merged = _deep_merge(defaults, user_data)
    return Config(merged, path)
