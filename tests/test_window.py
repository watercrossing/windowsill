"""Tests for ccq.window -- dispatch policies and window accounting (§5).

No real clock: every predicate takes an explicit ``now``. The peak-band
tests build ``now`` already in the band's own zone so the UTC↔local
conversion under test is exercised without hand-computed offsets. Accounting
tests run against a real tmp_path DB (the ``window`` ledger, §5.3).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import sqlite_utils

from ccq import db, window

LONDON = ZoneInfo("Europe/London")
PEAK = {"days": "mon-fri", "start": "13:00", "end": "19:00", "tz": "Europe/London"}


def make_db(tmp_path: Path) -> sqlite_utils.Database:
    return db.connect(tmp_path / "ccq.db")


# ---------------------------------------------------------------------------
# peak band (§1 secondary benefit; §5.1 offpeak)
# ---------------------------------------------------------------------------


def test_weekday_inside_band_is_peak() -> None:
    now = datetime(2026, 7, 15, 15, 0, tzinfo=LONDON)  # Wed 15:00
    assert window.is_peak(now, PEAK) is True


def test_weekday_outside_band_is_offpeak() -> None:
    now = datetime(2026, 7, 15, 20, 0, tzinfo=LONDON)  # Wed 20:00
    assert window.is_peak(now, PEAK) is False


def test_weekend_is_never_peak() -> None:
    now = datetime(2026, 7, 18, 15, 0, tzinfo=LONDON)  # Sat 15:00
    assert window.is_peak(now, PEAK) is False


def test_band_start_inclusive_end_exclusive() -> None:
    start = datetime(2026, 7, 15, 13, 0, tzinfo=LONDON)
    end = datetime(2026, 7, 15, 19, 0, tzinfo=LONDON)
    assert window.is_peak(start, PEAK) is True
    assert window.is_peak(end, PEAK) is False


def test_is_peak_converts_from_utc() -> None:
    # 14:00 UTC in summer is 15:00 London (BST) -> inside the band.
    now = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    assert window.is_peak(now, PEAK) is True


# ---------------------------------------------------------------------------
# policies (§5.1)
# ---------------------------------------------------------------------------


def test_share_always_permits() -> None:
    peak = datetime(2026, 7, 15, 15, 0, tzinfo=LONDON)
    assert window.policy_permits("share", now=peak, peak_band=PEAK, window_open=True) is True
    assert window.policy_permits("share", now=peak, peak_band=PEAK, window_open=False) is True


def test_offpeak_blocks_during_peak() -> None:
    peak = datetime(2026, 7, 15, 15, 0, tzinfo=LONDON)
    offpeak = datetime(2026, 7, 15, 22, 0, tzinfo=LONDON)
    assert window.policy_permits("offpeak", now=peak, peak_band=PEAK, window_open=None) is False
    assert window.policy_permits("offpeak", now=offpeak, peak_band=PEAK, window_open=None) is True


def test_fresh_requires_a_closed_window() -> None:
    now = datetime(2026, 7, 15, 2, 0, tzinfo=LONDON)
    assert window.policy_permits("fresh", now=now, peak_band=PEAK, window_open=False) is True
    assert window.policy_permits("fresh", now=now, peak_band=PEAK, window_open=True) is False


def test_only_fresh_needs_a_probe() -> None:
    assert window.policy_needs_probe("fresh") is True
    assert window.policy_needs_probe("share") is False
    assert window.policy_needs_probe("offpeak") is False


# ---------------------------------------------------------------------------
# accounting (§5.3)
# ---------------------------------------------------------------------------


def test_windows_in_last_30d_counts_only_recent(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.insert_window(conn, opened_at="2026-07-20T00:00:00+00:00")  # recent
    db.insert_window(conn, opened_at="2026-07-01T00:00:00+00:00")  # recent
    db.insert_window(conn, opened_at="2026-05-01T00:00:00+00:00")  # >30d ago
    now = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
    assert window.windows_in_last_30d(conn, now) == 2


def test_ceiling_predicate(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    now = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
    for _ in range(3):
        db.insert_window(conn, opened_at="2026-07-20T00:00:00+00:00")
    assert window.at_or_over_ceiling(conn, now, max_sessions=5) is False
    assert window.at_or_over_ceiling(conn, now, max_sessions=3) is True
    assert window.at_or_over_ceiling(conn, now, max_sessions=2) is True
