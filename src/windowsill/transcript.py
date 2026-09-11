"""Render a run's ``stream.jsonl`` into a human-readable ``transcript.md``.

Claude Code has no native pretty-printed export: a conversation is stored
on disk as newline-delimited JSON (``~/.claude/projects/<slug>/<id>.jsonl``),
and ``claude -p --output-format stream-json --verbose`` -- what a windowsill run
captures into ``stream.jsonl`` -- is that same event stream, one JSON object
per line. So "the format Claude Code uses" *is* JSONL; the readable form is
whatever you render it into. Markdown was chosen for that
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

A ``Task`` dispatch nests a whole subagent conversation into this same flat
stream, tagging its events with a top-level ``parent_tool_use_id``. Everything
that speaks *for the run* -- :func:`final_summary` above all -- has to filter
those out (:func:`is_subagent`), and the Markdown marks them.

Two pieces of that reading are shared rather than re-derived: :func:`final_summary`
(the last *orchestrator* turn that said something -- the body of the
completion email, see :mod:`windowsill.mail`) and :func:`result_parts`
(the footer fields of the ``result`` event). Both
consume the same events the Markdown renderer does, so this module stays the only
parser of the stream.

Pure and I/O-light: :func:`render_events` is string-in/string-out;
:func:`render_stream` only reads a path; :func:`write_transcript` reads
``stream.jsonl`` and writes ``transcript.md`` beside it. Malformed or blank
lines are skipped, so a killed run's partial stream still renders.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

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
                lines += ["> 💭 **Thinking**", ">", _blockquote(_truncate(thinking)), ""]
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


def result_parts(event: Mapping[str, Any]) -> list[str]:
    """The footer fields of a ``result`` event, in print order: the subtype,
    then turns, duration and cost where the envelope carries them.

    Shared with the email body (see :mod:`windowsill.mail`), which reports
    the same run in the same terms -- one description of "how the run
    went", rendered twice.

    ``total_cost_usd`` is shown when present but is not a cost metric
    here: on a subscription it is a shadow price, and the window-%
    delta is the unit that binds (design/decisions/window-percent-not-dollars.md).
    """
    parts = [str(event.get("subtype", "?"))]
    if event.get("num_turns") is not None:
        parts.append(f"{event['num_turns']} turns")
    duration_ms = event.get("duration_ms")
    if duration_ms is not None:
        parts.append(f"{duration_ms / 1000:.1f}s")
    cost = event.get("total_cost_usd")
    if cost:
        parts.append(f"${cost:.4f}")
    return parts


def _render_result(event: dict[str, Any]) -> list[str]:
    icon = "❌" if event.get("is_error") else "✅"
    subtype, *rest = result_parts(event)
    return ["---", "", " · ".join([f"**{icon} {subtype}**", *rest]), ""]


def is_subagent(event: Mapping[str, Any]) -> bool:
    """Whether a message event belongs to a nested subagent rather than to the
    orchestrator.

    A ``Task`` dispatch runs its subagent inside the same stream: the
    subagent's own ``assistant``/``user`` events are emitted alongside the
    orchestrator's, tagged at the *top level* of the envelope with
    ``parent_tool_use_id`` -- the ``tool_use_id`` of the ``Task`` call that
    dispatched them. The orchestrator's own turns carry the same key with a
    ``null`` value, so "absent or null" is the test, not "absent".

    Observed on real runs: of the 251 ``assistant`` events in job 16's stream,
    205 were subagent turns.
    """
    return event.get("parent_tool_use_id") is not None


#: A ``task_notification`` status that means the background task actually
#: finished its work. **Only this one.** Real streams also carry ``"stopped"``
#: -- job 16's last subagent was notified ``stopped`` one line before the final
#: result event, because the orchestrator ended its turn and the CLI tore the
#: agent down. "Notified" is therefore not the test; "notified *completed*" is.
_TASK_COMPLETED = "completed"


def live_background_tasks(events: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """The background tasks still running when the stream ended (see
    design/decisions/blocked-not-failed.md's ``blocked``).

    An orchestrator can end its turn while work it dispatched is still in
    flight -- a ``Task`` subagent, or a backgrounded ``Bash``. Nothing in the
    ``result`` envelope says so: job 16's first result event reads ``subtype:
    "success"``, ``is_error: false``, ``stop_reason: "end_turn"`` -- envelope-
    identical to a clean finish -- while its own ``result`` text reads "Two
    agents are running... In flight:". The discriminator lives in the
    ``system`` events instead, and this is the only thing that reads them.

    The ledger, from the two signals real streams carry:

    - ``task_started`` announces a task by ``task_id``; ``background_tasks_changed``
      carries the *current whole set* under ``tasks``. Both add to the live set.
    - ``task_notification`` with ``status == "completed"`` removes it. Any other
      status does not -- see :data:`_TASK_COMPLETED`.

    Whatever is left when the stream ends was live when the main thread stopped.
    Checked against every real capture in ``~/.windowsill/jobs``: jobs 1, 2, 4,
    13, 14, 15 end with an empty set (every task completed; job 14's trailing
    ``background_tasks_changed`` independently reports ``tasks: []``); job 16
    ends with one (``a5d6a37b1f1803ef2``, the subagent notified ``stopped``,
    which its trailing ``background_tasks_changed`` also still lists); job 18
    ends with two, never notified at all. The two signals agree on all seven.

    Backgrounded ``Bash`` (``task_type: "local_bash"``) counts the same as a
    subagent: the orchestrator dispatched work and ended without reading the
    result either way. In the real captures all 47 of them completed, so this
    costs nothing in practice -- ``task_type`` is reported so the mail can say
    which kind was left hanging.

    **Not** a signal for "the agent stopped to ask a question." There is none:
    ``stop_reason`` took only ``end_turn``/``tool_use`` across every real
    sample, and guessing from trailing punctuation would misfire on every run
    that ends on a rhetorical question. ``blocked`` is deliberately built so a
    real signal can set it later; today only this function does.
    """
    live: dict[str, dict[str, str]] = {}

    def remember(task: Mapping[str, Any]) -> None:
        task_id = task.get("task_id")
        if not task_id or task_id in live:
            return
        live[str(task_id)] = {
            "task_id": str(task_id),
            "description": str(task.get("description") or ""),
            "task_type": str(task.get("task_type") or ""),
        }

    for event in events:
        if not isinstance(event, dict) or event.get("type") != "system":
            continue
        subtype = event.get("subtype")
        if subtype == "task_started":
            remember(event)
        elif subtype == "background_tasks_changed":
            for task in event.get("tasks") or []:
                if isinstance(task, dict):
                    remember(task)
        elif subtype == "task_notification" and event.get("status") == _TASK_COMPLETED:
            live.pop(str(event.get("task_id")), None)

    return list(live.values())


def final_summary(events: Iterable[dict[str, Any]]) -> str:
    """The run's closing prose: the text of the last *orchestrator* turn that
    said anything (the email body, see :mod:`windowsill.mail`).

    Turns carrying only ``tool_use`` blocks are skipped -- a run's literal last
    assistant message is often a lone ``Write``, and the message a reader wants
    is the one addressed to *them*. ``thinking`` is excluded for the same
    reason. Returns ``""`` when the stream has no assistant prose at all (a run
    that died before its first turn, or one that only ever called tools).

    Subagent turns are excluded (:func:`is_subagent`). A subagent narrates to
    *its own* dispatcher, not to the reader of the email, and it finishes after
    the orchestrator's closing message in every stream where the last `Task` is
    still winding down -- so without the filter the mail body is whatever some
    nested agent happened to say last. Real case: job 16's completion email
    said "Now the mail tests." -- a subagent's aside -- in place of the
    orchestrator's actual closing report.

    Reads the same events :func:`render_events` does, so there is exactly one
    parser of the stream in this codebase.
    """
    summary = ""
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        if is_subagent(event):
            continue
        blocks = (event.get("message") or {}).get("content") or []
        text = "\n".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text").strip()
        if text:
            summary = text
    return summary


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
                # A subagent's turns are interleaved with the orchestrator's in
                # one flat stream, so an unmarked heading attributes a nested
                # agent's prose to the run itself -- on a Task-heavy run that is
                # most of the transcript (205 of job 16's 251 assistant turns).
                heading = "## 🤖 Assistant — subagent" if is_subagent(event) else "## 🤖 Assistant"
                lines += [heading, "", *rendered]
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
    yields everything up to the truncation. Shared with :mod:`windowsill.runner`,
    which reads the same stream looking for rate-limit signals
    (:func:`windowsill.runner._requeue_after_limit`).
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
    """Render ``<dir_>/stream.jsonl`` to ``<dir_>/transcript.md`` (see
    :func:`windowsill.runner.job_dir`).

    Returns the written path, or ``None`` if there is no ``stream.jsonl`` to
    render (nothing ran). An empty-but-present stream still writes a
    transcript -- a killed run leaves a file, and the prompt alone is worth
    recording.

    ``extra_path`` is the job's resolved ``--log`` destination (see README
    "Scheduler directives"): the same rendering is written there as well,
    missing parent directories created. It is an *addition*, never a
    move -- the job dir stays the canonical location everything else
    addresses (see :func:`windowsill.runner.job_dir`). A failure to write
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
