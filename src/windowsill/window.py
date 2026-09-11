"""Window policies and accounting.

Two things live here, both consumed by the scheduler:

- **Dispatch policies** (:func:`policy_permits`). ``--window share``
  dispatches any time; ``offpeak`` only outside the configured
  ``peak_band``; ``fresh`` only when a probe (:func:`usage.probe`) reports
  **no** live window -- the big overnight job that should own a whole
  window. :func:`policy_needs_probe` tells the scheduler which policies
  require a probe before deciding, so it never probes on a tick with no
  question to ask.
- **Window accounting.** The ``window`` table *is* the ledger: every
  dispatch made while no window was open inserts a row.
  :func:`windows_in_last_30d` is the trailing-30d count and
  :func:`at_or_over_ceiling` compares it against ``max_sessions_per_month``
  -- the ceiling from design/01-motivation.md that keeps the account off
  Anthropic's "excessive" radar.

``peak_band`` is passed in as plain config data (see
:data:`windowsill.config.DEFAULTS`): it is Anthropic policy that has
already changed, so nothing here is hardcoded -- days, times, and zone are
all read from the mapping.

Clock-injected: every function takes an explicit UTC-aware ``now``; no real
clock is read.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import sqlite_utils

from windowsill import db, usage

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

#: Only ``fresh`` verifies against a probe (:func:`usage.probe`); the rest decide from
#: the clock or unconditionally.
_PROBE_POLICIES = frozenset({"fresh"})


# ---------------------------------------------------------------------------
# peak band (offpeak policy; secondary benefit, see design/01-motivation.md)
# ---------------------------------------------------------------------------


def _parse_days(spec: str) -> set[int]:
    """Parse a ``days`` spec into weekday ints (Mon=0). Accepts ranges
    (``mon-fri``), singles (``sat``), and comma-joins of either."""
    days: set[int] = set()
    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if "-" in part:
            lo, hi = (p.strip() for p in part.split("-", 1))
            start, end = _WEEKDAYS[lo], _WEEKDAYS[hi]
            span = range(start, end + 1) if start <= end else [*range(start, 7), *range(0, end + 1)]
            days.update(span)
        else:
            days.add(_WEEKDAYS[part])
    return days


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def is_peak(now: datetime, peak_band: Mapping[str, Any]) -> bool:
    """Whether ``now`` falls in the peak band (start inclusive, end exclusive).

    ``now`` is any tz-aware datetime; it is converted into the band's own zone
    before the weekday/time test.
    """
    local = now.astimezone(ZoneInfo(peak_band["tz"]))
    if local.weekday() not in _parse_days(peak_band["days"]):
        return False
    start = _parse_hhmm(peak_band["start"])
    end = _parse_hhmm(peak_band["end"])
    current = local.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end  # band wraps past midnight


# ---------------------------------------------------------------------------
# policies
# ---------------------------------------------------------------------------


def policy_needs_probe(policy: str) -> bool:
    """Whether deciding this ``--window`` policy requires a /usage probe."""
    return policy in _PROBE_POLICIES


def policy_permits(
    policy: str,
    *,
    now: datetime,
    peak_band: Mapping[str, Any],
    window_open: bool | None,
) -> bool:
    """Whether a job with this ``--window`` policy may dispatch right now.

    - ``share`` -- always.
    - ``offpeak`` -- only when ``now`` is outside ``peak_band``.
    - ``fresh`` -- only when ``window_open`` is ``False`` (a probe reported no
      live window). ``window_open`` must be supplied for ``fresh``; ``None``
      (not probed) is treated as "cannot confirm closed" ⇒ not permitted, so a
      caller that skipped the required probe fails safe rather than dispatching
      into a live window.

    An unrecognised policy falls back to ``share`` semantics -- ``--window`` is
    a fixed-value scheduler directive, and refusing to dispatch on a typo would
    be worse than treating it as the default.
    """
    if policy == "offpeak":
        return not is_peak(now, peak_band)
    if policy == "fresh":
        return window_open is False
    return True


# ---------------------------------------------------------------------------
# accounting -- the trailing-30d ceiling from design/01-motivation.md
# ---------------------------------------------------------------------------


def record_probe(
    database: sqlite_utils.Database,
    parsed: "usage.ParsedUsage",
    *,
    now_str: str,
    opened_by: int | None = None,
) -> int | None:
    """Record a ``/usage`` probe in the ``window`` table (probes are cached
    regardless, see design/decisions/probes-are-free.md).

    Only an *open* window produces a row: a closed reading is the absence of a
    row (design/decisions/reset-clause-is-the-discriminator.md), never a row
    with a NULL ``reset_at``. Returns the row's id, or ``None`` for a closed
    reading.

    Three cases, in order, and the order is the point -- each exists to stop
    one window being counted twice against :func:`at_or_over_ceiling`'s ceiling:

    1. A row already carries this ``reset_at``: same window, seen again. Update
       it.
    2. ``opened_by`` names a job whose dispatch opened a window that has no
       ``reset_at`` yet (the tick that opened it could not know one). This
       probe is the first read that does, so complete that row in place.
    3. Otherwise insert. A window we merely observed -- opened from a phone,
       say -- still counts: the ceiling measures windows on the account, not
       windows this tool is proud of.
    """
    if not parsed.is_open:
        return None

    reset_at = parsed.reset_at.isoformat()
    fields: dict[str, Any] = dict(
        probed_at=now_str,
        pct_used=parsed.pct_used,
        weekly_pct=parsed.weekly_pct,
        weekly_reset_at=parsed.weekly_reset_at.isoformat() if parsed.weekly_reset_at else None,
        extra_bucket=parsed.extra_bucket,
        extra_pct=parsed.extra_pct,
    )

    existing = list(database["window"].rows_where("reset_at = ?", [reset_at], order_by="id desc", limit=1))
    if existing:
        db.update_window(database, existing[0]["id"], **fields)
        return existing[0]["id"]

    if opened_by is not None:
        pending = list(database["window"].rows_where("opened_by = ? AND reset_at IS NULL", [opened_by], order_by="id desc", limit=1))
        if pending:
            db.update_window(database, pending[0]["id"], reset_at=reset_at, **fields)
            return pending[0]["id"]

    return db.insert_window(database, opened_at=now_str, reset_at=reset_at, opened_by=opened_by, **fields)


def windows_in_last_30d(database: sqlite_utils.Database, now: datetime) -> int:
    """The trailing-30d count of windows *we* opened -- the ledger read behind
    :func:`at_or_over_ceiling`.

    ``opened_at`` is stored as a UTC ISO-8601 string; the cutoff is formatted
    the same way so the ``>=`` is both a correct chronological and lexical
    comparison, as everywhere else in the codebase.
    """
    cutoff = (now - timedelta(days=30)).isoformat()
    return len(list(database["window"].rows_where("opened_at >= ?", [cutoff])))


def at_or_over_ceiling(database: sqlite_utils.Database, now: datetime, *, max_sessions: int) -> bool:
    """Whether the trailing-30d window count has reached ``max_sessions_per_month``.

    At or over the ceiling, anchors are refused outright and real jobs warn but
    still run -- this predicate reports the fact; the policy split is the
    caller's.
    """
    return windows_in_last_30d(database, now) >= max_sessions
