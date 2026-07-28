"""Tests for windowsill.config — loading and defaulting ~/.windowsill/config.toml.

These tests never touch the real ~/.windowsill: every call to load_config()
below points at a path under pytest's tmp_path fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from windowsill.config import DEFAULTS, Config, load_config, state_dir

# ---------------------------------------------------------------------------
# Defaults, no file present
# ---------------------------------------------------------------------------


def test_defaults_used_when_no_file_exists(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does-not-exist" / "config.toml")

    assert cfg["queue"]["max_parallel"] == 1
    assert cfg["queue"]["default_model"] == "claude-opus-4-8"
    assert cfg["queue"]["default_timeout"] == "4h"
    assert cfg["queue"]["max_sessions_per_month"] == 60

    assert cfg["window"]["length"] == "5h"
    assert cfg["window"]["peak_band"] == {
        "days": "mon-fri",
        "start": "13:00",
        "end": "19:00",
        "tz": "Europe/London",
    }

    assert cfg["anchor"] == {
        "enabled": True,
        "time": "07:00",
        "model": "claude-haiku-4-5",
        "body": "Reply with the single word: ok",
    }

    assert cfg["safety"] == {
        "allowed_tools": ["Read", "Edit", "Write", "Grep", "Glob", "Bash"],
        "default_isolate": "worktree",
        "allow_dirty": False,
    }

    assert cfg["notify"]["default"] == "email"
    assert cfg["notify"]["ntfy"] == {"url": "https://ntfy.sh", "topic": "..."}
    assert cfg["notify"]["email"] == {
        "to": "you@example.org",
        "from": "noreply@example.org",
        "sendmail": "/usr/sbin/sendmail",
    }


def test_defaults_are_not_shared_mutable_state(tmp_path: Path) -> None:
    """Mutating one loaded config must not affect another load or DEFAULTS."""
    cfg1 = load_config(tmp_path / "config.toml")
    cfg1["queue"]["max_parallel"] = 99
    cfg1["safety"]["allowed_tools"].append("Task")

    cfg2 = load_config(tmp_path / "config.toml")
    assert cfg2["queue"]["max_parallel"] == 1
    assert cfg2["safety"]["allowed_tools"] == ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]

    assert DEFAULTS["queue"]["max_parallel"] == 1
    assert DEFAULTS["safety"]["allowed_tools"] == ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]


def test_returns_config_instance(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "config.toml")
    assert isinstance(cfg, Config)
    # Small, obvious interface: it's just a mapping.
    assert isinstance(cfg, dict)


# ---------------------------------------------------------------------------
# Partial user overrides merge over defaults
# ---------------------------------------------------------------------------


def test_partial_top_level_override_merges_with_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
        [queue]
        max_parallel = 4
        """
    )

    cfg = load_config(config_file)

    assert cfg["queue"]["max_parallel"] == 4
    # untouched queue keys keep their defaults
    assert cfg["queue"]["default_model"] == "claude-opus-4-8"
    assert cfg["queue"]["default_timeout"] == "4h"
    assert cfg["queue"]["max_sessions_per_month"] == 60

    # untouched sections are entirely default
    assert cfg["window"]["length"] == "5h"
    assert cfg["anchor"]["enabled"] is True


def test_partial_nested_override_preserves_sibling_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
        [notify.ntfy]
        topic = "my-ccq-topic"
        """
    )

    cfg = load_config(config_file)

    assert cfg["notify"]["ntfy"]["topic"] == "my-ccq-topic"
    # sibling default inside the same nested table survives
    assert cfg["notify"]["ntfy"]["url"] == "https://ntfy.sh"
    # unrelated notify keys survive
    assert cfg["notify"]["default"] == "email"
    assert cfg["notify"]["email"]["to"] == "you@example.org"


def test_peak_band_override_merges_field_by_field(tmp_path: Path) -> None:
    """peak_band is config, not code: overriding one field of the inline
    table must not clobber the other fields' defaults."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
        [window]
        peak_band = { tz = "America/New_York" }
        """
    )

    cfg = load_config(config_file)

    assert cfg["window"]["peak_band"]["tz"] == "America/New_York"
    assert cfg["window"]["peak_band"]["days"] == "mon-fri"
    assert cfg["window"]["peak_band"]["start"] == "13:00"
    assert cfg["window"]["peak_band"]["end"] == "19:00"
    # window.length wasn't touched
    assert cfg["window"]["length"] == "5h"


def test_list_values_are_replaced_not_merged(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
        [safety]
        allowed_tools = ["Read"]
        """
    )

    cfg = load_config(config_file)

    assert cfg["safety"]["allowed_tools"] == ["Read"]
    assert cfg["safety"]["default_isolate"] == "worktree"
    assert cfg["safety"]["allow_dirty"] is False


# ---------------------------------------------------------------------------
# Full file: every nested table from the §8.3 example loads verbatim
# ---------------------------------------------------------------------------


FULL_CONFIG_TOML = """
[queue]
max_parallel            = 1
default_model           = "claude-opus-4-8"
default_timeout         = "4h"
max_sessions_per_month  = 60

[window]
length    = "5h"
peak_band = { days = "mon-fri", start = "13:00", end = "19:00", tz = "Europe/London" }

[anchor]
enabled = true
time    = "07:00"
model   = "claude-haiku-4-5"
body    = "Reply with the single word: ok"

[safety]
allowed_tools   = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
default_isolate = "worktree"
allow_dirty     = false

[notify]
default = "email"

[notify.ntfy]
url   = "https://ntfy.sh"
topic = "..."

[notify.email]
to       = "you@example.org"
from     = "noreply@example.org"
sendmail = "/usr/sbin/sendmail"
"""


def test_full_example_config_round_trips(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(FULL_CONFIG_TOML)

    cfg = load_config(config_file)

    assert cfg["queue"] == {
        "max_parallel": 1,
        "default_model": "claude-opus-4-8",
        "default_timeout": "4h",
        "max_sessions_per_month": 60,
    }
    assert cfg["window"] == {
        "length": "5h",
        "peak_band": {
            "days": "mon-fri",
            "start": "13:00",
            "end": "19:00",
            "tz": "Europe/London",
        },
    }
    assert cfg["anchor"] == {
        "enabled": True,
        "time": "07:00",
        "model": "claude-haiku-4-5",
        "body": "Reply with the single word: ok",
    }
    assert cfg["safety"] == {
        "allowed_tools": ["Read", "Edit", "Write", "Grep", "Glob", "Bash"],
        "default_isolate": "worktree",
        "allow_dirty": False,
    }
    assert cfg["notify"] == {
        "default": "email",
        "ntfy": {"url": "https://ntfy.sh", "topic": "..."},
        "email": {
            "to": "you@example.org",
            "from": "noreply@example.org",
            "sendmail": "/usr/sbin/sendmail",
        },
    }


# ---------------------------------------------------------------------------
# Path / home overrides — must never touch the real ~/.windowsill
# ---------------------------------------------------------------------------


def test_home_override_resolves_to_dot_windowsill_config_toml(tmp_path: Path) -> None:
    ccq_dir = tmp_path / ".windowsill"
    ccq_dir.mkdir()
    (ccq_dir / "config.toml").write_text(
        """
        [queue]
        max_parallel = 7
        """
    )

    cfg = load_config(home=tmp_path)

    assert cfg["queue"]["max_parallel"] == 7
    assert cfg.path == ccq_dir / "config.toml"


def test_home_override_with_missing_file_gives_defaults(tmp_path: Path) -> None:
    cfg = load_config(home=tmp_path)
    assert cfg["queue"]["max_parallel"] == 1
    assert cfg.path == tmp_path / ".windowsill" / "config.toml"


# ---------------------------------------------------------------------------
# state_dir — the rename must not strand a pre-rename ~/.ccq
# ---------------------------------------------------------------------------


def test_state_dir_is_dot_windowsill_on_a_fresh_home(tmp_path: Path) -> None:
    assert state_dir(tmp_path) == tmp_path / ".windowsill"


def test_state_dir_falls_back_to_a_pre_rename_dot_ccq(tmp_path: Path) -> None:
    # The queue, the ledger, and any in-flight job dirs live in here.
    (tmp_path / ".ccq").mkdir()

    assert state_dir(tmp_path) == tmp_path / ".ccq"


def test_state_dir_prefers_dot_windowsill_when_both_exist(tmp_path: Path) -> None:
    # Once the user has moved (or started) a .windowsill, the old directory
    # stops being consulted — otherwise the fallback would be permanent.
    (tmp_path / ".ccq").mkdir()
    (tmp_path / ".windowsill").mkdir()

    assert state_dir(tmp_path) == tmp_path / ".windowsill"


def test_explicit_config_path_wins_over_home(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    (home_dir / ".windowsill").mkdir(parents=True)
    (home_dir / ".windowsill" / "config.toml").write_text("[queue]\nmax_parallel = 2\n")

    explicit_file = tmp_path / "explicit-config.toml"
    explicit_file.write_text("[queue]\nmax_parallel = 3\n")

    cfg = load_config(explicit_file, home=home_dir)

    assert cfg["queue"]["max_parallel"] == 3
    assert cfg.path == explicit_file


def test_malformed_toml_raises(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("this is not [ valid toml")

    with pytest.raises(Exception):
        load_config(config_file)
