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
  jobs forever -- and since the cache would then report the window live,
  nothing would re-probe to correct it.
- **No year in the timestamp.** The year is inferred by choosing the candidate
  (``now.year`` ± 1) that puts the reset nearest to now and in the future.
- **The timezone is parenthesised.** Resolve it via :mod:`zoneinfo`, store UTC.
- **The extra weekly bucket's name is read, not hardcoded, and the bucket is
  optional.** Support docs said Opus-only; the client printed ``Fable``, and
  on 2026-07-28 printed no extra bucket at all. Absent is ``None``, not a
  parse failure (design/decisions/extra-weekly-bucket-is-optional.md).

Everything after the weekly lines -- the "What's contributing to your limits
usage?" block -- is deliberately not parsed. It is machine-local ("does not
include other devices or claude.ai") and counts conversations, not windows,
so it must never feed a dispatch decision. The session and week lines are
server-side account truth.

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
from typing import Any, Callable, Iterable, Sequence
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
    # the reset clause itself: "resets Jul 17, 7:09pm (Europe/London)" -- and,
    # on the hour, "resets Jul 28, 7am (Europe/London)" with no ":mm" at all
    # (observed live 2026-07-28, §9). The minutes are optional for that reason:
    # requiring them read a live 74%-used window as *closed*.
    "reset": re.compile(
        r"resets\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2}),\s+(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>[ap]m)\s*\((?P<tz>[^)]+)\)"
    ),
}

#: argv appended to ``claude_cmd`` for a probe. ``--output-format json`` gives
#: the parseable envelope (§3.2); ``/usage`` is the metadata read (§4.2).
USAGE_ARGV_SUFFIX: tuple[str, ...] = ("-p", "/usage", "--output-format", "json")

# ---------------------------------------------------------------------------
# rate-limit signals (§5.4) -- one constant, because the shape is still open
# ---------------------------------------------------------------------------

#: How a run says "limit reached" rather than "failed". §9 lists the shape as
#: an open question -- exit code, a subtype, or only a human string -- so both
#: known channels are matched and every pattern lives here, where a future
#: observation can correct it without touching the detection logic.
RATE_LIMIT_PATTERNS: dict[str, re.Pattern[str]] = {
    # the human string, in a result event's text or its subtype
    "message": re.compile(r"(?:usage|rate)[ _-]?limit(?:s)?[ _-]?(?:reached|exceeded|error)|rate[ _-]?limited", re.IGNORECASE),
    # "Claude AI usage limit reached|1753900000" -- the epoch tail carries the reset
    "epoch": re.compile(r"limit reached\|(?P<epoch>\d{9,})", re.IGNORECASE),
}

#: ``rate_limit_event.rate_limit_info.status`` values that mean the request was
#: let through. Anything else is a limit (§5.4). ``allowed_warning`` is the
#: "you are close" heartbeat, not a refusal.
RATE_LIMIT_ALLOWED_STATUSES: frozenset[str] = frozenset({"allowed", "allowed_warning"})

#: Where a rate-limit event carries its reset time. Read as epoch seconds or as
#: an ISO-8601 string -- which one it is has not been observed.
_RESET_FIELDS: tuple[str, ...] = ("resetsAt", "resets_at", "reset_at", "retryAt")

_MONTHS = {name: i for i, name in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

# the aggregate weekly bucket; any *other* "Current week (X)" line is the extra
# (model-specific) bucket, whose name we read rather than assume (§4).
_AGGREGATE_WEEKLY_BUCKET = "all models"


@dataclass
class ParsedUsage:
    """One parse of ``/usage``. ``reset_at`` (and the ``is_open`` derived from
    it) is the only field the dispatcher needs; the rest feed the ``window``
    ledger (§6) and ``windowsill report``. All datetimes are UTC-aware."""

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
    minute = int(match["min"] or 0)
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
# rate-limit detection (§5.4)
# ---------------------------------------------------------------------------


@dataclass
class RateLimitSignal:
    """A run that stopped because of a limit, not because it failed (§5.4).

    ``source`` says which channel tripped -- useful in the events log while
    §9's open question is still open. ``reset_at`` is whatever the signal
    itself claimed, UTC-aware, or ``None``: the caller prefers a fresh
    ``/usage`` probe over this and falls back to it, never the other way.
    """

    source: str
    detail: str = ""
    reset_at: datetime | None = None


def _reset_from_field(value: Any) -> datetime | None:
    """Read a reset time given as epoch seconds (int, float, or digit string)
    or as an ISO-8601 timestamp. Anything else is ``None``."""
    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _rate_limit_from_event(event: dict[str, Any]) -> RateLimitSignal | None:
    info = event.get("rate_limit_info") or {}
    status = info.get("status")
    if status is None or status in RATE_LIMIT_ALLOWED_STATUSES:
        return None
    reset_at = None
    for field in _RESET_FIELDS:
        reset_at = _reset_from_field(info.get(field))
        if reset_at is not None:
            break
    return RateLimitSignal(source="rate_limit_event", detail=f"status={status}", reset_at=reset_at)


def _rate_limit_from_result(event: dict[str, Any]) -> RateLimitSignal | None:
    if not event.get("is_error"):
        return None
    text = " ".join(str(event.get(key, "")) for key in ("subtype", "result", "error", "stop_reason"))
    if not RATE_LIMIT_PATTERNS["message"].search(text):
        return None
    epoch = RATE_LIMIT_PATTERNS["epoch"].search(text)
    reset_at = _reset_from_field(epoch["epoch"]) if epoch else None
    return RateLimitSignal(source="result", detail=str(event.get("subtype", "error")), reset_at=reset_at)


def detect_rate_limit(events: Iterable[dict[str, Any]]) -> RateLimitSignal | None:
    """Whether a run's event stream says it hit a limit (§5.4).

    Two channels, both matched because §9 hasn't settled which one a real
    limit uses: a ``rate_limit_event`` reporting a status that isn't an
    allowed one, and an erroring ``result`` event whose text matches
    :data:`RATE_LIMIT_PATTERNS`. A signal carrying its own reset time wins
    over one that doesn't -- the caller wants the most specific answer it can
    get before falling back to a probe.

    Returns ``None`` for an ordinary run, including the informational
    ``rate_limit_event`` that fires on *every* run: treating that as a limit
    would block the whole queue for five hours on a job that succeeded.
    """
    best: RateLimitSignal | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "rate_limit_event":
            signal = _rate_limit_from_event(event)
        elif kind == "result":
            signal = _rate_limit_from_result(event)
        else:
            continue
        if signal is None:
            continue
        if best is None or (best.reset_at is None and signal.reset_at is not None):
            best = signal
    return best


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

    Exposed separately from :func:`probe` so ``windowsill doctor`` can inspect the
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

    That a probe is free is a behavioural finding on one client version
    (2026-07-17), not a documented guarantee; if ``/usage`` ever starts
    costing a window, the failure is silent and expensive
    (design/decisions/probes-are-free.md). Each probe returns a
    ``session_id`` and may leave a conversation record on disk; it does not
    move the local conversation counter.
    """
    envelope = invoke(run=run, claude_cmd=claude_cmd)
    resolved_now = now if now is not None else datetime.now(timezone.utc)
    return parse_usage(envelope.get("result", ""), now=resolved_now)
