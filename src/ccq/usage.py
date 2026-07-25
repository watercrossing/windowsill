"""The /usage window oracle (design/04-usage-oracle.md §4).

``claude -p "/usage" --output-format json`` returns ground truth about the
account's 5-hour window. The ``--output-format json`` envelope wraps the
human string in ``result`` but does **not** structure it (§4.1), so the text
is parsed here. It is treated as hostile: every regex lives in the one
module-level :data:`USAGE_PATTERNS` constant, because this string will change.

The load-bearing rules, verbatim from §4.1:

- **The reset clause is the sole discriminator.** ``Current session: N% used``
  with a ``· resets …`` clause ⇒ the window is open until that time; the same
  line *without* the clause ⇒ closed. The line is always present, so the
  absence of the line is never the signal.
- **The percentage is display-only.** A window opened by one tiny request
  reads ``0% used · resets …`` -- still open. State is never inferred from %.
- **A missing reset is ``None``, never ``now + 5h``.** Defaulting it would read
  the closed state as a window live until morning and starve ``--window fresh``
  jobs forever.
- **No year in the timestamp.** The year is inferred by choosing the candidate
  (``now.year`` ± 1) that puts the reset nearest to now and in the future.
- **The timezone is parenthesised.** Resolve it via :mod:`zoneinfo`, store UTC.
- **The extra weekly bucket's name is read, not hardcoded** (``Fable`` today).

Clock-injected: :func:`parse_usage` takes an explicit ``now`` and
:func:`probe` an injectable ``run`` command seam, so nothing here reads a real
clock or invokes a real ``claude`` under test.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# patterns -- all of them here, because this string will change (§4.1)
# ---------------------------------------------------------------------------

USAGE_PATTERNS: dict[str, re.Pattern[str]] = {
    # "Current session: N% used[ · resets …]" -- the window line. The reset
    # tail is captured loosely and validated by the "reset" pattern; whether
    # that matches is the open/closed discriminator.
    "session": re.compile(r"Current session:\s*(?P<pct>\d+)%\s*used(?P<reset>[^\n]*)"),
    # "Current week (<bucket>): N% used[ · resets …]" -- one per weekly bucket.
    "week": re.compile(r"Current week \((?P<bucket>[^)]+)\):\s*(?P<pct>\d+)%\s*used(?P<reset>[^\n]*)"),
    # the reset clause itself: "resets Jul 17, 7:09pm (Europe/London)".
    "reset": re.compile(r"resets\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2}),\s+(?P<hour>\d{1,2}):(?P<min>\d{2})\s*(?P<ampm>[ap]m)\s*\((?P<tz>[^)]+)\)"),
}

#: argv appended to ``claude_cmd`` for a probe. ``--output-format json`` gives
#: the parseable envelope (§3.2); ``/usage`` is the metadata read (§4.2).
USAGE_ARGV_SUFFIX: tuple[str, ...] = ("-p", "/usage", "--output-format", "json")

_MONTHS = {name: i for i, name in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

# the aggregate weekly bucket; any *other* "Current week (X)" line is the extra
# (model-specific) bucket, whose name we read rather than assume (§4).
_AGGREGATE_WEEKLY_BUCKET = "all models"


@dataclass
class ParsedUsage:
    """One parse of ``/usage``. ``reset_at`` (and the ``is_open`` derived from
    it) is the only field the dispatcher needs; the rest feed the ``window``
    ledger (§6) and ``ccq report``. All datetimes are UTC-aware."""

    pct_used: int | None
    reset_at: datetime | None
    weekly_pct: int | None = None
    weekly_reset_at: datetime | None = None
    extra_bucket: str | None = None
    extra_pct: int | None = None
    raw: str = ""

    @property
    def is_open(self) -> bool:
        """A window is open iff the session line carried a reset clause (§4.1).
        Derived from ``reset_at`` alone -- never from the percentage."""
        return self.reset_at is not None


# ---------------------------------------------------------------------------
# timestamp resolution (§4.1)
# ---------------------------------------------------------------------------


def _to_24h(hour: str, ampm: str) -> int:
    h = int(hour)
    if ampm == "pm" and h != 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    return h


def _resolve_reset(match: re.Match[str], now: datetime) -> datetime | None:
    """Turn a matched reset clause into a UTC datetime, inferring the year.

    Builds the wall-clock time in the parenthesised zone for each candidate
    year (``now.year`` ± 1), converts to UTC, and picks the nearest one that is
    in the future -- so a "Jan 1" reset seen on 31 Dec rolls into next year.
    """
    month = _MONTHS[match["mon"].title()]
    day = int(match["day"])
    hour = _to_24h(match["hour"], match["ampm"])
    minute = int(match["min"])
    zone = ZoneInfo(match["tz"])

    candidates: list[datetime] = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            local = datetime(year, month, day, hour, minute, tzinfo=zone)
        except ValueError:
            continue  # e.g. Feb 29 in a non-leap candidate year
        candidates.append(local.astimezone(timezone.utc))

    if not candidates:
        return None
    future = [c for c in candidates if c >= now]
    if future:
        return min(future, key=lambda c: c - now)
    return min(candidates, key=lambda c: abs(c - now))


def _reset_from_tail(tail: str, now: datetime) -> datetime | None:
    match = USAGE_PATTERNS["reset"].search(tail)
    return _resolve_reset(match, now) if match else None


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def parse_usage(text: str, *, now: datetime) -> ParsedUsage:
    """Parse ``/usage`` output into a :class:`ParsedUsage`.

    ``now`` must be UTC-aware; it drives year inference only (§4.1). No I/O,
    no clock -- the sole external input beyond ``text`` is ``now``.
    """
    session = USAGE_PATTERNS["session"].search(text)
    pct_used = int(session["pct"]) if session else None
    reset_at = _reset_from_tail(session["reset"], now) if session else None

    weekly_pct: int | None = None
    weekly_reset_at: datetime | None = None
    extra_bucket: str | None = None
    extra_pct: int | None = None

    for week in USAGE_PATTERNS["week"].finditer(text):
        bucket = week["bucket"].strip()
        pct = int(week["pct"])
        if bucket == _AGGREGATE_WEEKLY_BUCKET:
            weekly_pct = pct
            weekly_reset_at = _reset_from_tail(week["reset"], now)
        elif extra_bucket is None:
            extra_bucket = bucket
            extra_pct = pct

    return ParsedUsage(
        pct_used=pct_used,
        reset_at=reset_at,
        weekly_pct=weekly_pct,
        weekly_reset_at=weekly_reset_at,
        extra_bucket=extra_bucket,
        extra_pct=extra_pct,
        raw=text,
    )


# ---------------------------------------------------------------------------
# probe: invoke claude, unwrap the envelope, parse (§4.1/§4.2)
# ---------------------------------------------------------------------------


def _default_run(argv: Sequence[str]) -> str:
    """Run ``argv`` and return its stdout. The one place a real ``claude`` is
    invoked; tests substitute a ``run`` that returns a canned envelope."""
    completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=120, check=False)
    return completed.stdout


def invoke(*, run: Callable[[Sequence[str]], str] = _default_run, claude_cmd: Sequence[str] = ("claude",)) -> dict:
    """Probe ``/usage`` and return the parsed ``--output-format json`` envelope.

    Exposed separately from :func:`probe` so ``ccq doctor`` can inspect the
    envelope directly (e.g. assert ``num_turns == 0`` -- that a probe is still
    free, §4.2)."""
    stdout = run([*claude_cmd, *USAGE_ARGV_SUFFIX])
    return json.loads(stdout)


def probe(
    *,
    now: datetime | None = None,
    run: Callable[[Sequence[str]], str] = _default_run,
    claude_cmd: Sequence[str] = ("claude",),
) -> ParsedUsage:
    """Probe ``/usage`` and return the parsed window state.

    Free and account-global (§4.2): reads server-side truth, sees other
    surfaces, and does not open a window. ``run`` is the injectable command
    seam; ``now`` defaults to the real UTC clock but is injectable for tests.
    """
    envelope = invoke(run=run, claude_cmd=claude_cmd)
    resolved_now = now if now is not None else datetime.now(timezone.utc)
    return parse_usage(envelope.get("result", ""), now=resolved_now)
