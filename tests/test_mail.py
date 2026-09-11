"""Tests for windowsill.mail -- the HTML completion email body.

Two things this module cannot be allowed to do: emit markup an untrusted
summary supplied (the run's own prose is Claude's, but the point of escaping
first is that it doesn't matter whose it is), and raise. windowsill.notify's
rule for the whole notification path is "a failed notification is logged
and never fails the job" -- for mail that means every render here takes
degenerate input (empty summary, no result event, no usage) and returns
*something*, not an exception.

``markdown_to_html`` is tested against hand-written snippets for each construct
and once against the real ``markdown_summary.jsonl`` fixture, the same one
``transcript.final_summary`` is pinned to in ``test_transcript.py`` -- so the
round trip here exercises the same text a completion email would actually
carry, headings/bold/code/table/ordered-list/fence/blockquote all in one turn.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from windowsill import mail, transcript

FIXTURES = Path(__file__).parent / "fixtures" / "stream"

# ---------------------------------------------------------------------------
# markdown_to_html -- construct by construct
# ---------------------------------------------------------------------------


def test_heading_levels_and_cap_at_h3() -> None:
    html = mail.markdown_to_html("# One\n\n## Two\n\n#### Four becomes h3")
    assert "<h1" in html and ">One</h1>" in html
    assert "<h2" in html and ">Two</h2>" in html
    assert "<h3" in html and ">Four becomes h3</h3>" in html
    assert "<h4" not in html


def test_bold_italic_and_inline_code() -> None:
    html = mail.markdown_to_html("Fixed **both** sites, *quickly*, via `git commit`.")
    assert "<strong>both</strong>" in html
    assert "<em>quickly</em>" in html
    assert "<code" in html and ">git commit</code>" in html


def test_link_renders_as_anchor_with_href() -> None:
    html = mail.markdown_to_html("See [the docs](https://example.com/docs).")
    assert '<a href="https://example.com/docs"' in html
    assert ">the docs</a>" in html


def test_fenced_code_block() -> None:
    html = mail.markdown_to_html("```\nprint(1)\nline two\n```")
    assert "<pre" in html
    assert "<code>print(1)\nline two</code>" in html


def test_fenced_code_block_content_is_escaped_not_interpreted() -> None:
    # Markdown inside a fence must stay literal -- it's code, not prose.
    html = mail.markdown_to_html("```\n**not bold** <b>not html</b>\n```")
    assert "<strong>" not in html
    assert "&lt;b&gt;" in html
    assert "**not bold**" in html


def test_bullet_list() -> None:
    html = mail.markdown_to_html("- first\n- second")
    assert "<ul" in html
    assert html.count("<li") == 2
    assert "first" in html and "second" in html
    assert "</ul>" in html


def test_numbered_list_renders_as_ordered_list() -> None:
    html = mail.markdown_to_html("1. first\n2. second")
    assert "<ol" in html and "</ol>" in html
    assert html.count("<li") == 2


def test_blockquote() -> None:
    html = mail.markdown_to_html("> a quoted line")
    assert "<blockquote" in html
    assert "a quoted line</blockquote>" in html


def test_table_with_header_and_rows() -> None:
    html = mail.markdown_to_html("| job | state |\n|---|---|\n| one | done |\n| two | failed |")
    assert "<table" in html
    assert "<th" in html and ">job</th>" in html and ">state</th>" in html
    assert html.count("<tr>") == 3  # one header row (in <thead>) plus two body rows
    assert ">one</td>" in html and ">failed</td>" in html


def test_pipe_line_without_divider_is_not_treated_as_a_table() -> None:
    # `_is_table_row` alone is not enough -- the divider is what promotes a
    # plain paragraph containing pipes into an actual table.
    html = mail.markdown_to_html("| just a sentence with | a pipe in it |")
    assert "<table" not in html


def test_html_in_summary_is_escaped_not_executed() -> None:
    html = mail.markdown_to_html("<script>alert(document.cookie)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_ampersand_in_summary_is_escaped() -> None:
    html = mail.markdown_to_html("cats & dogs")
    assert "&amp;" in html
    assert " & " not in html


def test_script_tag_survives_inside_inline_code_only_as_escaped_text() -> None:
    # Even wrapped in a code span, raw markup must never reach the output --
    # only the escaped, inert text does.
    html = mail.markdown_to_html("`<img src=x onerror=alert(1)>`")
    assert "<img" not in html
    assert "&lt;img" in html


def test_markdown_summary_fixture_round_trips_every_construct() -> None:
    """The fixture (tests/fixtures/stream/markdown_summary.jsonl) is the same
    one test_transcript.py pins final_summary to -- a real closing message
    with a heading, bold, inline code, a table, an ordered list, a fence and
    a blockquote, all in one turn."""
    events = list(transcript.iter_events(FIXTURES / "markdown_summary.jsonl"))
    summary = transcript.final_summary(events)
    assert summary  # sanity: the fixture is shaped the way this test assumes

    html = mail.markdown_to_html(summary)

    assert "<h2" in html and ">What changed</h2>" in html
    assert "<strong>both</strong>" in html
    assert ">src/windowsill/cli.py</code>" in html
    assert "<table" in html and ">job</th>" in html and ">haiku-hello</code>" in html
    assert "<ol" in html and "still named <code" in html
    assert "<pre" in html and "denied-writer2" in html
    assert "<blockquote" in html and "Nothing else is outstanding.</blockquote>" in html


# ---------------------------------------------------------------------------
# outcome_line / next_action / usage_rows / subject -- table-driven over RunFacts
# ---------------------------------------------------------------------------

SUCCESS_RESULT = {"subtype": "success", "num_turns": 3, "duration_ms": 8411, "total_cost_usd": 0.08}


def test_outcome_line_success_includes_delta_and_result_parts() -> None:
    facts = mail.RunFacts(job_id=1, name="n", job_state="done", outcome="success", result=SUCCESS_RESULT, delta_pct=12)
    assert mail.outcome_line(facts) == "success · 3 turns · 8.4s · $0.0800 · Δ 12% of the window"


def test_outcome_line_appends_permission_denials_pluralised() -> None:
    facts = mail.RunFacts(job_id=1, name="n", job_state="failed", outcome="error", result={"subtype": "error_during_execution"}, permission_denials=2)
    assert mail.outcome_line(facts) == "error_during_execution · 2 permission denials"


def test_outcome_line_singular_for_one_denial() -> None:
    facts = mail.RunFacts(job_id=1, name="n", job_state="done", outcome="success", result=SUCCESS_RESULT, permission_denials=1)
    line = mail.outcome_line(facts)
    assert line.endswith("1 permission denial")
    assert "1 permission denials" not in line


def test_outcome_line_omits_denials_when_zero() -> None:
    facts = mail.RunFacts(job_id=1, name="n", job_state="done", outcome="success", result=SUCCESS_RESULT, permission_denials=0)
    assert "denial" not in mail.outcome_line(facts)


def test_outcome_line_falls_back_to_outcome_with_no_result_event() -> None:
    # e.g. a timeout that never produced a result envelope.
    facts = mail.RunFacts(job_id=1, name="n", job_state="failed", outcome="timeout", result=None)
    assert mail.outcome_line(facts) == "timeout"


def test_next_action_done_state_says_finished() -> None:
    facts = mail.RunFacts(job_id=9, name="n", job_state="done", outcome="success", has_conversation=True)
    lines = mail.next_action(facts)
    assert lines[0] == "finished — nothing further."
    assert lines[-1] == "To pick the conversation up by hand: windowsill resume 9"


def _blocked_facts(**overrides: object) -> mail.RunFacts:
    defaults = dict(
        job_id=16,
        name="windowsill-updates-1",
        job_state="blocked",
        outcome="incomplete",
        result={"subtype": "success", "num_turns": 21, "duration_ms": 202468},
        live_tasks=[{"task_id": "aAAA", "description": "Fix notify defaults and email body", "task_type": "local_agent"}],
    )
    defaults.update(overrides)
    return mail.RunFacts(**defaults)  # type: ignore[arg-type]


def test_next_action_blocked_names_the_human_and_the_command() -> None:
    """design/decisions/blocked-not-failed.md: nothing in windowsill moves a
    blocked job, so the mail says so and gives the command. It must never
    reach the generic state fallback."""
    lines = mail.next_action(_blocked_facts())

    assert lines[0].startswith("blocked — the run ended while work it had started was still going")
    assert "Still running at exit (subagent): Fix notify defaults and email body" in lines
    assert lines[-1] == "Nothing will move this job on its own — pick it up with: ccresume 16"
    assert not any(line == "state blocked." for line in lines)


def test_next_action_blocked_distinguishes_a_background_command() -> None:
    facts = _blocked_facts(live_tasks=[{"task_id": "bCCC", "description": "uv run pytest -q", "task_type": "local_bash"}])
    assert "Still running at exit (background command): uv run pytest -q" in mail.next_action(facts)


def test_outcome_line_states_the_live_tasks_next_to_the_success_subtype() -> None:
    """The envelope of a blocked run reads "success"; unqualified, the line
    says the opposite of what happened."""
    line = mail.outcome_line(_blocked_facts())
    assert "success" in line
    assert "1 background task still running" in line


def test_subject_of_a_blocked_run_does_not_end_on_the_word_success() -> None:
    subject = mail.subject(_blocked_facts())
    assert "blocked" in subject
    assert "1 still running" in subject


def test_next_action_no_conversation_points_at_windowsill_show() -> None:
    facts = mail.RunFacts(job_id=9, name="n", job_state="failed", outcome="error", has_conversation=False)
    lines = mail.next_action(facts)
    assert lines[-1] == "No conversation to resume (windowsill show 9 for the transcript)."


def test_next_action_rate_limited_requeue_reads_as_automatic() -> None:
    """runner._requeue_after_limit: job back at `ready` with the queue
    blocked is the rate-limit requeue -- the reader should be told it
    resolves itself."""
    facts = mail.RunFacts(
        job_id=3,
        name="n",
        job_state="ready",
        outcome="rate_limited",
        blocked_until="2026-08-01T02:00:00Z",
        blocked_reason="rate limit reached",
    )
    lines = mail.next_action(facts)
    assert lines[0] == f"will resume automatically in the next window at {mail._local('2026-08-01T02:00:00Z')}."
    assert lines[1] == "Reason: rate limit reached."


def test_next_action_ready_and_blocked_for_a_non_rate_limit_reason_reads_as_queued() -> None:
    facts = mail.RunFacts(
        job_id=3,
        name="n",
        job_state="ready",
        outcome="held",
        blocked_until="2026-08-01T02:00:00Z",
        blocked_reason="manually held",
    )
    lines = mail.next_action(facts)
    assert lines[0] == f"queued, blocked at {mail._local('2026-08-01T02:00:00Z')}."


def test_next_action_blocked_but_not_ready_state_omits_the_reason_line() -> None:
    """Only the `ready`+blocked branch prints "Reason:"; any other terminal
    state just notes the queue is blocked, plainly."""
    facts = mail.RunFacts(
        job_id=3,
        name="n",
        job_state="held",
        outcome="held",
        blocked_until="2026-08-01T02:00:00Z",
        blocked_reason="rate limit reached",
    )
    lines = mail.next_action(facts)
    assert lines[0] == "held — needs `windowsill release` before it runs again."
    assert lines[1] == f"The queue is blocked until {mail._local('2026-08-01T02:00:00Z')}."
    assert not any(line.startswith("Reason:") for line in lines)


def test_next_action_unknown_job_state_falls_back_to_a_generic_line() -> None:
    facts = mail.RunFacts(job_id=1, name="n", job_state="mystery-state", outcome="?")
    assert mail.next_action(facts)[0] == "state mystery-state."


def test_usage_rows_none_reading_and_no_ledger_count_is_empty() -> None:
    assert mail.usage_rows(None) == []


def test_usage_rows_closed_window_says_closed_not_a_bare_percentage() -> None:
    from windowsill import usage

    reading = usage.ParsedUsage(pct_used=0, reset_at=None)
    rows = mail.usage_rows(reading)
    assert rows == [("Session window", "closed — no window open")]


def test_usage_rows_open_window_with_weekly_and_extra_bucket() -> None:
    from datetime import datetime, timezone

    from windowsill import usage

    reading = usage.ParsedUsage(
        pct_used=42,
        reset_at=datetime(2026, 8, 1, 2, 0, tzinfo=timezone.utc),
        weekly_pct=10,
        weekly_reset_at=datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc),
        extra_bucket="Fable",
        extra_pct=5,
    )
    rows = dict(mail.usage_rows(reading, windows_in_30d=7, max_sessions=60))
    assert rows["Session window"] == "42% used"
    assert rows["Week (all models)"].startswith("10% used, resets ")
    assert rows["Week (Fable)"] == "5% used"
    assert rows["Windows in 30d"] == "7 / 60"


def test_usage_rows_windows_in_30d_without_a_ceiling_prints_the_bare_count() -> None:
    rows = dict(mail.usage_rows(None, windows_in_30d=3, max_sessions=None))
    assert rows["Windows in 30d"] == "3"


def test_subject_includes_job_id_name_state_and_result_detail() -> None:
    facts = mail.RunFacts(job_id=42, name="refactor-auth", job_state="done", outcome="success", result=SUCCESS_RESULT)
    assert mail.subject(facts) == "[windowsill 42] refactor-auth: done (success, 3 turns, 8.4s, $0.0800)"


def test_subject_appends_denied_count_when_present() -> None:
    facts = mail.RunFacts(job_id=42, name="n", job_state="done", outcome="success", result=SUCCESS_RESULT, permission_denials=2)
    assert mail.subject(facts).endswith("2 denied)")


def test_subject_falls_back_to_outcome_with_no_result_event() -> None:
    facts = mail.RunFacts(job_id=7, name="n", job_state="failed", outcome="timeout", result=None)
    assert mail.subject(facts) == "[windowsill 7] n: failed (timeout)"


# ---------------------------------------------------------------------------
# the attempt history (a rate-limited job runs more than once via
# runner._requeue_after_limit)
# ---------------------------------------------------------------------------

#: Three attempts of the shape the user actually reported: two runs that hit
#: the window ceiling overnight and a third, three-minute one that finished.
#: 21:21 to 07:03 is 9h42m of wall clock against 1h09m of running -- the gap
#: between those two numbers is the entire reason this section exists.
THREE_ATTEMPTS = [
    mail.Attempt(
        n=1,
        started_at="2026-07-20T21:21:00Z",
        finished_at="2026-07-20T21:52:00Z",
        outcome="rate_limited",
        delta_pct=68,
        num_turns=41,
        duration_ms=1_860_000,
        summary="Reworked the parser; hit the ceiling mid-test-run.",
    ),
    mail.Attempt(
        n=2,
        started_at="2026-07-21T02:00:00Z",
        finished_at="2026-07-21T02:35:00Z",
        outcome="rate_limited",
        delta_pct=100,
        num_turns=33,
        duration_ms=2_100_000,
        summary="Picked the tests back up; ceiling again.",
    ),
    mail.Attempt(
        n=3,
        started_at="2026-07-21T07:00:00Z",
        finished_at="2026-07-21T07:03:00Z",
        outcome="done",
        delta_pct=4,
        num_turns=6,
        duration_ms=180_000,
        summary="All green.",
    ),
]


def test_attempt_line_carries_number_time_duration_outcome_delta_and_turns() -> None:
    line = mail.attempt_line(THREE_ATTEMPTS[0])
    assert line == f"#1 · {mail._local('2026-07-20T21:21:00Z')} · 31m 00s · rate_limited · Δ 68% · 41 turns"


def test_attempt_line_falls_back_to_the_row_timestamps_without_a_duration() -> None:
    """A killed run leaves no result envelope and therefore no duration_ms --
    but the run row still knows when it started and stopped."""
    attempt = mail.Attempt(n=2, started_at="2026-07-21T02:00:00Z", finished_at="2026-07-21T02:35:00Z", outcome="killed")
    assert "35m 00s" in mail.attempt_line(attempt)


def test_attempt_line_survives_a_half_written_run_row() -> None:
    assert mail.attempt_line(mail.Attempt(n=1)) == "#1 · - · - · ?"


def test_totals_line_reports_wall_clock_running_time_turns_and_window_pct() -> None:
    assert mail.totals_line(THREE_ATTEMPTS) == (
        f"3 runs · 9h 42m wall ({mail._local('2026-07-20T21:21:00Z')} → {mail._local('2026-07-21T07:03:00Z')})"
        " · 1h 09m running · 80 turns · 172% of the window"
    )


def test_window_pct_spent_sums_the_per_run_deltas() -> None:
    assert mail.window_pct_spent(THREE_ATTEMPTS) == 68 + 100 + 4


def test_window_pct_spent_ignores_a_negative_delta() -> None:
    """A window that reset mid-run leaves pct_after below pct_before; the
    difference is then two different windows' readings, not a refund."""
    attempts = [mail.Attempt(n=1, delta_pct=90), mail.Attempt(n=2, delta_pct=-80)]
    assert mail.window_pct_spent(attempts) == 90


def test_window_pct_spent_ignores_attempts_with_no_reading() -> None:
    assert mail.window_pct_spent([mail.Attempt(n=1, delta_pct=12), mail.Attempt(n=2)]) == 12


def test_the_mail_never_invents_a_cost_unit() -> None:
    """windowsill records no tokens and no dollars anywhere, so the totals must
    not imply either -- and the percentage it does record is a floor."""
    rendered = mail.render_text(_facts(attempts=THREE_ATTEMPTS)) + mail.render_html(_facts(attempts=THREE_ATTEMPTS))
    assert "token" not in rendered.lower()
    assert "$" not in rendered.replace("$0.0800", "")  # the envelope's own cost field is the transcript's, not ours
    assert "floor, not a sum" in rendered


def test_subject_names_the_run_count_when_there_was_more_than_one() -> None:
    facts = _facts(attempts=THREE_ATTEMPTS)
    assert mail.subject(facts) == "[windowsill 42] refactor-auth: done (success, 3 turns, 8.4s, $0.0800, 3 runs)"


def test_subject_of_a_single_run_job_says_nothing_about_runs() -> None:
    assert "runs" not in mail.subject(_facts())


def test_render_html_lists_every_attempt_with_its_own_summary() -> None:
    html = mail.render_html(_facts(attempts=THREE_ATTEMPTS))
    assert "Attempts" in html
    # The whole complaint: the two rate-limited runs' output was lost.
    assert "Reworked the parser; hit the ceiling mid-test-run." in html
    assert "Picked the tests back up; ceiling again." in html
    assert "9h 42m wall" in html and "172% of the window" in html


def test_render_text_lists_every_attempt_with_its_own_summary() -> None:
    text = mail.render_text(_facts(attempts=THREE_ATTEMPTS))
    assert "Attempts:" in text
    assert "Reworked the parser; hit the ceiling mid-test-run." in text
    assert "Picked the tests back up; ceiling again." in text
    assert "3 runs · 9h 42m wall" in text


def test_a_single_run_mail_is_exactly_what_it_always_was() -> None:
    """The ordinary case must not change at all: no Attempts section, no run
    count, byte-for-byte the mail a one-attempt job sent before any of this."""
    one = _facts(attempts=[THREE_ATTEMPTS[0]])
    assert mail.render_html(one) == mail.render_html(_facts())
    assert mail.render_text(one) == mail.render_text(_facts())
    assert mail.subject(one) == mail.subject(_facts())
    assert "Attempts" not in mail.render_html(_facts())
    assert "Attempts" not in mail.render_text(_facts())


def test_a_long_attempt_summary_is_clipped() -> None:
    attempts = [THREE_ATTEMPTS[0], mail.Attempt(n=2, outcome="done", summary="word " * 400)]
    text = mail.render_text(_facts(attempts=attempts))
    assert "…" in text
    assert max(len(line) for line in text.splitlines()) < 400


def test_an_attempt_summary_is_escaped_in_the_html() -> None:
    attempts = [THREE_ATTEMPTS[0], mail.Attempt(n=2, outcome="done", summary="<script>steal()</script>")]
    html = mail.render_html(_facts(attempts=attempts))
    assert "<script>steal()</script>" not in html
    assert "&lt;script&gt;" in html


def test_build_message_with_attempts_survives_as_string() -> None:
    message = mail.build_message(_facts(attempts=THREE_ATTEMPTS))
    rendered = message.as_string()
    assert "3 runs" in message["Subject"]
    assert "multipart/alternative" in rendered


# ---------------------------------------------------------------------------
# the rate-limit ping ([notify].on_rate_limit)
# ---------------------------------------------------------------------------


def test_rate_limit_notice_names_the_resume_time_in_the_title() -> None:
    title, _ = mail.rate_limit_notice(job_id=20, name="windowsill-todo-3", blocked_until="2026-07-21T02:00:00Z")
    assert title == f"windowsill-todo-3: rate-limited, back at {mail._local('2026-07-21T02:00:00Z')}"


def test_rate_limit_notice_says_the_pause_cost_no_retry() -> None:
    """README "Scheduler directives": a rate limit does not consume a retry,
    and a bare "2 retries left" would let the reader assume it had."""
    _, body = mail.rate_limit_notice(job_id=20, name="n", blocked_until="2026-07-21T02:00:00Z", retries_left=2)
    assert "This pause cost no retry (2 still available for an actual failure)." in body


def test_rate_limit_notice_carries_the_usage_rows() -> None:
    rows = [("Session window", "100% used"), ("Windows in 30d", "18 / 60")]
    _, body = mail.rate_limit_notice(job_id=20, name="n", blocked_until=None, usage=rows)
    assert "Session window: 100% used" in body
    assert "Windows in 30d: 18 / 60" in body


def test_rate_limit_notice_stays_short() -> None:
    """A "still alive, back at 02:00" ping, not a report."""
    _, body = mail.rate_limit_notice(
        job_id=20,
        name="n",
        blocked_until="2026-07-21T02:00:00Z",
        retries_left=1,
        usage=[("Session window", "100% used")],
    )
    assert len(body.splitlines()) == 4


def test_build_rate_limit_message_is_plain_text_under_the_usual_subject_prefix() -> None:
    title, body = mail.rate_limit_notice(job_id=20, name="n", blocked_until="2026-07-21T02:00:00Z")
    message = mail.build_rate_limit_message(20, title, body)
    assert message["Subject"] == f"[windowsill 20] {title}"
    assert message.get_content_type() == "text/plain"  # nothing to render: the run is not over
    assert body in message.get_content()


# ---------------------------------------------------------------------------
# render_html / render_text / build_message
# ---------------------------------------------------------------------------


def _facts(**overrides: object) -> mail.RunFacts:
    base: dict[str, object] = dict(
        job_id=42,
        name="refactor-auth",
        job_state="done",
        outcome="success",
        summary="All done. **Green** across the board.",
        result=SUCCESS_RESULT,
        delta_pct=12,
        permission_denials=0,
        has_conversation=True,
        usage=[("Session window", "42% used"), ("Windows in 30d", "7 / 60")],
    )
    base.update(overrides)
    return mail.RunFacts(**base)  # type: ignore[arg-type]


def test_render_html_contains_summary_outcome_next_and_usage_blocks() -> None:
    html = mail.render_html(_facts())
    assert "<strong>Green</strong>" in html
    assert "Outcome" in html and "success" in html
    assert "Next" in html and "windowsill resume 42" in html
    assert "Window usage" in html and "42% used" in html


def test_render_html_is_self_contained_no_external_refs() -> None:
    """Self-contained HTML: inline styles only, no external CSS/JS/images --
    a mail client strips or refuses all three."""
    html = mail.render_html(_facts())
    assert "<link" not in html
    assert "<script" not in html
    assert "src=" not in html
    assert 'style="' in html  # styling is present, just inline


def test_render_html_omits_usage_block_when_there_is_none() -> None:
    html = mail.render_html(_facts(usage=[]))
    assert "Window usage" not in html


def test_render_html_uses_placeholder_for_empty_summary() -> None:
    html = mail.render_html(_facts(summary=""))
    assert "the run produced no closing message" in html


def test_render_html_escapes_summary_html() -> None:
    html = mail.render_html(_facts(summary="<script>steal()</script>"))
    assert "<script>steal()</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_text_contains_summary_outcome_next_and_usage() -> None:
    text = mail.render_text(_facts())
    assert "All done. **Green** across the board." in text  # unrendered Markdown
    assert "Outcome: success" in text
    assert "Next:\n  finished — nothing further." in text
    assert "Window usage:\n  Session window: 42% used" in text


def test_render_text_omits_usage_section_when_there_is_none() -> None:
    text = mail.render_text(_facts(usage=[]))
    assert "Window usage" not in text


def test_render_text_uses_placeholder_for_empty_summary() -> None:
    text = mail.render_text(_facts(summary=""))
    assert "(the run produced no closing message)" in text


def test_build_message_is_multipart_alternative_with_text_and_html_parts() -> None:
    message = mail.build_message(_facts())
    assert isinstance(message, EmailMessage)
    assert message.get_content_type() == "multipart/alternative"

    parts = {part.get_content_type(): part for part in message.walk() if not part.is_multipart()}
    assert "text/plain" in parts
    assert "text/html" in parts
    assert "All done." in parts["text/plain"].get_content()
    assert "<strong>Green</strong>" in parts["text/html"].get_content()


def test_build_message_subject_matches_subject_function() -> None:
    facts = _facts()
    message = mail.build_message(facts)
    assert message["Subject"] == mail.subject(facts)


def test_build_message_survives_as_string() -> None:
    message = mail.build_message(_facts())
    rendered = message.as_string()
    assert "Subject:" in rendered
    assert "multipart/alternative" in rendered


def test_build_message_survives_as_string_with_degenerate_facts() -> None:
    """windowsill.notify's rule for the whole notification path: it must
    never fail a run. A run with no summary, no result event and no usage
    must still produce a complete, serialisable message."""
    facts = mail.RunFacts(job_id=1, name="n", job_state="failed", outcome="error", summary="", result=None, usage=[])
    message = mail.build_message(facts)
    rendered = message.as_string()
    assert "Subject:" in rendered
    parts = {part.get_content_type(): part for part in message.walk() if not part.is_multipart()}
    assert parts["text/plain"].get_content().strip()
    assert parts["text/html"].get_content().strip()
