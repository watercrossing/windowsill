"""Render a run's ``stream.jsonl`` into a human-readable ``transcript.md``.

Claude Code has no native pretty-printed export: a conversation is stored
on disk as newline-delimited JSON (``~/.claude/projects/<slug>/<id>.jsonl``),
and ``claude -p --output-format stream-json --verbose`` -- what a ccq run
captures into ``stream.jsonl`` -- is that same event stream, one JSON object
per line. So "the format Claude Code uses" *is* JSONL; the readable form is
whatever you render it into. design/06-state.md §6.2 chose Markdown
(``transcript.md`` in the job dir), so this module turns the stream into
Markdown.

The event envelope carries a ``type`` (``system`` / ``assistant`` / ``user``
/ ``rate_limit_event`` / ``result``) and, for the message events, a
``message.content`` list of blocks (``text`` / ``thinking`` / ``tool_use`` /
``tool_result``) -- the ordinary API message shape. Only the message events
and the final ``result`` render; ``system`` init/thinking-token events are
noise. A ``rate_limit_event`` renders **only** when it reports an actual
limit: the informational one (``status: "allowed"``) fires on every run and
is suppressed.

Pure and I/O-light: :func:`render_events` is string-in/string-out;
:func:`render_stream` only reads a path; :func:`write_transcript` reads
``stream.jsonl`` and writes ``transcript.md`` beside it. Malformed or blank
lines are skipped, so a killed run's partial stream still renders.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

#: Tool inputs and results are truncated to keep a transcript readable --
#: same limit the standalone transcripts/render.py uses.
TRUNCATE = 2000


def _truncate(text: str) -> str:
    if len(text) > TRUNCATE:
        return text[:TRUNCATE] + "\n... [truncated]"
    return text


def _text_of(content: Any) -> str:
    """Flatten a ``content`` value (a bare string, or a list of text blocks)
    into plain text -- used for ``tool_result`` bodies, which come both ways.
    """
    if isinstance(content, str):
        return content
    out: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text", ""))
    return "\n".join(out)


def _blockquote(text: str) -> str:
    """Prefix every line with ``> `` so a plain-Markdown viewer sets a
    thinking block apart from ordinary prose."""
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _render_assistant(message: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text", "").rstrip()
            if text:
                lines += [text, ""]
        elif kind == "thinking":
            thinking = block.get("thinking", "").rstrip()
            if thinking:
                lines += ["> 💭 **Thinking**", ">", _blockquote(thinking), ""]
        elif kind == "tool_use":
            name = block.get("name", "?")
            inp = json.dumps(block.get("input", {}), indent=2)
            lines += [f"**🔧 `{name}`**", "", "```json", _truncate(inp), "```", ""]
    return lines


def _render_tool_results(message: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = _truncate(_text_of(block.get("content")).rstrip())
        if body:
            lines += ["↳ *tool result*", "", "```", body, "```", ""]
    return lines


def _render_rate_limit(event: dict[str, Any]) -> list[str]:
    info = event.get("rate_limit_info") or {}
    status = info.get("status")
    # "allowed" is the informational heartbeat emitted on every run; only a
    # non-allowed status is an actual limit worth surfacing.
    if status in (None, "allowed"):
        return []
    kind = info.get("rateLimitType", "?")
    return [f"> ⛔ **rate limit**: {status} ({kind})", ""]


def _render_result(event: dict[str, Any]) -> list[str]:
    icon = "❌" if event.get("is_error") else "✅"
    subtype = event.get("subtype", "?")
    parts = [f"**{icon} {subtype}**"]
    if event.get("num_turns") is not None:
        parts.append(f"{event['num_turns']} turns")
    duration_ms = event.get("duration_ms")
    if duration_ms is not None:
        parts.append(f"{duration_ms / 1000:.1f}s")
    cost = event.get("total_cost_usd")
    if cost:
        parts.append(f"${cost:.4f}")
    return ["---", "", " · ".join(parts), ""]


def render_events(events: Iterable[dict[str, Any]], *, prompt: str | None = None) -> str:
    """Render an iterable of stream-json events into a Markdown transcript.

    ``prompt`` (the job body) is prepended as the opening turn when given --
    the stream itself does not echo the ``-p`` stdin prompt, so without this
    the transcript would start mid-conversation.
    """
    lines: list[str] = ["# Transcript", ""]
    if prompt and prompt.strip():
        lines += ["## 👤 Prompt", "", prompt.rstrip(), ""]

    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "assistant":
            rendered = _render_assistant(event.get("message") or {})
            if rendered:
                lines += ["## 🤖 Assistant", "", *rendered]
        elif kind == "user":
            lines += _render_tool_results(event.get("message") or {})
        elif kind == "rate_limit_event":
            lines += _render_rate_limit(event)
        elif kind == "result":
            lines += _render_result(event)

    return "\n".join(lines).rstrip() + "\n"


def iter_events(stream_path: Path) -> Iterable[dict[str, Any]]:
    """Yield the well-formed JSON objects in a ``stream.jsonl``.

    Blank and malformed lines are skipped, so a run killed mid-write still
    yields everything up to the truncation. Shared with :mod:`ccq.runner`,
    which reads the same stream looking for rate-limit signals (§5.4).
    """
    with stream_path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def render_stream(stream_path: Path, *, prompt: str | None = None) -> str:
    """Read a ``stream.jsonl`` file and render it. A missing file renders as
    an empty (prompt-only) transcript rather than raising, so a run that died
    before writing any stream still yields something readable."""
    events = list(iter_events(stream_path)) if stream_path.exists() else []
    return render_events(events, prompt=prompt)


def write_transcript(dir_: Path, *, prompt: str | None = None, extra_path: Path | None = None) -> Path | None:
    """Render ``<dir_>/stream.jsonl`` to ``<dir_>/transcript.md`` (§6.2).

    Returns the written path, or ``None`` if there is no ``stream.jsonl`` to
    render (nothing ran). An empty-but-present stream still writes a
    transcript -- a killed run leaves a file, and the prompt alone is worth
    recording.

    ``extra_path`` is the job's resolved ``--log`` destination (§2.3): the
    same rendering is written there as well, missing parent directories
    created. It is an *addition*, never a move -- the job dir stays the
    canonical location everything else addresses (§6.2). A failure to write
    it (unwritable directory, a path that is really a directory) is
    swallowed: the run has already happened, and losing the copy must not
    lose the transcript.
    """
    stream_path = dir_ / "stream.jsonl"
    if not stream_path.exists():
        return None
    text = render_stream(stream_path, prompt=prompt)
    out = dir_ / "transcript.md"
    out.write_text(text)
    if extra_path is not None:
        try:
            extra_path.parent.mkdir(parents=True, exist_ok=True)
            extra_path.write_text(text)
        except OSError:
            pass
    return out
