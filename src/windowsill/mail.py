"""The completion email's body (design/08-interface.md §8.4).

An overnight run's mail has one job: answer "what happened, and what does the
queue do next" without making the reader open a terminal. The first attempt
sent the *first line* of the final message plus a bare ``Δ 32%``, which
answered neither. So this module builds the body out of four things, in the
order a person reads them:

1. **The run's own final summary** -- the last assistant turn (§8.4 via
   :func:`windowsill.transcript.final_summary`), which is already written as
   Markdown, rendered to HTML.
2. **The outcome footer** -- the same fields the rendered transcript prints
   (:func:`windowsill.transcript.result_parts`), plus the Δ window % (§4.3) and
   the permission-denial count (§9) when there is one.
3. **The next action windowsill itself will take** -- read off the job's §7
   state and the ``sched`` block gate (§6), never invented, plus the
   ``windowsill resume <id>`` escape hatch that is the morning-after path (§8.1).
4. **Window usage** -- whatever the ``/usage`` oracle last read (§4), which is
   the only unit that binds (§1).

A job that hit the window ceiling gets a fifth: an **attempt history**, one
line per ``runs`` row with that attempt's own last words under it, and a totals
line whose wall-clock span is the number that makes "this took ten hours"
visible. It appears only when there was more than one attempt -- a job that ran
once sends the mail it always did. The totals report window percentage and
nothing else: windowsill records no tokens and no dollars anywhere, and the
percentage it does record does not add up across a window boundary, which is
what :data:`WINDOW_PCT_CAVEAT` says in the mail itself.

Last, :func:`rate_limit_notice` composes the one notification that is not about
a finished job: the ``[notify].on_rate_limit`` ping that says the queue is
paused and when it comes back.

Constraints that shape the implementation:

- **Self-contained HTML.** Inline styles only; no external CSS, JS, or images.
  A mail client will strip or refuse to fetch all three.
- **A plaintext alternative that is worth reading.** The summary *is* Markdown,
  so the text part is the same content unrendered -- not a degraded stub.
- **Stdlib only** (§0.6). :mod:`email.message` builds the
  ``multipart/alternative``; the Markdown subset below is rendered by hand
  rather than by adding a dependency for one email body.

The Markdown renderer is deliberately a subset -- headings, fenced code,
tables, lists, blockquotes, rules, paragraphs, and inline code/bold/italic/links.
It is what Claude writes in a summary. Anything unrecognised survives as
escaped text, which is the failure mode you want in an email: unstyled, never
broken and never executable.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Iterable, Mapping, Sequence

from windowsill import transcript, usage

# ---------------------------------------------------------------------------
# inline styles (§8.4: self-contained, because it is email)
# ---------------------------------------------------------------------------

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

STYLES: dict[str, str] = {
    "body": f"margin:0;padding:24px;background:#f6f7f8;font-family:{_FONT};font-size:15px;line-height:1.55;color:#1a1a1a;",
    "card": "max-width:760px;margin:0 auto;padding:28px 32px;background:#ffffff;border:1px solid #e3e5e8;border-radius:8px;",
    "h1": "margin:1.2em 0 0.5em;font-size:22px;line-height:1.3;font-weight:600;",
    "h2": "margin:1.4em 0 0.5em;font-size:18px;line-height:1.3;font-weight:600;",
    "h3": "margin:1.4em 0 0.4em;font-size:16px;line-height:1.3;font-weight:600;",
    "p": "margin:0 0 0.9em;",
    "pre": (
        "margin:0 0 1em;padding:12px 14px;background:#f4f5f7;border:1px solid #e3e5e8;border-radius:6px;"
        f"overflow-x:auto;font-family:{_MONO};font-size:13px;line-height:1.45;white-space:pre-wrap;"
    ),
    "code": f"padding:1px 4px;background:#f0f1f3;border-radius:3px;font-family:{_MONO};font-size:13px;",
    "ul": "margin:0 0 0.9em;padding-left:1.4em;",
    "li": "margin:0 0 0.25em;",
    "blockquote": "margin:0 0 1em;padding:2px 0 2px 14px;border-left:3px solid #d8dade;color:#4a4d52;",
    "hr": "margin:1.6em 0;border:0;border-top:1px solid #e3e5e8;",
    "table": "margin:0 0 1em;border-collapse:collapse;width:100%;",
    "th": "padding:6px 10px;border:1px solid #e3e5e8;background:#f4f5f7;text-align:left;font-weight:600;",
    "td": "padding:6px 10px;border:1px solid #e3e5e8;vertical-align:top;",
    "a": "color:#0b62c4;",
    "label": "margin:1.6em 0 0.3em;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:#6b6f76;font-weight:600;",
    "fact": "margin:0 0 0.4em;font-size:15px;",
    "facttable": "margin:0;border-collapse:collapse;",
    "factkey": "padding:2px 14px 2px 0;color:#6b6f76;white-space:nowrap;",
    "factval": "padding:2px 0;",
    "caveat": "margin:0 0 0.6em;font-size:13px;color:#6b6f76;",
    "attempt": "margin:0.7em 0 0.1em;font-size:15px;",
    "attemptsummary": f"margin:0 0 0.2em 1.6em;font-family:{_MONO};font-size:13px;color:#4a4d52;",
}


def _style(name: str) -> str:
    return f' style="{STYLES[name]}"'


# ---------------------------------------------------------------------------
# Markdown subset -> HTML
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```+\s*(\S*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_DIVIDER_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

# Inline spans, applied to already-escaped text. `code` first and non-greedy:
# whatever is inside a code span must not then be read as emphasis.
_INLINE_CODE_RE = re.compile(r"`([^`]+?)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(\S(?:[^*]*?\S)?)\*(?![\w*])")


def _inline(text: str) -> str:
    """Render inline Markdown in one line of already-HTML-escaped text.

    Code spans are extracted to placeholders before emphasis runs, so
    ``*`` inside ``\\`` ``\\`` stays literal -- an argv or a glob in a summary
    is the common case, and italicising half of it would be wrong.
    """
    spans: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        spans.append(f"<code{_style('code')}>{match.group(1)}</code>")
        return f"\x00{len(spans) - 1}\x00"

    out = _INLINE_CODE_RE.sub(_stash, text)
    out = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}"{_style("a")}>{m.group(1)}</a>', out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], out)


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_table_row(line: str) -> bool:
    return "|" in line and line.strip().startswith("|")


def markdown_to_html(text: str) -> str:
    """Render the Markdown subset this module supports (see module docstring).

    Line-based and single-pass: every line is escaped first, so anything the
    renderer does not recognise degrades to visible text and no input can emit
    markup of its own.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag = ""
    quote: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p{_style('p')}>{'<br>'.join(paragraph)}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            rendered = "".join(f"<li{_style('li')}>{item}</li>" for item in list_items)
            out.append(f"<{list_tag}{_style('ul')}>{rendered}</{list_tag}>")
            list_items.clear()
            list_tag = ""

    def flush_quote() -> None:
        if quote:
            out.append(f"<blockquote{_style('blockquote')}>{'<br>'.join(quote)}</blockquote>")
            quote.clear()

    def flush_all() -> None:
        flush_paragraph()
        flush_list()
        flush_quote()

    index = 0
    while index < len(lines):
        line = lines[index]

        fence = _FENCE_RE.match(line)
        if fence:
            flush_all()
            index += 1
            block: list[str] = []
            while index < len(lines) and not _FENCE_RE.match(lines[index]):
                block.append(html.escape(lines[index]))
                index += 1
            index += 1  # closing fence (or EOF)
            out.append(f"<pre{_style('pre')}><code>{chr(10).join(block)}</code></pre>")
            continue

        if not line.strip():
            flush_all()
            index += 1
            continue

        if _RULE_RE.match(line):
            flush_all()
            out.append(f"<hr{_style('hr')}>")
            index += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_all()
            level = min(len(heading.group(1)), 3)
            out.append(f"<h{level}{_style(f'h{level}')}>{_inline(html.escape(heading.group(2).strip()))}</h{level}>")
            index += 1
            continue

        # A table is a row followed by a `|---|---|` divider; without the
        # divider it is just a line that happens to contain pipes.
        if _is_table_row(line) and index + 1 < len(lines) and _TABLE_DIVIDER_RE.match(lines[index + 1]):
            flush_all()
            header = _cells(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and _is_table_row(lines[index]):
                rows.append(_cells(lines[index]))
                index += 1
            head = "".join(f"<th{_style('th')}>{_inline(html.escape(cell))}</th>" for cell in header)
            body = "".join("<tr>" + "".join(f"<td{_style('td')}>{_inline(html.escape(cell))}</td>" for cell in row) + "</tr>" for row in rows)
            out.append(f"<table{_style('table')}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue

        quoted = _QUOTE_RE.match(line)
        if quoted:
            flush_paragraph()
            flush_list()
            quote.append(_inline(html.escape(quoted.group(1))))
            index += 1
            continue

        bullet = _BULLET_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if bullet or ordered:
            flush_paragraph()
            flush_quote()
            tag = "ul" if bullet else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            list_items.append(_inline(html.escape((bullet or ordered).group(1))))
            index += 1
            continue

        flush_list()
        flush_quote()
        paragraph.append(_inline(html.escape(line.strip())))
        index += 1

    flush_all()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# the facts the body reports (§6 runs row + §7 state + §4 usage)
# ---------------------------------------------------------------------------

#: How the §7 job state a run left behind reads as "what happens next". Only
#: states the lifecycle actually has; the two non-terminal ones are refined by
#: :func:`next_action` from the ``sched`` block gate (§5.4).
_NEXT_ACTION: dict[str, str] = {
    "done": "finished — nothing further.",
    "failed": "failed — will not retry.",
    "expired": "expired: its --before deadline passed before it could run — will not run.",
    "skipped": "skipped — will not run.",
    "cancelled": "cancelled — will not run.",
    "ready": "back on the queue; the next tick will dispatch it.",
    "queued": "queued; a tick will promote it when it is due.",
    "held": "held — needs `windowsill release` before it runs again.",
    "running": "still running.",
    # §7 "blocked": the main thread ended while work it dispatched was still
    # running, so its closing summary describes work that had not happened.
    # Nothing moves this job on its own -- :func:`next_action` names the human.
    "blocked": "blocked — the run ended while work it had started was still going, so it is not finished.",
}


@dataclass(frozen=True)
class Attempt:
    """One row of the ``runs`` table (§6), as the mail reports it.

    A job that hits the window ceiling is requeued rather than failed (§5.4), so
    "the run" a completion mail describes is routinely the *last* of several,
    and the two that came before it are where the night actually went. The job
    dir holds only the last attempt's ``stream.jsonl`` -- the next dispatch
    overwrites it -- so ``summary`` is the copy :mod:`windowsill.runner` wrote
    into ``runs.summary`` while that attempt was still the current one.
    """

    n: int
    started_at: str | None = None
    finished_at: str | None = None
    outcome: str = ""
    delta_pct: int | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    summary: str = ""


@dataclass
class RunFacts:
    """Everything the completion email says about one finished run.

    Assembled by the caller from the pieces it already has in hand -- the
    ``jobs``/``runs`` rows (§6), the result envelope, and the post-run
    ``/usage`` reading (§4.3) -- so this module reads no database and no clock.
    """

    job_id: int
    name: str
    #: The §7 job state the run left behind.
    job_state: str
    #: The §6 ``runs.outcome`` value.
    outcome: str
    #: The final assistant message, as Markdown.
    summary: str = ""
    result: Mapping[str, Any] | None = None
    delta_pct: int | None = None
    permission_denials: int | None = None
    #: ``sched.blocked_until`` when the queue is held (§5.4), and why. Set
    #: only while the hold is *live*: an expired one is left in the ``sched``
    #: row forever (the scheduler just stops honouring it), and this module
    #: reads no clock, so the caller does the comparison -- see
    #: :func:`windowsill.runner._run_facts`. Present here means "held now".
    blocked_until: str | None = None
    blocked_reason: str | None = None
    #: Whether ``windowsill resume <id>`` has a conversation to resume.
    has_conversation: bool = False
    #: Background work still in flight when the run's stream ended -- what put
    #: the job in §7's ``blocked`` state. ``task_id`` / ``description`` /
    #: ``task_type``, from :func:`windowsill.transcript.live_background_tasks`.
    live_tasks: list[dict[str, str]] = field(default_factory=list)
    #: The window reading (§4) behind the usage block, as label/value pairs.
    usage: list[tuple[str, str]] = field(default_factory=list)
    #: Every attempt at this job, oldest first, when there was more than one.
    #: Left empty for a single-run job so its mail is exactly what it always
    #: was -- an "Attempts" section listing one attempt is noise.
    attempts: list[Attempt] = field(default_factory=list)


def outcome_line(facts: RunFacts) -> str:
    """The run's one-line result: the transcript's own footer fields, plus the
    two things only the DB knows (§4.3's Δ%, §9's denial count).

    Same fields, same order as ``transcript.md``'s footer -- the email is not a
    second opinion about how the run went.
    """
    parts = list(transcript.result_parts(facts.result)) if facts.result else [facts.outcome]
    # The envelope of an "incomplete" run reads "success" -- that is the whole
    # problem it names (§7 "blocked"), so the count has to be stated next to it
    # or the line says the opposite of what happened.
    if facts.live_tasks:
        count = len(facts.live_tasks)
        parts.append(f"{count} background task{'s' if count != 1 else ''} still running")
    if facts.delta_pct is not None:
        parts.append(f"Δ {facts.delta_pct}% of the window")
    if facts.permission_denials:
        parts.append(f"{facts.permission_denials} permission denial{'s' if facts.permission_denials != 1 else ''}")
    return " · ".join(parts)


def next_action(facts: RunFacts) -> list[str]:
    """What windowsill will do next, in plain sentences (§7 + §5.4).

    The states are §7's, read off the job row -- nothing here invents one. The
    interesting case is a job back at ``ready`` while ``sched.blocked_until``
    holds the queue: that is §5.4's rate-limit requeue, i.e. the run ran out of
    window and will pick up by itself when the next one opens.

    The last line is always the manual path (§8.1's ``windowsill resume``), because
    the point of the mail is that the reader never has to go looking for it.

    ``blocked`` (§7) is the state where that last line stops being a footnote
    and becomes the instruction: nothing in windowsill will move the job, so
    the mail names the human's command outright and lists what was still
    running when the run stopped.
    """
    lines: list[str] = []
    blocked = bool(facts.blocked_until)
    if facts.job_state == "blocked":
        lines.append(_NEXT_ACTION["blocked"])
        for task in facts.live_tasks:
            what = task.get("description") or task.get("task_id") or "?"
            kind = "subagent" if task.get("task_type") == "local_agent" else "background command"
            lines.append(f"Still running at exit ({kind}): {what}")
        lines.append(f"Nothing will move this job on its own — pick it up with: ccresume {facts.job_id}")
        return lines
    if facts.job_state == "ready" and blocked:
        limit = facts.blocked_reason and "rate limit" in facts.blocked_reason
        what = "will resume automatically in the next window" if limit else "queued, blocked"
        lines.append(f"{what} at {_local(facts.blocked_until)}.")
        if facts.blocked_reason:
            lines.append(f"Reason: {facts.blocked_reason}.")
    else:
        lines.append(_NEXT_ACTION.get(facts.job_state, f"state {facts.job_state}."))
        if blocked:
            lines.append(f"The queue is blocked until {_local(facts.blocked_until)}.")
    if facts.has_conversation:
        lines.append(f"To pick the conversation up by hand: windowsill resume {facts.job_id}")
    else:
        lines.append(f"No conversation to resume (windowsill show {facts.job_id} for the transcript).")
    return lines


def usage_rows(
    reading: "usage.ParsedUsage | None",
    *,
    windows_in_30d: int | None = None,
    max_sessions: int | None = None,
) -> list[tuple[str, str]]:
    """The window block: what the ``/usage`` oracle last read (§4).

    The same data as Claude Code's own ``/usage`` -- session %, when the window
    resets, the weekly buckets it reported -- plus §5.3's trailing-30d window
    count against the ceiling, which is the number the whole tool exists to
    keep down (§1).

    A closed window is stated as closed rather than shown as ``0%``: the
    percentage is display-only and the reset clause is the discriminator
    (§4.1), so printing a bare 0% would read as "a fresh window" when it means
    "no window".
    """
    rows: list[tuple[str, str]] = []
    if reading is not None:
        if reading.is_open:
            rows.append(("Session window", f"{reading.pct_used}% used" if reading.pct_used is not None else "open"))
            rows.append(("Resets", _local(reading.reset_at.isoformat()) if reading.reset_at else "-"))
        else:
            rows.append(("Session window", "closed — no window open"))
        if reading.weekly_pct is not None:
            weekly = f"{reading.weekly_pct}% used"
            if reading.weekly_reset_at is not None:
                weekly += f", resets {_local(reading.weekly_reset_at.isoformat())}"
            rows.append(("Week (all models)", weekly))
        if reading.extra_bucket:
            rows.append((f"Week ({reading.extra_bucket})", f"{reading.extra_pct}% used"))
    if windows_in_30d is not None:
        rows.append(("Windows in 30d", f"{windows_in_30d} / {max_sessions}" if max_sessions is not None else str(windows_in_30d)))
    return rows


# ---------------------------------------------------------------------------
# the attempt history (§5.4: a requeued job runs more than once)
# ---------------------------------------------------------------------------

#: How much of an attempt's closing line the history shows. ``runs.summary``
#: already holds a single clipped line, but this render must survive whatever
#: is actually in that column, including a paragraph someone wrote by hand.
ATTEMPT_SUMMARY_CHARS = 240

#: The sentence that keeps the totals honest, printed next to them.
#:
#: windowsill records no token counts and no dollar cost anywhere; the only
#: cost unit in the database is window percentage (§4.3's ``pct_before`` /
#: ``pct_after``), so that is the only one reported here and nothing is
#: estimated from it. It is also not additive across attempts: a rate-limited
#: run ends at 100% and its successor starts at 0% *of a different window*, so
#: summing the deltas measures only what was spent inside each window we
#: watched. Whatever the account burned between them -- and a window boundary
#: is exactly where the unobserved spending lives -- is not in these numbers.
#: Hence "floor": the true cost is this or more, never less.
WINDOW_PCT_CAVEAT = "Window % is per-window and resets between attempts, so the total is a floor, not a sum."


def _clip(text: str, limit: int = ATTEMPT_SUMMARY_CHARS) -> str:
    """One line of at most ``limit`` characters, whatever came in."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _duration(seconds: float | None) -> str:
    """A span as the coarsest form that still reads as a duration."""
    if seconds is None:
        return "-"
    hours, rest = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _elapsed(start: str | None, end: str | None) -> float | None:
    """Seconds between two stored timestamps, or ``None`` if either is missing
    or unparseable -- a half-written ``runs`` row must not break the mail."""
    if not start or not end:
        return None
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (last - first).total_seconds()


def window_pct_spent(attempts: Sequence[Attempt]) -> int:
    """Window % spent across every attempt -- a floor; see :data:`WINDOW_PCT_CAVEAT`.

    Negative deltas are dropped rather than subtracted: a delta goes negative
    only when the window reset mid-run, which means the reading after is from a
    different window than the reading before and their difference is not a cost.
    """
    return sum(max(0, attempt.delta_pct) for attempt in attempts if attempt.delta_pct is not None)


def _ran_seconds(attempt: Attempt) -> float | None:
    """How long the attempt was actually running.

    The result envelope's ``duration_ms`` when there is one; otherwise the gap
    between the run row's own timestamps, which is all a killed or timed-out
    attempt leaves behind.
    """
    if attempt.duration_ms:
        return attempt.duration_ms / 1000
    return _elapsed(attempt.started_at, attempt.finished_at)


def attempt_line(attempt: Attempt) -> str:
    """One attempt as a single line: when it started, how long, how it went."""
    parts = [f"#{attempt.n}", _local(attempt.started_at), _duration(_ran_seconds(attempt)), attempt.outcome or "?"]
    if attempt.delta_pct is not None:
        parts.append(f"Δ {attempt.delta_pct}%")
    if attempt.num_turns is not None:
        parts.append(f"{attempt.num_turns} turn{'s' if attempt.num_turns != 1 else ''}")
    return " · ".join(parts)


def totals_line(attempts: Sequence[Attempt]) -> str:
    """The line that makes "it took ten hours" visible.

    Wall time is measured from the *first* attempt's start to the last one's
    finish, which is much longer than the run durations added up: the hours a
    rate-limited job spends waiting for the next window are the whole reason
    this section exists, and they appear in no run row.
    """
    first, last = attempts[0], attempts[-1]
    wall = _elapsed(first.started_at, last.finished_at)
    ran = sum(_ran_seconds(attempt) or 0 for attempt in attempts)
    turns = sum(attempt.num_turns or 0 for attempt in attempts)
    return " · ".join(
        [
            f"{len(attempts)} runs",
            f"{_duration(wall)} wall ({_local(first.started_at)} → {_local(last.finished_at)})",
            f"{_duration(ran)} running",
            f"{turns} turns",
            f"{window_pct_spent(attempts)}% of the window",
        ]
    )


def subject(facts: RunFacts) -> str:
    """The Subject line: job id, name, state, and how it went (§8.4).

    ``windowsill-complete: done`` said neither which job nor whether it did anything;
    a subject is the only part of the mail a phone shows.
    """
    detail = list(transcript.result_parts(facts.result)) if facts.result else [facts.outcome]
    # The run count belongs in the subject or not at all: "3 runs" is the
    # difference between a three-minute job and a job that spent the night
    # waiting out two rate limits, and the body saying so is no use to the
    # reader deciding on a lock screen whether to open it.
    if len(facts.attempts) > 1:
        detail.append(f"{len(facts.attempts)} runs")
    # A blocked run's envelope subtype is "success" (§7); on a phone that is
    # the only word the reader sees, so it must not be the last one.
    if facts.live_tasks:
        detail.append(f"{len(facts.live_tasks)} still running")
    if facts.permission_denials:
        detail.append(f"{facts.permission_denials} denied")
    return f"[windowsill {facts.job_id}] {facts.name}: {facts.job_state} ({', '.join(detail)})"


# ---------------------------------------------------------------------------
# timestamps (§8.1: stored UTC, displayed as bare system local time)
# ---------------------------------------------------------------------------


def _local(value: str | None) -> str:
    """A stored UTC timestamp as bare local wall-clock time (§8.1).

    The CLI's own copy of this rule is ``cli._local``; mail must not import the
    CLI (the CLI imports the runner, which sends the mail), and the rule is
    three lines.
    """
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# the body, both parts
# ---------------------------------------------------------------------------

#: What a run with no assistant prose at all gets instead of a blank email.
NO_SUMMARY = "_(the run produced no closing message)_"

_NO_SUMMARY_TEXT = "(the run produced no closing message)"


def _facts_block_html(label: str, lines: Sequence[str]) -> str:
    body = "".join(f"<p{_style('fact')}>{_inline(html.escape(line))}</p>" for line in lines)
    return f"<p{_style('label')}>{html.escape(label)}</p>{body}"


def _attempts_block_html(attempts: Sequence[Attempt]) -> str:
    parts = [
        f"<p{_style('label')}>Attempts</p>",
        f"<p{_style('fact')}>{html.escape(totals_line(attempts))}</p>",
        f"<p{_style('caveat')}>{html.escape(WINDOW_PCT_CAVEAT)}</p>",
    ]
    for attempt in attempts:
        parts.append(f"<p{_style('attempt')}>{html.escape(attempt_line(attempt))}</p>")
        if attempt.summary.strip():
            parts.append(f"<p{_style('attemptsummary')}>{html.escape(_clip(attempt.summary))}</p>")
    return "".join(parts)


def _attempts_block_text(attempts: Sequence[Attempt]) -> str:
    lines = [f"  {totals_line(attempts)}", f"  {WINDOW_PCT_CAVEAT}", ""]
    for attempt in attempts:
        lines.append(f"  {attempt_line(attempt)}")
        if attempt.summary.strip():
            lines.append(f"      {_clip(attempt.summary)}")
    return "Attempts:\n" + "\n".join(lines)


def _usage_block_html(rows: Iterable[tuple[str, str]]) -> str:
    cells = "".join(f"<tr><td{_style('factkey')}>{html.escape(key)}</td><td{_style('factval')}>{html.escape(value)}</td></tr>" for key, value in rows)
    return f"<p{_style('label')}>Window usage</p><table{_style('facttable')}><tbody>{cells}</tbody></table>"


def render_html(facts: RunFacts) -> str:
    """The HTML part: the summary, then the three appended blocks (§8.4)."""
    summary = markdown_to_html(facts.summary.strip() or NO_SUMMARY)
    sections = [
        summary,
        f"<hr{_style('hr')}>",
        _facts_block_html("Outcome", [outcome_line(facts)]),
    ]
    # The history sits under the outcome because it is the outcome, widened:
    # the run this mail is about is only the last of several (§5.4).
    if len(facts.attempts) > 1:
        sections.append(_attempts_block_html(facts.attempts))
    sections.append(_facts_block_html("Next", next_action(facts)))
    if facts.usage:
        sections.append(_usage_block_html(facts.usage))
    card = "\n".join(sections)
    return (
        "<!DOCTYPE html>\n"
        f'<html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(subject(facts))}</title></head>"
        f"<body{_style('body')}><div{_style('card')}>\n{card}\n</div></body></html>\n"
    )


def render_text(facts: RunFacts) -> str:
    """The plaintext part: the same content, unrendered (§8.4).

    The summary is Markdown, so a text-only reader loses the styling and
    nothing else -- which is the whole reason the body is built from the
    Markdown rather than from HTML.
    """
    blocks = [
        facts.summary.strip() or _NO_SUMMARY_TEXT,
        "-" * 60,
        f"Outcome: {outcome_line(facts)}",
    ]
    if len(facts.attempts) > 1:
        blocks.append(_attempts_block_text(facts.attempts))
    blocks.append("Next:\n" + "\n".join(f"  {line}" for line in next_action(facts)))
    if facts.usage:
        blocks.append("Window usage:\n" + "\n".join(f"  {key}: {value}" for key, value in facts.usage))
    return "\n\n".join(blocks) + "\n"


def build_message(facts: RunFacts) -> EmailMessage:
    """The ``multipart/alternative`` message body: text first, then HTML.

    Headers other than ``Subject`` are the transport's business
    (:mod:`windowsill.notify` adds ``To``/``From`` from the config), so this
    returns a message the sender finishes addressing.
    """
    message = EmailMessage()
    message["Subject"] = subject(facts)
    message.set_content(render_text(facts))
    message.add_alternative(render_html(facts), subtype="html")
    return message


# ---------------------------------------------------------------------------
# the rate-limit ping (§5.4 + [notify].on_rate_limit)
# ---------------------------------------------------------------------------


def rate_limit_notice(
    *,
    job_id: int,
    name: str,
    blocked_until: str | None,
    retries_left: int | None = None,
    usage: Sequence[tuple[str, str]] = (),
) -> tuple[str, str]:
    """``(title, body)`` for the "still alive, back at 02:00" ping (§8.4).

    Deliberately not a report. A rate-limited run is requeued, not finished
    (§5.4), so the summary mail has not been earned yet and sending one now
    would describe a job that is still going. What the reader needs at 21:21 is
    the one thing they cannot see from silence: that the queue knows, and when
    it will pick up again. So: one line, then the same usage block the summary
    mail carries (:func:`usage_rows`), so the two agree.

    The retry line says what it says because a rate limit deliberately does
    *not* consume a retry -- it did not fail, it ran out of window (§2.3, and
    :func:`windowsill.runner._requeue_after_limit`). Printing a bare "2 of 3
    retries left" invites the reader to assume this pause used one.
    """
    resumes = _local(blocked_until)
    title = f"{name}: rate-limited, back at {resumes}"
    lines = [f"Out of window; requeued and resuming at {resumes}."]
    if retries_left is not None:
        lines.append(f"This pause cost no retry ({retries_left} still available for an actual failure).")
    lines.extend(f"{key}: {value}" for key, value in usage)
    lines.append(f"windowsill show {job_id}")
    return title, "\n".join(lines)


def build_rate_limit_message(job_id: int, title: str, body: str) -> EmailMessage:
    """The pause ping as an email: the ntfy one-liner, addressed.

    Plain text and nothing else. There is no summary to render -- the run is
    not over -- so the ``multipart/alternative`` machinery above would be
    styling four lines. The subject keeps :func:`subject`'s ``[windowsill
    <id>]`` prefix so both mails about a job thread and sort together.
    """
    message = EmailMessage()
    message["Subject"] = f"[windowsill {job_id}] {title}"
    message.set_content(body if body.endswith("\n") else body + "\n")
    return message
