"""Tests for ccq.usage -- the /usage window oracle (design/04-usage-oracle.md §4).

Fixture-first and clock-injected (design/00-instructions.md §0.3): the parser
is pinned to the verbatim §4 fixtures -- **both** the open and closed forms,
because a parser tested only against the open branch never meets the closed
one, which is the branch that decides "window closed, dispatch a fresh job".
No `claude` is ever invoked: `probe` takes an injectable `run` seam that
returns a canned `--output-format json` envelope. No real clock: every parse
takes an explicit `now`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ccq import usage

FIXTURES = Path(__file__).parent / "fixtures"
OPEN_TEXT = (FIXTURES / "usage_open.txt").read_text()
CLOSED_TEXT = (FIXTURES / "usage_closed.txt").read_text()

# The open fixture was captured mid-afternoon on 2026-07-17; the closed one at
# ~02:00 the same night (§4). Anchoring `now` there keeps every reset a
# near-future one, which is what the year-inference rule expects.
NOW_OPEN = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
NOW_CLOSED = datetime(2026, 7, 17, 2, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# the discriminator: reset clause present ⇒ open; absent ⇒ closed (§4.1)
# ---------------------------------------------------------------------------


def test_open_fixture_is_open() -> None:
    parsed = usage.parse_usage(OPEN_TEXT, now=NOW_OPEN)
    assert parsed.is_open is True
    assert parsed.reset_at is not None


def test_closed_fixture_is_closed() -> None:
    parsed = usage.parse_usage(CLOSED_TEXT, now=NOW_CLOSED)
    assert parsed.is_open is False
    assert parsed.reset_at is None


def test_closed_state_is_the_missing_reset_clause_not_the_percentage() -> None:
    # 0% with a reset clause is an OPEN window opened by one tiny request --
    # the percentage is display-only and must never decide state (§4.1).
    text = "Current session: 0% used · resets Jul 17, 7:09pm (Europe/London)\n"
    parsed = usage.parse_usage(text, now=NOW_OPEN)
    assert parsed.is_open is True
    assert parsed.pct_used == 0
    assert parsed.reset_at is not None


def test_missing_reset_is_none_never_now_plus_5h() -> None:
    # §4.1: never default a missing reset_at to now+5h -- that reads closed as
    # live and starves --window fresh jobs forever.
    parsed = usage.parse_usage(CLOSED_TEXT, now=NOW_CLOSED)
    assert parsed.reset_at is None


# ---------------------------------------------------------------------------
# percentages and buckets
# ---------------------------------------------------------------------------


def test_session_pct_parsed() -> None:
    assert usage.parse_usage(OPEN_TEXT, now=NOW_OPEN).pct_used == 15
    assert usage.parse_usage(CLOSED_TEXT, now=NOW_CLOSED).pct_used == 0


def test_weekly_all_models_bucket() -> None:
    parsed = usage.parse_usage(OPEN_TEXT, now=NOW_OPEN)
    assert parsed.weekly_pct == 1
    assert parsed.weekly_reset_at is not None


def test_extra_bucket_name_read_not_hardcoded() -> None:
    parsed = usage.parse_usage(OPEN_TEXT, now=NOW_OPEN)
    assert parsed.extra_bucket == "Fable"
    assert parsed.extra_pct == 0


def test_extra_bucket_follows_a_renamed_model() -> None:
    # §4: "read the bucket name from the output; do not hardcode a model."
    text = "Current session: 0% used\nCurrent week (all models): 3% used\nCurrent week (Sonnet): 2% used\n"
    parsed = usage.parse_usage(text, now=NOW_OPEN)
    assert parsed.extra_bucket == "Sonnet"
    assert parsed.extra_pct == 2


# ---------------------------------------------------------------------------
# timestamp: tz → UTC, year inference (§4.1)
# ---------------------------------------------------------------------------


def test_reset_converted_to_utc() -> None:
    # Jul 17 7:09pm Europe/London is BST (+1) ⇒ 18:09 UTC.
    parsed = usage.parse_usage(OPEN_TEXT, now=NOW_OPEN)
    assert parsed.reset_at == datetime(2026, 7, 17, 18, 9, tzinfo=timezone.utc)


def test_reset_stored_timezone_is_utc() -> None:
    parsed = usage.parse_usage(OPEN_TEXT, now=NOW_OPEN)
    assert parsed.reset_at.tzinfo == timezone.utc


def test_year_inference_picks_nearest_future() -> None:
    text = "Current session: 5% used · resets Jul 17, 7:09pm (Europe/London)\n"
    parsed = usage.parse_usage(text, now=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc))
    assert parsed.reset_at.year == 2026


def test_year_inference_new_years_eve_rolls_forward() -> None:
    # now = 31 Dec 23:30 UTC; a "Jan 1, 5:59am" reset is next year, not this.
    text = "Current session: 5% used · resets Jan 1, 5:59am (Europe/London)\n"
    now = datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc)
    parsed = usage.parse_usage(text, now=now)
    assert parsed.reset_at.year == 2027
    assert parsed.reset_at > now


def test_year_inference_new_years_eve_same_day_future_stays_this_year() -> None:
    # a "Dec 31, 11:59pm" reset seen at 23:30 on Dec 31 is later the same day.
    text = "Current session: 5% used · resets Dec 31, 11:59pm (Europe/London)\n"
    now = datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc)  # 22:30 UTC = 22:30 GMT
    parsed = usage.parse_usage(text, now=now)
    assert parsed.reset_at.year == 2026


# ---------------------------------------------------------------------------
# probe: envelope unwrap + no real claude (§4.1/§3.2)
# ---------------------------------------------------------------------------


def _envelope(text: str, **extra) -> str:
    payload = {"type": "result", "subtype": "success", "is_error": False, "result": text, "num_turns": 0, "session_id": "probe-1"}
    payload.update(extra)
    return json.dumps(payload)


def test_probe_unwraps_json_envelope() -> None:
    calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(argv)
        return _envelope(CLOSED_TEXT)

    parsed = usage.probe(now=NOW_CLOSED, run=fake_run)
    assert parsed.is_open is False
    assert parsed.pct_used == 0
    # the probe asks claude for /usage as structured json
    assert "/usage" in calls[0]
    assert "json" in " ".join(calls[0])


def test_probe_open_window() -> None:
    parsed = usage.probe(now=NOW_OPEN, run=lambda argv: _envelope(OPEN_TEXT))
    assert parsed.is_open is True
    assert parsed.pct_used == 15
    assert parsed.reset_at == datetime(2026, 7, 17, 18, 9, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# rate-limit detection (§5.4) -- shape still open (§9), so both channels
# ---------------------------------------------------------------------------


def _allowed_event(**extra):
    info = {"status": "allowed", "rateLimitType": "five_hour"}
    info.update(extra)
    return {"type": "rate_limit_event", "rate_limit_info": info}


def test_no_rate_limit_in_an_ordinary_run() -> None:
    events = [
        {"type": "system", "subtype": "init"},
        _allowed_event(),
        {"type": "result", "subtype": "success", "is_error": False, "result": "done"},
    ]
    assert usage.detect_rate_limit(events) is None


def test_allowed_warning_is_not_a_limit() -> None:
    # The "you're getting close" heartbeat must not block the queue for 5h.
    assert usage.detect_rate_limit([_allowed_event(status="allowed_warning")]) is None


def test_rejected_rate_limit_event_is_detected_with_its_reset() -> None:
    reset_epoch = 1785200000
    events = [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": reset_epoch}}]

    signal = usage.detect_rate_limit(events)

    assert signal is not None
    assert signal.source == "rate_limit_event"
    assert signal.reset_at == datetime.fromtimestamp(reset_epoch, tz=timezone.utc)


def test_rate_limit_event_reset_may_be_iso() -> None:
    events = [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resets_at": "2026-07-28T07:00:00+00:00"}}]

    signal = usage.detect_rate_limit(events)

    assert signal.reset_at == datetime(2026, 7, 28, 7, 0, tzinfo=timezone.utc)


def test_rate_limit_from_an_erroring_result_string() -> None:
    events = [{"type": "result", "subtype": "error", "is_error": True, "result": "Claude AI usage limit reached|1785200000"}]

    signal = usage.detect_rate_limit(events)

    assert signal is not None
    assert signal.source == "result"
    assert signal.reset_at == datetime.fromtimestamp(1785200000, tz=timezone.utc)


def test_ordinary_error_result_is_not_a_rate_limit() -> None:
    events = [{"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "ran out of turns"}]
    assert usage.detect_rate_limit(events) is None


def test_successful_result_mentioning_rate_limits_is_not_a_limit() -> None:
    # A job that *wrote about* rate limiting has not hit one.
    events = [{"type": "result", "subtype": "success", "is_error": False, "result": "I documented the rate limit handling"}]
    assert usage.detect_rate_limit(events) is None


def test_signal_carrying_a_reset_wins_over_one_without() -> None:
    events = [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
        {"type": "result", "subtype": "error", "is_error": True, "result": "usage limit reached|1785200000"},
    ]

    signal = usage.detect_rate_limit(events)

    assert signal.reset_at == datetime.fromtimestamp(1785200000, tz=timezone.utc)


def test_malformed_events_are_skipped() -> None:
    assert usage.detect_rate_limit(["not a dict", None, {"type": "rate_limit_event"}]) is None
