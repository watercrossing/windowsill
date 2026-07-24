"""ccq configuration: ``~/.ccq/config.toml``.

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
``~/.ccq``.
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
        "default_model": "claude-opus-4-8",
        "default_timeout": "4h",
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
        "model": "claude-haiku-4-5",
        "body": "Reply with the single word: ok",
    },
    "safety": {
        "allowed_tools": ["Read", "Edit", "Write", "Grep", "Glob", "Bash"],
        "default_isolate": "worktree",
        "allow_dirty": False,
    },
    "notify": {
        "default": "email",
        "ntfy": {
            "url": "https://ntfy.sh",
            "topic": "...",
        },
        "email": {
            "to": "you@example.org",
            "from": "noreply@example.org",
            "sendmail": "/usr/sbin/sendmail",
        },
    },
}


class Config(dict):
    """The fully-resolved ccq configuration.

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


def default_config_path(home: Path | None = None) -> Path:
    """Return ``<home>/.ccq/config.toml``.

    Uses the real ``Path.home()`` when ``home`` is not given — callers
    that must not touch the real filesystem (e.g. tests) should always
    pass ``home`` explicitly.
    """
    base = home if home is not None else Path.home()
    return base / ".ccq" / "config.toml"


def load_config(config_path: Path | None = None, *, home: Path | None = None) -> Config:
    """Load ccq's configuration, defaults filled in.

    Resolution of the file to read:

    - ``config_path``, if given, is used verbatim.
    - otherwise ``<home>/.ccq/config.toml`` — using the real home
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
