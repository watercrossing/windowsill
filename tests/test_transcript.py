"""Tests for windowsill.transcript -- rendering a run's stream.jsonl to Markdown.

Fixture-first (design/00-instructions.md §0.3): the renderer is a parser of
the ``--output-format stream-json --verbose`` event stream, so it is pinned
to a fixture (``tests/fixtures/stream/basic.jsonl``) whose event shapes were
taken from a real captured run -- assistant text/thinking/tool_use blocks, a
``user`` tool_result turn, an informational ``rate_limit_event``, and the
final ``result`` event. Pure string in, string out; no clock, no I/O beyond
reading the fixture path.
"""

from __future__ import annotations

import json
from pathlib import Path

from windowsill import transcript

FIXTURES = Path(__file__).parent / "fixtures" / "stream"


def test_renders_assistant_text() -> None:
    md = transcript.render_stream(FIXTURES / "basic.jsonl")
    assert "## 🤖 Assistant" in md
    assert "I'll start by reading the module." in md
    assert "Migration complete. Tests are green." in md


def test_renders_thinking_marked_and_quoted() -> None:
    md = transcript.render_stream(FIXTURES / "basic.jsonl")
    assert "Thinking" in md
    # thinking is quoted so a plain-markdown viewer sets it apart from prose
    assert "> Let me plan the refactor before touching code." in md


def test_renders_tool_use_with_input() -> None:
    md = transcript.render_stream(FIXTURES / "basic.jsonl")
    assert "Read" in md
    assert "src/auth.py" in md


def test_renders_tool_result() -> None:
    md = transcript.render_stream(FIXTURES / "basic.jsonl")
    assert "legacy_verify" in md


def test_result_summary_line() -> None:
    md = transcript.render_stream(FIXTURES / "basic.jsonl")
    assert "success" in md
    assert "3 turns" in md


def test_prompt_prepended_when_given() -> None:
    md = transcript.render_stream(FIXTURES / "basic.jsonl", prompt="Refactor the auth module.")
    assert "Prompt" in md
    assert "Refactor the auth module." in md
    # the prompt comes before the first assistant turn
    assert md.index("Refactor the auth module.") < md.index("## 🤖 Assistant")


def test_informational_rate_limit_event_is_hidden() -> None:
    # the fixture's rate_limit_event has status "allowed" -- it fires on every
    # run and is not a limit; it must not clutter the transcript.
    md = transcript.render_stream(FIXTURES / "basic.jsonl")
    assert "rate limit" not in md.lower()


def test_real_rate_limit_event_is_surfaced(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1784805000},
            }
        )
        + "\n"
    )
    md = transcript.render_stream(stream)
    assert "rate limit" in md.lower()
    assert "rejected" in md


def test_long_tool_result_truncated(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    big = "x" * 5000
    stream.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": big}]}}) + "\n")
    md = transcript.render_stream(stream)
    assert "truncated" in md
    assert len(md) < 4000


def test_tool_result_content_as_block_list(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": [{"type": "text", "text": "42 tests passed"}]}]}}
        )
        + "\n"
    )
    md = transcript.render_stream(stream)
    assert "42 tests passed" in md


def test_blank_and_malformed_lines_skipped(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        "\n"
        + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}})
        + "\n"
        + "not json{{{\n"
        + "\n"
    )
    md = transcript.render_stream(stream)
    assert "hello" in md


# ---------------------------------------------------------------------------
# final_summary + result_parts -- shared with the completion email (§8.4)
# ---------------------------------------------------------------------------


def test_final_summary_is_the_last_assistant_prose() -> None:
    events = list(transcript.iter_events(FIXTURES / "basic.jsonl"))
    assert transcript.final_summary(events) == "Migration complete. Tests are green."


def test_final_summary_skips_a_tool_only_final_turn() -> None:
    """A run's literal last turn is often a bare Write; the message the reader
    wants is the last one addressed to them."""
    events = list(transcript.iter_events(FIXTURES / "tools_only_tail.jsonl"))
    summary = transcript.final_summary(events)
    assert summary == "Here is what I did: renamed the module and fixed both call sites."
    assert "NOTES.md" not in summary


def test_final_summary_is_empty_with_no_assistant_message_at_all() -> None:
    events = list(transcript.iter_events(FIXTURES / "no_assistant.jsonl"))
    assert transcript.final_summary(events) == ""


def test_final_summary_excludes_thinking() -> None:
    events = list(transcript.iter_events(FIXTURES / "markdown_summary.jsonl"))
    summary = transcript.final_summary(events)
    assert "Not addressed to the reader" not in summary
    assert summary.startswith("Done. Summary:")


def test_final_summary_keeps_a_very_long_message_whole(tmp_path: Path) -> None:
    """The transcript truncates tool output; the closing summary is the point
    of the email and is never trimmed."""
    stream = tmp_path / "stream.jsonl"
    long_text = "This is the summary. " * 1000
    stream.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": long_text}]}}) + "\n")

    summary = transcript.final_summary(transcript.iter_events(stream))

    assert summary == long_text.strip()
    assert "truncated" not in summary


def test_final_summary_joins_multiple_text_blocks_of_one_turn(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    content = [{"type": "text", "text": "first half"}, {"type": "tool_use", "name": "Read", "input": {}}, {"type": "text", "text": "second half"}]
    stream.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}}) + "\n")

    assert transcript.final_summary(transcript.iter_events(stream)) == "first half\nsecond half"


def test_result_parts_are_the_footers_fields_in_print_order() -> None:
    events = list(transcript.iter_events(FIXTURES / "basic.jsonl"))
    result = next(event for event in events if event["type"] == "result")

    assert transcript.result_parts(result) == ["success", "3 turns", "8.4s", "$0.0800"]


def test_result_parts_omit_what_the_envelope_does_not_carry() -> None:
    assert transcript.result_parts({"subtype": "error_during_execution"}) == ["error_during_execution"]
    assert transcript.result_parts({}) == ["?"]


def test_transcript_footer_and_result_parts_agree() -> None:
    """One description of "how the run went", rendered twice."""
    md = transcript.render_stream(FIXTURES / "basic.jsonl")
    for part in transcript.result_parts({"subtype": "success", "num_turns": 3, "duration_ms": 8411, "total_cost_usd": 0.08}):
        assert part in md


def test_write_transcript_writes_file_and_returns_path(tmp_path: Path) -> None:
    (tmp_path / "stream.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}}) + "\n"
    )
    path = transcript.write_transcript(tmp_path, prompt="do it")
    assert path == tmp_path / "transcript.md"
    assert path.exists()
    text = path.read_text()
    assert "done" in text
    assert "do it" in text


def test_write_transcript_missing_stream_returns_none(tmp_path: Path) -> None:
    assert transcript.write_transcript(tmp_path) is None
    assert not (tmp_path / "transcript.md").exists()


def test_write_transcript_extra_path_gets_the_same_rendering(tmp_path: Path) -> None:
    """§2.3 --log: an addition, never a move -- the job dir keeps its copy."""
    (tmp_path / "stream.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}}) + "\n"
    )
    extra = tmp_path / "elsewhere" / "nested" / "run.md"

    path = transcript.write_transcript(tmp_path, prompt="do it", extra_path=extra)

    assert path == tmp_path / "transcript.md"
    assert extra.read_text() == path.read_text()


def test_write_transcript_unwritable_extra_path_is_swallowed(tmp_path: Path) -> None:
    (tmp_path / "stream.jsonl").write_text("")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory would have to be")

    path = transcript.write_transcript(tmp_path, prompt="the ask", extra_path=blocker / "run.md")

    assert path is not None
    assert "the ask" in path.read_text()


def test_empty_stream_still_writes_a_transcript(tmp_path: Path) -> None:
    (tmp_path / "stream.jsonl").write_text("")
    path = transcript.write_transcript(tmp_path, prompt="the ask")
    assert path is not None
    assert "the ask" in path.read_text()
