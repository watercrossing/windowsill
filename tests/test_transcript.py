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


def test_renders_thinking_interleaved_with_prose_and_tool_use() -> None:
    # a single turn carrying thinking + text + tool_use blocks together --
    # all three must render, and the thinking stays visually distinct (in
    # its own blockquote) from the ordinary prose and the tool call.
    md = transcript.render_stream(FIXTURES / "thinking_and_prose.jsonl")
    assert "> 💭 **Thinking**" in md
    assert "> Let me check the auth module before I touch it." in md
    assert "I'll start by reading the module, then patch it." in md
    assert "Read" in md
    assert "src/auth.py" in md


def test_renders_a_thinking_only_turn() -> None:
    # the fixture's second assistant turn carries only a thinking block --
    # it must still render even though it has no prose or tool call.
    md = transcript.render_stream(FIXTURES / "thinking_and_prose.jsonl")
    assert "> That's the whole fix -- nothing else worth saying." in md


def test_long_thinking_truncated(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    big = "x" * 5000
    stream.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": big}]}}) + "\n")
    md = transcript.render_stream(stream)
    assert "truncated" in md
    assert len(md) < 4000


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


def test_final_summary_ignores_a_trailing_thinking_only_turn() -> None:
    """final_summary feeds the completion email body (§8.4); a thinking block
    must never leak into it, even when it is the stream's literal last
    assistant turn (the fixture's second turn is thinking-only)."""
    events = list(transcript.iter_events(FIXTURES / "thinking_and_prose.jsonl"))
    summary = transcript.final_summary(events)
    assert summary == "I'll start by reading the module, then patch it."
    assert "nothing else worth saying" not in summary


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


def test_final_summary_ignores_subagent_turns() -> None:
    """A Task dispatch runs its subagent in the same stream, tagged with
    ``parent_tool_use_id``; its narration is addressed to the orchestrator, not
    to the reader of the email. The fixture's literal last assistant turn is a
    subagent's, so an unfiltered scan returns that instead of the run's own
    closing report -- which is what job 16's completion email actually said.
    """
    events = list(transcript.iter_events(FIXTURES / "subagent_turns.jsonl"))
    summary = transcript.final_summary(events)
    assert summary == "The agent is running. Meanwhile I fixed the licence metadata and pushed 27baa1e."
    assert "Now the mail tests." not in summary


def test_is_subagent_treats_a_null_parent_tool_use_id_as_the_orchestrator() -> None:
    """The orchestrator's own events carry the key with a ``null`` value, not
    no key at all -- so "absent or null" is the test."""
    assert not transcript.is_subagent({"type": "assistant", "parent_tool_use_id": None})
    assert not transcript.is_subagent({"type": "assistant"})
    assert transcript.is_subagent({"type": "assistant", "parent_tool_use_id": "toolu_TASK1"})


def test_subagent_turns_are_marked_in_the_transcript() -> None:
    out = transcript.render_stream(FIXTURES / "subagent_turns.jsonl")
    assert "## 🤖 Assistant — subagent" in out
    # the orchestrator's own turns keep the plain heading
    assert "## 🤖 Assistant\n" in out


# ---------------------------------------------------------------------------
# live_background_tasks -- the §7 "blocked" discriminator
# ---------------------------------------------------------------------------


def test_live_background_tasks_finds_what_never_completed() -> None:
    """The fixture's envelope is a clean success -- subtype "success",
    is_error false, stop_reason "end_turn" -- exactly like job 16's. Only the
    system events say a subagent was still going."""
    events = list(transcript.iter_events(FIXTURES / "live_background_tasks.jsonl"))
    live = transcript.live_background_tasks(events)

    assert [task["task_id"] for task in live] == ["aAAA"]
    assert live[0]["description"] == "Fix notify defaults and email body"
    assert live[0]["task_type"] == "local_agent"


def test_a_stopped_notification_is_not_a_completion() -> None:
    """Job 16's last subagent was notified ``stopped`` one line before the
    final result -- the orchestrator quit out from under it. Treating any
    notification as terminal would call that run clean."""
    events = [
        {"type": "system", "subtype": "task_started", "task_id": "aAAA", "description": "d", "task_type": "local_agent"},
        {"type": "system", "subtype": "task_notification", "task_id": "aAAA", "status": "stopped"},
    ]
    assert [task["task_id"] for task in transcript.live_background_tasks(events)] == ["aAAA"]


def test_live_background_tasks_empty_when_everything_completed() -> None:
    events = list(transcript.iter_events(FIXTURES / "basic.jsonl"))
    assert transcript.live_background_tasks(events) == []


def test_background_tasks_changed_alone_announces_a_live_task() -> None:
    """The two signals are redundant on real streams but not identical: some
    captures carry ``background_tasks_changed`` and some don't, so either one
    announcing a task is enough."""
    events = [
        {"type": "system", "subtype": "background_tasks_changed", "tasks": [{"task_id": "aAAA", "description": "d", "task_type": "local_agent"}]},
    ]
    assert [task["task_id"] for task in transcript.live_background_tasks(events)] == ["aAAA"]


def test_a_completed_task_is_cleared_even_if_it_was_announced_twice() -> None:
    events = [
        {"type": "system", "subtype": "background_tasks_changed", "tasks": [{"task_id": "aAAA", "description": "d", "task_type": "local_agent"}]},
        {"type": "system", "subtype": "task_started", "task_id": "aAAA", "description": "d", "task_type": "local_agent"},
        {"type": "system", "subtype": "task_notification", "task_id": "aAAA", "status": "completed"},
    ]
    assert transcript.live_background_tasks(events) == []


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
