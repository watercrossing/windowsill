"""Tests for windowsill.mail -- the HTML completion email body (design/08-interface.md §8.4).

Two things this module cannot be allowed to do: emit markup an untrusted
summary supplied (the run's own prose is Claude's, but the point of escaping
first is that it doesn't matter whose it is), and raise. §8.4's rule for the
whole notification path is "a failed notification is logged and never fails
the job" -- for mail that means every render here takes degenerate input
(empty summary, no result event, no usage) and returns *something*, not an
exception.

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
    assert lines[-1] == "To pick the conversation up by hand: ccq resume 9"


def test_next_action_no_conversation_points_at_ccq_show() -> None:
    facts = mail.RunFacts(job_id=9, name="n", job_state="failed", outcome="error", has_conversation=False)
    lines = mail.next_action(facts)
    assert lines[-1] == "No conversation to resume (ccq show 9 for the transcript)."


def test_next_action_rate_limited_requeue_reads_as_automatic() -> None:
    """§5.4: job back at `ready` with the queue blocked is the rate-limit
    requeue -- the reader should be told it resolves itself."""
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
    assert lines[0] == "held — needs `ccq release` before it runs again."
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
    assert mail.subject(facts) == "[ccq 42] refactor-auth: done (success, 3 turns, 8.4s, $0.0800)"


def test_subject_appends_denied_count_when_present() -> None:
    facts = mail.RunFacts(job_id=42, name="n", job_state="done", outcome="success", result=SUCCESS_RESULT, permission_denials=2)
    assert mail.subject(facts).endswith("2 denied)")


def test_subject_falls_back_to_outcome_with_no_result_event() -> None:
    facts = mail.RunFacts(job_id=7, name="n", job_state="failed", outcome="timeout", result=None)
    assert mail.subject(facts) == "[ccq 7] n: failed (timeout)"


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
    assert "Next" in html and "ccq resume 42" in html
    assert "Window usage" in html and "42% used" in html


def test_render_html_is_self_contained_no_external_refs() -> None:
    """§8.4: inline styles only, no external CSS/JS/images -- a mail client
    strips or refuses all three."""
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
    """§8.4's rule for the whole notification path: it must never fail a run.
    A run with no summary, no result event and no usage must still produce a
    complete, serialisable message."""
    facts = mail.RunFacts(job_id=1, name="n", job_state="failed", outcome="error", summary="", result=None, usage=[])
    message = mail.build_message(facts)
    rendered = message.as_string()
    assert "Subject:" in rendered
    parts = {part.get_content_type(): part for part in message.walk() if not part.is_multipart()}
    assert parts["text/plain"].get_content().strip()
    assert parts["text/html"].get_content().strip()
