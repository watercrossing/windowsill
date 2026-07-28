"""windowsill job files: directive parsing and the pass-through split.

Implements design/02-job-file.md §2 exactly. A job file is a UTF-8 text file: a
directive header, a blank line, then the prompt body (verbatim, no
templating, no substitution).

The central design rule (§2.2) is that directives are split into two
kinds *by lookup, not by syntax*:

- **Scheduler directives** (§2.3, the complete, enumerated set) are
  consumed here and never reach ``claude``.
- **Everything else** is passed through untouched, in order, as argv
  elements destined for ``claude``. This module never validates or
  rejects an unrecognised directive — that is the entire point: a new
  Claude Code flag works on day one with no parser change.

This module is pure: it takes job-file text and returns a
:class:`JobFile`. No filesystem, no subprocess, no network, no clock.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# Directive lines: "^#\$\s+(.*)$" (§2.1 rule 1).
_DIRECTIVE_RE = re.compile(r"^#\$\s+(.*)$")

# §2.3, the complete set of scheduler directives, split by arity. A
# boolean directive takes no value; everything else takes exactly one
# logical value (possibly several shlex tokens, rejoined with spaces —
# see e.g. "--at tomorrow 02:00").
BOOLEAN_DIRECTIVES: frozenset[str] = frozenset(
    {
        "--anchor",
        "--allow-dirty",
        "--hold",
        "--no-resume-on-limit",
    }
)

VALUE_DIRECTIVES: frozenset[str] = frozenset(
    {
        "--name",
        "--at",
        "--before",
        "--window",
        "--cwd",
        "--log",
        "--resume",
        "--resume-from",
        "--hold-jid",
        "--retry",
        "--priority",
        "--isolate",
        "--notify",
        "--timeout",
    }
)

SCHEDULER_DIRECTIVES: frozenset[str] = BOOLEAN_DIRECTIVES | VALUE_DIRECTIVES

# Directives whose single value gets further coerced beyond a plain str.
_INT_DIRECTIVES = {"--retry", "--priority"}
_COMMA_LIST_DIRECTIVES = {"--hold-jid"}


@dataclass
class Directives:
    """The scheduler directives enumerated in design/02-job-file.md §2.3.

    Fields left at their dataclass default mean "absent from the job
    file" — for defaults that depend on context this module doesn't
    have (submit-time cwd, filename stem, config), that's ``None`` (or
    ``[]`` for ``hold_jid``) and resolving it is the caller's job.
    Defaults that are static and spec-given (``window``, ``retry``,
    ``priority``, ``timeout``, and the boolean flags) are baked in here.
    """

    name: str | None = None
    at: str | None = None
    before: str | None = None
    window: str = "share"
    anchor: bool = False
    cwd: str | None = None
    log: str | None = None
    resume: str | None = None
    resume_from: str | None = None
    hold_jid: list[str] = field(default_factory=list)
    retry: int = 0
    priority: int = 0
    isolate: str | None = None
    allow_dirty: bool = False
    notify: str | None = None
    hold: bool = False
    timeout: str = "4h"
    no_resume_on_limit: bool = False


@dataclass
class JobFile:
    """The result of parsing one job file.

    ``passthrough`` is the pass-through argv list, in the order
    directives appeared in the file — argv elements, never a
    concatenated shell string (§2.2). ``body`` is the verbatim prompt
    body. ``model`` mirrors a ``--model`` pass-through value, if any
    (§2.2: "Mirror, don't intercept" — it also still appears in
    ``passthrough``).
    """

    directives: Directives
    passthrough: list[str]
    body: str
    model: str | None = None


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _apply_directive(directives: Directives, name: str, tokens: list[str]) -> None:
    """Assign one recognised scheduler directive's tokens onto ``directives``."""
    field_name = name.lstrip("-").replace("-", "_")

    if name in BOOLEAN_DIRECTIVES:
        setattr(directives, field_name, True)
        return

    # A directive's value may itself have been written as several shlex
    # tokens (e.g. an unquoted "--at tomorrow 02:00") — rejoin them into
    # one logical value.
    value = " ".join(tokens[1:])

    if name in _COMMA_LIST_DIRECTIVES:
        setattr(directives, field_name, [part.strip() for part in value.split(",") if part.strip()])
    elif name in _INT_DIRECTIVES:
        setattr(directives, field_name, int(value))
    else:
        setattr(directives, field_name, value)


def _mirrored_model(passthrough: list[str]) -> str | None:
    """Find the value of a "--model" pass-through directive, if any.

    --model is pass-through (§2.2), never consumed — this only mirrors
    its value for display/anchor-default purposes elsewhere. The last
    occurrence wins, matching ordinary flag-overriding behaviour.
    """
    model: str | None = None
    for i, tok in enumerate(passthrough):
        if tok == "--model" and i + 1 < len(passthrough):
            model = passthrough[i + 1]
    return model


def parse_jobfile(text: str) -> JobFile:
    """Parse job-file text into directives, pass-through argv, and body.

    See module docstring and design/02-job-file.md §2 for the exact rules. Pure:
    no I/O, no clock — ``text`` is the entire job file's contents.
    """
    raw_lines = text.splitlines(keepends=True)
    plain_lines = text.splitlines()
    n = len(plain_lines)

    idx = 0
    if n > 0 and plain_lines[0].startswith("#!"):
        # Rule 2: a leading #! line is ignored, so job files can be
        # chmod +x and self-submit.
        idx = 1

    directives = Directives()
    passthrough: list[str] = []

    i = idx
    terminated = False
    body_start = n  # no blank line / prose found ⇒ no body

    while i < n:
        line = plain_lines[i]

        if _is_blank(line):
            # Rule 3: body starts right after the first blank line
            # following the header.
            body_start = i + 1
            terminated = True
            break

        match = _DIRECTIVE_RE.match(line)
        if match:
            content = match.group(1)

            if content.startswith("#"):
                # Rule 4: "#$ #..." is a comment.
                i += 1
                continue

            # Rule 5: a trailing "\" continues the directive onto the
            # next physical line (no "#$" prefix required there).
            parts = [content]
            j = i
            while parts[-1].rstrip().endswith("\\"):
                stripped = parts[-1].rstrip()
                parts[-1] = stripped[:-1].rstrip()
                j += 1
                if j >= n:
                    break
                parts.append(plain_lines[j])

            full_text = " ".join(part for part in parts if part != "")
            # Rule 6: shlex.split, never a bare .split().
            tokens = shlex.split(full_text) if full_text.strip() else []
            i = j + 1

            if not tokens:
                continue

            name = tokens[0]
            if name in SCHEDULER_DIRECTIVES:
                _apply_directive(directives, name, tokens)
            else:
                passthrough.extend(tokens)
            continue

        if line.startswith("#"):
            # Rule 4: a bare "#" line inside the header is a comment.
            i += 1
            continue

        # Rule 1: header terminated by prose. It cannot resume later
        # in the file — stop looking for directives entirely. Since
        # there was no blank-line separator, the body starts right
        # here, verbatim, including this line.
        body_start = i
        terminated = True
        break

    if not terminated:
        body_start = n

    body = "".join(raw_lines[body_start:]) if body_start < len(raw_lines) else ""

    return JobFile(
        directives=directives,
        passthrough=passthrough,
        body=body,
        model=_mirrored_model(passthrough),
    )
