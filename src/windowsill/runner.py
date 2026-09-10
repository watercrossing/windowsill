"""windowsill run execution: invocation, detached spawn, reap, timeout.

Implements design/03-execution-model.md §3.2 (the invocation), §3.3 (safety for unattended
runs), and the parts of §6.2 (the job-directory filesystem layout) that a
run touches: ``cmd.json``, ``stream.jsonl``, ``result.json``, ``resume.sh``.
(``job.md`` is the verbatim submission and is written at submit time, not
here.)

Every seam a caller might need to fake is a keyword argument with a real
default:

- ``claude_cmd`` -- the argv prefix that gets exec'd. Production passes
  ``("claude",)``; tests substitute a fake local script that writes a
  canned ``stream-json`` transcript and exits, so no real ``claude`` ever
  runs.
- ``popen`` -- defaults to :class:`subprocess.Popen`; tests can substitute
  a spy/fake to assert on how a process would have been spawned without
  spawning one.
- ``clock`` / ``sleep`` -- default to :func:`time.monotonic` /
  :func:`time.sleep`; tests substitute deterministic fakes so timeout
  enforcement is tested without depending on real wall-clock time.
- ``now`` -- default produces a real UTC ISO-8601 timestamp; tests pass a
  fixed/deterministic callable.
- ``root`` -- the job-directory root (normally ``~/.windowsill``); always passed
  explicitly by callers, so tests point it at a ``tmp_path``.
- ``on_complete`` -- the argv of the next detached process to spawn once a
  run finishes (chain dispatch, wired up by a later module). This module
  does not know or care what it is; it just ``Popen``s it and exits,
  never awaiting it in-process, so a fast-failing job can't fork-bomb.

Runs are spawned in their own process group (``start_new_session=True``,
i.e. ``setsid``) so §3.3's "SIGTERM the process group, then SIGKILL after
the grace period" can target the whole tree with :func:`os.killpg`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from windowsill import db, mail, notify, transcript, usage, window

# ---------------------------------------------------------------------------
# job directory layout (§6.2)
# ---------------------------------------------------------------------------


def job_dir(root: Path, job_id: int, name: str) -> Path:
    """``<root>/jobs/<5-digit zero-padded id>-<name>`` (§6.2: "so ls sorts").

    Pure path arithmetic -- does not create the directory.
    """
    return root / "jobs" / f"{job_id:05d}-{name}"


def log_destination(log_path: str | None, *, job_id: int, name: str, cwd: str) -> Path | None:
    """Resolve a job's ``--log`` (§2.3) into the extra file its rendered
    transcript is written to, or ``None`` when the job didn't ask for one.

    A value ending in a path separator, or naming a directory that already
    exists, means "in there", under the same ``<5-digit id>-<name>`` naming
    the job dir uses (§6.2); anything else is taken as the file itself. A
    relative path resolves against the *job's* ``cwd``, not windowsill's: the job
    file is written from the project's point of view, while ``windowsill tick``
    runs from wherever cron left it.
    """
    if not log_path:
        return None
    path = Path(log_path).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    if log_path.endswith(("/", os.sep)) or path.is_dir():
        path = path / f"{job_id:05d}-{name}.md"
    return path


def write_job_md(dir_: Path, text: str) -> Path:
    """Write ``job.md``: the verbatim submission (§6.2), at submit time."""
    path = dir_ / "job.md"
    path.write_text(text)
    return path


def write_cmd_json(dir_: Path, *, argv: Sequence[str], cwd: str, env: dict[str, str] | None) -> Path:
    """Write ``cmd.json``: the resolved argv + env + cwd, for reproducibility."""
    path = dir_ / "cmd.json"
    path.write_text(json.dumps({"argv": list(argv), "cwd": cwd, "env": env or {}}, indent=2) + "\n")
    return path


def write_result_json(dir_: Path, result_event: dict[str, Any]) -> Path:
    """Write ``result.json``: the final ``stream-json`` result event."""
    path = dir_ / "result.json"
    path.write_text(json.dumps(result_event, indent=2) + "\n")
    return path


def write_resume_sh(dir_: Path, *, cwd: str, claude_cmd: Sequence[str], conversation_id: str) -> Path:
    """Write ``resume.sh``: ``cd <cwd> && claude --resume <id>`` (§6.2)."""
    path = dir_ / "resume.sh"
    cmd = " ".join(shlex.quote(part) for part in claude_cmd)
    path.write_text(f"cd {shlex.quote(cwd)} && {cmd} --resume {shlex.quote(conversation_id)}\n")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# §3.2 the invocation
# ---------------------------------------------------------------------------


#: Every ``claude`` flag (confirmed via ``claude --help``, v2.1.218) that
#: names, restricts, or bypasses tool permissions. A job whose own
#: pass-through directives (§2.2) name any of these has an opinion of its
#: own about tool permissions, so §3.3's ``safety.allowed_tools`` default
#: must yield to it rather than injecting an allowlist on top -- the job's
#: own flag always wins; windowsill supplies a default, not a policy
#: override (§0.4 "pass-through beats validation", extended to defaults).
#:
#: ``--allowed-tools``/``--allowedTools`` and ``--disallowed-tools``/
#: ``--disallowedTools`` are the CLI's own two spellings for the same
#: flags. ``--permission-mode`` and both `--dangerously-skip-permissions``
#: and ``--allow-dangerously-skip-permissions`` bypass or reshape the
#: permission system entirely, which would make an injected allowlist
#: meaningless. ``--tools`` is a different mechanism (it restricts which
#: tools *exist* at all, not which are pre-approved) but a job naming it
#: has equally made its own decision about tool availability.
TOOL_PERMISSION_FLAGS = frozenset(
    {
        "--allowed-tools",
        "--allowedTools",
        "--disallowed-tools",
        "--disallowedTools",
        "--permission-mode",
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--tools",
    }
)


def names_any_flag(passthrough: Sequence[str], flags: frozenset[str]) -> bool:
    """Whether ``passthrough`` already names any flag in ``flags``.

    Recognises both ``--flag value`` and ``--flag=value`` forms. The shared
    engine behind every "does the job's own pass-through already have an
    opinion about X" yield rule in this module (§0.4 "pass-through beats
    validation", extended to defaults) -- :func:`names_tool_permission_flag`
    (§3.3's ``safety.allowed_tools``) and :func:`names_system_prompt_flag`
    (§3.2's appended system prompt) are both one-line wrappers over this,
    not separate implementations of the same read.
    """
    return any(token.split("=", 1)[0] in flags for token in passthrough)


def names_tool_permission_flag(passthrough: Sequence[str]) -> bool:
    """Whether ``passthrough`` already names a flag in :data:`TOOL_PERMISSION_FLAGS`.

    Used by :func:`build_argv` to decide whether to yield, and by callers
    that need to log the yield (§3.3's "silent-but-visible": no error, but
    the run's event log makes clear windowsill did not inject).
    """
    return names_any_flag(passthrough, TOOL_PERMISSION_FLAGS)


#: Every ``claude`` flag (confirmed via ``claude --help``, v2.1.218, plus a
#: grep of the installed CLI's bundle for two more it deliberately
#: ``.hideHelp()``s) that names or replaces the session's system prompt. A
#: job whose own pass-through directives (§2.2) name any of these has an
#: opinion of its own about the system prompt, so windowsill's default
#: appended prompt (§3.2) must yield to it entirely, the same shape as
#: :data:`TOOL_PERMISSION_FLAGS` -- the job's own flag always wins; this
#: also doubles as the feature's free per-job opt-out (``#$
#: --append-system-prompt ""``), so no new scheduler directive is needed.
#:
#: ``--append-system-prompt`` is the one windowsill injects (append, not
#: replace -- ``--system-prompt`` strips Claude Code's own harness
#: instructions, confirmed by ``--exclude-dynamic-system-prompt-sections``'s
#: help text: "Only applies with the default system prompt (ignored with
#: --system-prompt)"). ``--append-system-prompt-file`` and
#: ``--system-prompt-file`` do not appear in ``claude --help`` output but do
#: exist in the bundle, each mutually exclusive with its inline sibling: the
#: bundle contains the hard error strings "Cannot use both
#: --append-system-prompt and --append-system-prompt-file" and "Cannot use
#: both --system-prompt and --system-prompt-file". A job naming any of the
#: four is a job with its own idea about the system prompt.
SYSTEM_PROMPT_FLAGS = frozenset(
    {
        "--append-system-prompt",
        "--append-system-prompt-file",
        "--system-prompt",
        "--system-prompt-file",
    }
)


def names_system_prompt_flag(passthrough: Sequence[str]) -> bool:
    """Whether ``passthrough`` already names a flag in :data:`SYSTEM_PROMPT_FLAGS`.

    Used by :func:`build_argv` to decide whether to yield windowsill's
    default appended system prompt, and by :func:`resolve_system_prompt` to
    decide whether it is worth resolving one at all.
    """
    return names_any_flag(passthrough, SYSTEM_PROMPT_FLAGS)


def build_argv(
    passthrough: Sequence[str],
    *,
    resume: str | None = None,
    claude_cmd: Sequence[str] = ("claude",),
    allowed_tools: Sequence[str] = (),
    system_prompt: str | None = None,
    add_dirs: Sequence[str] = (),
) -> list[str]:
    """The argv for §3.2's invocation, minus ``cd <cwd> &&`` (that's the
    subprocess ``cwd=`` kwarg) and minus the body (goes on stdin, never
    argv -- see :func:`spawn`).

    ``claude_cmd`` is the injectable command seam: production passes
    ``("claude",)``; tests substitute a fake executable/argv that writes a
    canned stream + result and exits.

    ``allowed_tools`` is config's ``safety.allowed_tools`` default (§3.3,
    design/08-interface.md §8.3), rendered as a single ``--allowed-tools``
    argv token holding a comma-separated list (confirmed spelling and
    argument shape: ``claude --help``, v2.1.218 -- "Comma or
    space-separated list of tool names to allow"; the CLI also accepts
    ``--allowedTools``, but this file otherwise uses kebab-case flags
    throughout, so ``--allowed-tools`` is used here for consistency).
    Empty/unset ``allowed_tools`` injects nothing -- the pre-existing
    default behaviour, unchanged for any caller that doesn't pass it.
    Injection is also skipped -- silently, from this function's point of
    view -- when ``passthrough`` already names a tool-permission flag of
    its own (:func:`names_tool_permission_flag`); ``run_job`` is
    responsible for logging that yield (§3.3).

    ``system_prompt`` is windowsill's default appended system prompt (§3.2),
    already fully resolved by :func:`resolve_system_prompt` -- this function
    does no config reading, journal-path substitution, or re-execution
    detection of its own. Injected as ``--append-system-prompt <text>``,
    same yield shape as ``allowed_tools``: skipped when ``passthrough``
    already names a flag in :data:`SYSTEM_PROMPT_FLAGS`
    (:func:`names_system_prompt_flag`) -- the job's own flag always wins,
    which is also this feature's free per-job opt-out. ``add_dirs`` (the
    job dir, so the agent can write ``journal.md``) is injected as one
    ``--add-dir`` per entry, nested under the same yield check: granting
    directory access for a journal the prompt never told the agent about
    would be pointless, so it rises and falls with ``system_prompt``.
    ``--add-dir`` is variadic on the CLI (it swallows tokens until the
    next flag), so it is always the last injected token, directly
    abutting pass-through, whose every token is itself a flag.
    """
    argv = list(claude_cmd) + ["-p", "--output-format", "stream-json", "--verbose"]
    if resume:
        argv += ["--resume", resume]
    if allowed_tools and not names_tool_permission_flag(passthrough):
        argv += ["--allowed-tools", ",".join(allowed_tools)]
    if (system_prompt or add_dirs) and not names_system_prompt_flag(passthrough):
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        for add_dir in add_dirs:
            argv += ["--add-dir", add_dir]
    argv += list(passthrough)
    return argv


# ---------------------------------------------------------------------------
# timeout string -> seconds (§3.3 "--timeout")
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$")
_DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 1}


def parse_timeout_seconds(value: str | None) -> float | None:
    """Parse a duration like ``"4h"``, ``"30m"``, ``"90s"`` into seconds.

    ``None`` (no ``--timeout`` recorded on the job) means "do not enforce
    a timeout at all", not "zero".
    """
    if not value:
        return None
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ValueError(f"unparseable timeout: {value!r}")
    amount, unit = match.groups()
    return float(amount) * _DURATION_UNIT_SECONDS[unit]


# ---------------------------------------------------------------------------
# pid liveness + process-group signalling
# ---------------------------------------------------------------------------


def is_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process, via ``kill(pid, 0)``.

    This is how a *different* windowsill invocation than the one that spawned the
    run (e.g. a later ``windowsill tick``) checks on it -- it isn't the parent in
    the ``wait()`` sense, so signal-0 liveness is the only portable check.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# detached spawn (§3.3: own process group, output to the job dir)
# ---------------------------------------------------------------------------


def spawn(
    argv: Sequence[str],
    body: str,
    *,
    cwd: str,
    dir_: Path,
    env: dict[str, str] | None = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> subprocess.Popen:
    """Spawn ``argv`` detached in its own process group.

    The body goes on stdin via a file in ``dir_`` (never argv -- §3.2:
    bodies exceed ``ARG_MAX`` and contain everything a shell hates). Never
    ``shell=True``: pass-through directives are user text headed for argv,
    and a shell would turn a quoted directive into a command.
    stdout is redirected straight to ``dir_/stream.jsonl`` so a killed run
    still leaves a usable partial transcript (§3.2). stderr is discarded
    rather than interleaved into the same file, which would corrupt the
    one-json-object-per-line stream format.

    ``popen`` is the injectable spawn seam (defaults to
    :class:`subprocess.Popen`); ``argv`` (via :func:`build_argv`'s
    ``claude_cmd``) is the injectable command seam.
    """
    stdin_path = dir_ / ".stdin"
    stdin_path.write_text(body)
    stdout_path = dir_ / "stream.jsonl"
    with stdin_path.open("rb") as stdin_f, stdout_path.open("wb") as stdout_f:
        process = popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdin=stdin_f,
            stdout=stdout_f,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return process


# ---------------------------------------------------------------------------
# timeout enforcement (§3.3: SIGTERM the process group, SIGKILL after grace)
# ---------------------------------------------------------------------------


def wait_with_timeout(
    process: Any,
    *,
    timeout_seconds: float | None,
    grace_seconds: float = 30.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.05,
    terminate_group: Callable[[int], None] = _terminate_process_group,
    kill_group: Callable[[int], None] = _kill_process_group,
) -> bool:
    """Block until ``process`` exits, enforcing ``timeout_seconds``.

    On timeout, SIGTERM's the process group and waits up to
    ``grace_seconds`` more, then SIGKILL's it (§3.3). Returns whether a
    timeout was enforced. ``process`` only needs ``.pid`` and a
    ``.poll() -> int | None`` method (a real :class:`subprocess.Popen`
    satisfies this; so does a bare test fake).

    ``clock``/``sleep`` are the injectable time seam -- tests drive this
    deterministically with fakes rather than real wall-clock waits.
    """
    if timeout_seconds is None:
        while process.poll() is None:
            sleep(poll_interval)
        return False

    deadline = clock() + timeout_seconds
    while process.poll() is None and clock() < deadline:
        sleep(poll_interval)

    if process.poll() is not None:
        return False  # exited on its own before the deadline

    terminate_group(process.pid)
    grace_deadline = clock() + grace_seconds
    while process.poll() is None and clock() < grace_deadline:
        sleep(poll_interval)

    if process.poll() is None:
        kill_group(process.pid)
        while process.poll() is None:
            sleep(poll_interval)

    return True


# ---------------------------------------------------------------------------
# result derivation (§3.2: "derive the result from the last result event")
# ---------------------------------------------------------------------------


def parse_result_event(stream_path: Path) -> dict[str, Any] | None:
    """Read ``stream.jsonl`` and return the **last ``"result"`` event in it**.
    ``None`` for a missing/empty file, or a stream that ends mid-run (killed
    before a result event was ever emitted).

    Not "the last event", which is what §3.2 says and what this used to do.
    The result event is not reliably the final line: a run that leaves
    background tasks behind gets a teardown tail after it, as the CLI kills
    them and reports each one. Job 2's real capture (2026-08-28) ends

        240 result                  subtype=success, is_error=false
        241 system background_tasks_changed
        242 system task_updated      status=killed
        243 system task_notification status=stopped
        244 system background_tasks_changed  tasks: []
        245 system task_updated      status=killed
        246 system task_notification status=stopped

    -- six lines of teardown for the two backgrounded builds it had started.
    Reading line 246 and finding no ``"result"`` reported the run as
    ``killed`` (:func:`outcome_from_result`) with ``summary`` "no result
    event", six lines below a ``subtype: "success"`` envelope; it also cost
    the ``conversation_id``, so the mail said there was nothing to resume
    while the session id sat in the stream. The word "killed" in those events
    is about the *tasks*, not the run.

    *Last*, not first: a stream can carry more than one result event (see
    :func:`windowsill.transcript.live_background_tasks` on job 16), and the
    last one is the run's own outcome.
    """
    if not stream_path.exists():
        return None

    result_event: dict[str, Any] | None = None
    for event in transcript.iter_events(stream_path):
        if event.get("type") == "result":
            result_event = event
    return result_event


def outcome_from_result(
    result_event: dict[str, Any] | None,
    *,
    timed_out: bool,
    live_tasks: Sequence[Mapping[str, str]] = (),
) -> str:
    """Derive a ``runs.outcome`` value from a parsed result event.

    ``timed_out`` (our own §3.3 enforcement fired) always wins. Otherwise:
    no result event at all means the process died without ever emitting
    one (crashed, OOM-killed, etc.) -- ``"killed"``. A result event's
    ``is_error`` flag (§3.2) decides ``"done"`` vs. ``"failed"``.
    ``rate_limited`` is not produced here -- that's a later milestone
    (§4/§5 rate-limit handling), deliberately out of scope for this module.

    ``live_tasks`` is :func:`windowsill.transcript.live_background_tasks`'
    verdict: background work the orchestrator dispatched and did not wait for.
    A run that would otherwise read ``done`` but left tasks in flight is
    ``"incomplete"`` instead -- it did not fail, it stopped early, and its own
    closing summary describes work that had not happened yet. Only ``done`` is
    downgraded: a run that genuinely errored is better described as ``failed``.
    """
    if timed_out:
        return "timeout"
    if result_event is None:
        return "killed"
    if result_event.get("is_error"):
        return "failed"
    return "incomplete" if live_tasks else "done"


# ---------------------------------------------------------------------------
# reap: the lightweight pid-liveness + result check (§3.1 step 2, "windowsill tick")
# ---------------------------------------------------------------------------


@dataclass
class ReapStatus:
    alive: bool
    outcome: str | None
    result: dict[str, Any] | None
    #: Background work still in flight when the stream ended (§7 ``blocked``).
    live_tasks: list[dict[str, str]] = field(default_factory=list)


def reap(
    dir_: Path,
    pid: int,
    *,
    alive_check: Callable[[int], bool] = is_alive,
) -> ReapStatus:
    """Check a run's pid liveness and, if it's gone, its result.

    This is the resilience path a later scheduler module uses per-tick:
    it doesn't (can't) ``wait()`` on a pid that isn't its own child, so it
    checks liveness and, on exit, materialises/derives ``result.json`` the
    same way :func:`run_job` does -- covering the case where the detached
    run wrapper itself died uncleanly before finishing that step.

    ``result.json`` is trusted verbatim if already present; otherwise it's
    derived from ``stream.jsonl``'s last event and written out. The live
    background-task check reads ``stream.jsonl`` either way -- ``result.json``
    is one event and cannot answer it (§7 ``blocked``).
    """
    if alive_check(pid):
        return ReapStatus(alive=True, outcome=None, result=None)

    result_path = dir_ / "result.json"
    stream_path = dir_ / "stream.jsonl"
    if result_path.exists():
        result_event: dict[str, Any] | None = json.loads(result_path.read_text())
    else:
        result_event = parse_result_event(stream_path)
        if result_event is not None:
            write_result_json(dir_, result_event)

    live_tasks = list(transcript.live_background_tasks(transcript.iter_events(stream_path))) if stream_path.exists() else []
    outcome = outcome_from_result(result_event, timed_out=False, live_tasks=live_tasks)
    return ReapStatus(alive=False, outcome=outcome, result=result_event, live_tasks=live_tasks)


# ---------------------------------------------------------------------------
# run_job: the detached run wrapper (§3.1, §3.2, §3.3, §6.2, end to end)
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# job.state on completion. Run outcomes (§6 runs.outcome) are a different
# vocabulary from job states (§7): "timeout" and "killed" are run outcomes,
# never job states, so both map to the terminal job state "failed" -- and a
# kill -9 mid-run must leave a *failed* job (§11 acceptance #10), not a
# "killed" one. The "incomplete" run outcome is the one that leaves a job
# needing a human: job state "blocked" (§7).
#
# Public, and imported by windowsill.scheduler for its reboot-safety reap path:
# the two must agree about what an outcome leaves behind, and sharing the dict
# is the only form of "in lockstep" that cannot drift.
JOB_STATE_FOR_OUTCOME = {
    "done": "done",
    "failed": "failed",
    "timeout": "failed",
    "killed": "failed",
    "incomplete": "blocked",
}


#: §5.4's last-resort reset time when neither the rate-limit signal nor a
#: ``/usage`` probe supplies one. Deliberately the config'd window length: it
#: is a guess, and a guess that is too long merely delays, while one that is
#: too short walks straight back into the limit.
_DEFAULT_WINDOW_LENGTH = "5h"

#: §5.4's replacement body for a rate-limit requeue that resumes.
RESUME_ON_LIMIT_BODY = "Continue where you left off."


# ---------------------------------------------------------------------------
# §3.2 the default appended system prompt, and the journal it advertises
# ---------------------------------------------------------------------------

#: The filename windowsill writes/advertises the per-job status journal
#: under, always directly inside the job dir (never the working tree/
#: worktree -- it must not pollute the branch left for morning review).
#: Being keyed on the job id, not the conversation, is what lets it
#: survive a fresh-context re-dispatch.
JOURNAL_FILENAME = "journal.md"

#: Injected whenever ``[prompt].enabled`` (byte-identical every run, no
#: per-job substitution). States the operating rules a job's own file would
#: otherwise have to spell out by hand -- design/02-job-file.md's own
#: example job file already writes "if you hit a blocker ... write what you
#: tried ... to BLOCKED.md" -- and hoists that boilerplate rather than
#: competing with it: same filename, same idea.
SYSTEM_PROMPT_BASE = (
    "You are running unattended under windowsill, a batch queue that runs one "
    "job per session inside a fixed usage window. Nobody is watching this "
    "session and no question you ask will be answered. Never end a turn on a "
    "question. If you are blocked, write what you tried and what you need "
    "into `BLOCKED.md`, then carry on with the next piece of work.\n\n"
    "Context is the budget that binds here, not time and not money. For work "
    "that spans many files, delegate the reading and editing to subagents and "
    "keep this session for planning and results."
)

#: Appended after :data:`SYSTEM_PROMPT_BASE` whenever ``[prompt].journal``.
#: ``{journal}`` is substituted with the job dir's absolute journal path.
#: Explicitly rations updates -- the journal is the *expensive* half of this
#: feature (each write is an Edit/Write call that stays in context for the
#: rest of the run), so the prompt itself is the only thing keeping that
#: cost down.
SYSTEM_PROMPT_JOURNAL = (
    "Keep a status journal at `{journal}`, under a page:\n\n"
    "    Goal  — one line.\n"
    "    Done  — what is finished, and the evidence (file, test, commit).\n"
    "    Next  — what remains, in order.\n"
    "    Notes — decisions and dead ends worth not repeating.\n\n"
    "Write it once early, then update it when you finish a step or change "
    "plan — not more often; every update costs context. You own this "
    "file: never ask a subagent to write it."
)

#: Appended only when windowsill *knows* this run is a fresh-context
#: re-execution of a job that has already tried at least once: a prior run
#: exists in ``runs`` and this dispatch carries no ``resume_id``. That is
#: exactly the ``--retry``/``--no-resume-on-limit`` shape (runner.py's own
#: requeue paths), both of which re-dispatch with a brand-new conversation
#: and the original body, discarding everything the previous attempt did.
#: Never injected on an ordinary first run, and never on a resumed run --
#: a resumed agent already has this history in its own context and doesn't
#: need to be told to go read a file about it.
SYSTEM_PROMPT_REEXEC = (
    "This is attempt {attempt} of this job. The previous attempt ended: "
    "{outcome}. This session has none of its history — read `{journal}` "
    "first and continue from Next. Do not start over."
)


@dataclass(frozen=True)
class SystemPromptPlan:
    """What :func:`resolve_system_prompt` decided to inject for one run.

    ``text`` is the value for ``--append-system-prompt``, or ``None`` when
    nothing should be injected at all (config off, an anchor job, or the
    job's own pass-through already names a system-prompt flag of its own --
    §3.2's yield rule). ``add_dir`` is the job dir to also pass as
    ``--add-dir``, or ``None`` whenever ``text`` doesn't mention the journal
    by path -- kept together with ``text`` so the two can never drift apart
    (a directory grant for a journal the prompt never told the agent about
    would be silently pointless).

    ``yielded`` distinguishes the one reason for ``text is None`` that a
    reader of the morning's event log would want to know about -- the job
    brought its own system-prompt flag -- from the two that are simply
    windowsill having nothing on offer (disabled, or an anchor). Only the
    first is worth an event: §3.3's "silent-but-visible" shape, the same one
    ``allowed_tools_yielded`` already uses. A job that names its own flag
    while ``[prompt].enabled`` is false yielded nothing and reports
    ``False``.
    """

    text: str | None = None
    add_dir: Path | None = None
    yielded: bool = False


def resolve_system_prompt(
    passthrough: Sequence[str],
    *,
    config: Mapping[str, Any] | None,
    job_dir: Path,
    is_anchor: bool,
    prior_runs: Sequence[Mapping[str, Any]],
    resume_id: str | None,
) -> SystemPromptPlan:
    """Resolve §3.2's default appended system prompt for one dispatch.

    Pure: no I/O, no clock, no probing -- every fact it needs (config,
    whether this is an anchor job, the job's prior ``runs`` rows in
    dispatch order, and whether this dispatch carries a ``resume_id``) is
    handed in. ``run_job`` supplies ``prior_runs`` via ``db.list_runs``
    called *before* this dispatch's own run is inserted, so it naturally
    excludes the run currently being resolved.

    Nothing is injected (``SystemPromptPlan()``, both fields ``None``) when:

    - ``[prompt].enabled`` is false,
    - ``is_anchor`` -- injecting a ~250-token prompt and a journal
      instruction into a Haiku "reply with the single word: ok" ping is
      absurd, or
    - the job's own pass-through already names a flag in
      :data:`SYSTEM_PROMPT_FLAGS` (:func:`names_system_prompt_flag`) -- the
      job's own flag always wins, and this is also the feature's free
      per-job opt-out (``#$ --append-system-prompt ""``): no new scheduler
      directive is needed.

    Otherwise the text is either ``[prompt].text`` verbatim (with
    ``"{journal}"`` substituted -- a plain string replace, not
    ``str.format``, since a user's override may itself contain other brace
    characters), or built from :data:`SYSTEM_PROMPT_BASE` plus, when
    ``[prompt].journal``, :data:`SYSTEM_PROMPT_JOURNAL`, plus, when
    ``[prompt].journal`` *and* ``prior_runs`` is non-empty *and*
    ``resume_id`` is ``None``, :data:`SYSTEM_PROMPT_REEXEC` (naming the
    attempt number and the previous attempt's outcome). The re-execution
    line is gated on ``[prompt].journal`` too: it tells the agent to go
    read the journal, which presupposes one is being kept.

    ``add_dir`` is the job dir itself whenever the resolved text is
    non-empty and ``[prompt].journal`` is on (covers both the built-in text
    and an override that mentions ``{journal}``); ``None`` otherwise.
    """
    prompt_config = (config or {}).get("prompt") or {}
    # Order matters only for what gets *reported*: the two "nothing on offer"
    # cases are checked first so that a disabled/anchor job which also happens
    # to name its own flag doesn't log a yield it never made.
    if is_anchor or not prompt_config.get("enabled", True):
        return SystemPromptPlan()
    if names_system_prompt_flag(passthrough):
        return SystemPromptPlan(yielded=True)

    journal_enabled = bool(prompt_config.get("journal", True))
    journal_path = job_dir / JOURNAL_FILENAME
    add_dir = job_dir if journal_enabled else None

    override = prompt_config.get("text") or ""
    if override:
        return SystemPromptPlan(text=override.replace("{journal}", str(journal_path)), add_dir=add_dir)

    parts = [SYSTEM_PROMPT_BASE]
    if journal_enabled:
        parts.append(SYSTEM_PROMPT_JOURNAL.format(journal=journal_path))
        if prior_runs and resume_id is None:
            prior_outcome = prior_runs[-1].get("outcome") or "unknown"
            parts.append(
                SYSTEM_PROMPT_REEXEC.format(
                    attempt=len(prior_runs) + 1,
                    outcome=prior_outcome,
                    journal=journal_path,
                )
            )

    return SystemPromptPlan(text="\n\n".join(parts), add_dir=add_dir)


def read_journal(job_dir: Path) -> str:
    """The job's ``journal.md``, or ``""`` when there isn't one worth reading.

    Used only as the completion email's fallback summary (§8.4): the primary
    is still the run's closing prose, which is addressed to the reader and
    beats a Goal/Done/Next checklist in the 90% case. This covers the case
    where there *is* no closing prose -- a run killed by ``--timeout``
    mid-tool-call, or one that died before its first turn -- which is exactly
    the morning the email is currently worthless and the journal on disk is
    intact. A journal the agent created and never wrote to is not a summary,
    so whitespace counts as absent.
    """
    path = job_dir / JOURNAL_FILENAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text.strip()


@dataclass
class RunOutcome:
    job_id: int
    run_id: int
    pid: int
    exit_code: int | None
    outcome: str
    result: dict[str, Any] | None
    #: Δ window % (§4.3), when both probes succeeded.
    delta_pct: int | None = None
    #: The job state this run left behind: terminal, ``ready`` when the run was
    #: requeued for a retry or a rate limit (§5.4), or ``blocked`` when it ended
    #: with background work still live (§7).
    job_state: str = ""
    #: Background work still in flight when the stream ended (§7 ``blocked``).
    live_tasks: list[dict[str, str]] = field(default_factory=list)


def _default_probe() -> "usage.ParsedUsage":
    """The production ``/usage`` oracle (§4.2).

    Deliberately an indirection rather than ``probe=usage.probe`` as a default
    argument: a default is bound once, at import, so a test replacing
    ``usage.probe`` would never be seen. Looking it up per call keeps the
    "no real ``claude`` in the suite" guard (tests/conftest.py) effective.
    """
    return usage.probe()


def _safe_probe(probe: Callable[[], "usage.ParsedUsage"]) -> "usage.ParsedUsage | None":
    """Probe ``/usage``, or return ``None`` if the probe itself failed.

    The probe is instrumentation (§4.3's Δ%) and a fallback source for §5.4's
    reset time. A `claude` that won't answer a probe must not take the run
    down with it, so every failure mode collapses to "no reading".
    """
    try:
        return probe()
    except Exception:  # noqa: BLE001 -- instrumentation must never fail the run
        return None


def _limit_reset_at(
    signal: "usage.RateLimitSignal",
    probed: "usage.ParsedUsage | None",
    *,
    now_str: str,
    window_length: str,
) -> str:
    """§5.4's reset-time fallback chain, most trustworthy first: a fresh
    ``/usage`` probe, then the error's own timestamp, then ``now`` plus the
    configured window length."""
    if probed is not None and probed.reset_at is not None:
        return probed.reset_at.isoformat()
    if signal.reset_at is not None:
        return signal.reset_at.isoformat()
    seconds = parse_timeout_seconds(window_length) or parse_timeout_seconds(_DEFAULT_WINDOW_LENGTH)
    return (datetime.fromisoformat(now_str.replace("Z", "+00:00")) + timedelta(seconds=seconds)).isoformat()


def _summary(result_event: dict[str, Any] | None) -> str:
    """The one-line result a notification carries (§8.4)."""
    if not result_event:
        return "no result event"
    text = str(result_event.get("result") or result_event.get("subtype") or "")
    first_line = text.strip().splitlines()[0] if text.strip() else str(result_event.get("subtype", ""))
    return first_line[:200]


def _requeue_after_limit(
    database: "db.sqlite_utils.Database",
    job: dict[str, Any],
    signal: "usage.RateLimitSignal",
    probed: "usage.ParsedUsage | None",
    *,
    conversation_id: str | None,
    finished_at: str,
    window_length: str,
) -> str:
    """§5.4: put a rate-limited job back on the queue and block the scheduler.

    The job returns to ``ready`` with its **retry count untouched** -- it did
    not fail, it ran out of window -- and ``sched.blocked_until`` holds the
    whole queue until the window resets, so the next tick doesn't walk the
    next job straight into the same wall.

    Unless ``--no-resume-on-limit``, the requeued job carries the conversation
    it got as far as, with §5.4's continuation body. The original submission
    survives on disk as ``job.md`` (§6.2) either way. Resuming re-pays for the
    whole conversation, so a job resumed across several windows costs more
    than the same work split into jobs; this is a safety net for a job that
    overran, not a plan (README, "Scope a job to finish inside one window").
    """
    blocked_until = _limit_reset_at(signal, probed, now_str=finished_at, window_length=window_length)
    db.update_sched(database, blocked_until=blocked_until, blocked_reason=f"rate limit ({signal.source}: {signal.detail})")

    fields: dict[str, Any] = dict(state="ready", pid=None, conversation_id=conversation_id)
    if conversation_id and not job["no_resume_on_limit"]:
        fields["resume_id"] = conversation_id
        fields["body"] = RESUME_ON_LIMIT_BODY
    db.update_job(database, job["id"], **fields)
    db.log_event(
        database,
        ts=finished_at,
        job_id=job["id"],
        kind="rate_limited",
        detail=f"{signal.source}; blocked until {blocked_until}",
    )
    return "ready"


#: Prefixed to the journal when it stands in for a missing closing message,
#: so nobody reads a Goal/Done/Next checklist as a report addressed to them.
JOURNAL_SUMMARY_NOTE = "*The run left no closing message. This is the journal it kept.*"


def _journal_summary(job_dir: Path) -> str:
    """The journal as an email body, or ``""`` when there is nothing to show."""
    journal = read_journal(job_dir)
    return f"{JOURNAL_SUMMARY_NOTE}\n\n{journal}" if journal else ""


def _attempts(rows: Sequence[Mapping[str, Any]]) -> list["mail.Attempt"]:
    """The job's ``runs`` rows as the mail's attempt history (§8.4).

    Returns nothing at all for a single row: one attempt is not a history, and
    a mail for the ordinary case must look exactly as it always did.

    The Δ is recomputed here rather than stored because the run row keeps the
    two readings it came from, and a stored delta would be a third number to
    keep consistent with them.
    """
    if len(rows) < 2:
        return []
    attempts: list[mail.Attempt] = []
    for number, row in enumerate(rows, start=1):
        before, after = row.get("pct_before"), row.get("pct_after")
        attempts.append(
            mail.Attempt(
                n=number,
                started_at=row.get("started_at"),
                finished_at=row.get("finished_at"),
                outcome=row.get("outcome") or "",
                delta_pct=after - before if before is not None and after is not None else None,
                num_turns=row.get("num_turns"),
                duration_ms=row.get("duration_ms"),
                summary=row.get("summary") or "",
            )
        )
    return attempts


def _usage_rows(
    database: "db.sqlite_utils.Database",
    reading: "usage.ParsedUsage | None",
    *,
    at: str,
    config: Mapping[str, Any] | None,
) -> list[tuple[str, str]]:
    """The §4 window block both notifications carry, so they cannot disagree."""
    return mail.usage_rows(
        reading,
        windows_in_30d=window.windows_in_last_30d(database, datetime.fromisoformat(at.replace("Z", "+00:00"))),
        max_sessions=(config or {}).get("queue", {}).get("max_sessions_per_month"),
    )


def _run_facts(
    database: "db.sqlite_utils.Database",
    job: dict[str, Any],
    job_state: str,
    *,
    outcome: str,
    result_event: dict[str, Any] | None,
    events: Sequence[dict[str, Any]],
    live_tasks: Sequence[Mapping[str, str]],
    delta_pct: int | None,
    permission_denials: int | None,
    conversation_id: str | None,
    reading: "usage.ParsedUsage | None",
    finished_at: str,
    config: Mapping[str, Any] | None,
    job_dir: Path,
) -> "mail.RunFacts":
    """Assemble what the completion email reports (§8.4).

    Everything here is already in hand or one cheap read away: the ``events``
    the rate-limit check just walked (so the final summary costs no second
    parse of ``stream.jsonl``), the post-run ``/usage`` reading (§4.3), the
    ``sched`` block gate (§5.4), and the §5.3 ledger count.

    ``summary`` is the run's closing prose, falling back to the journal
    (:func:`read_journal`) only when there is none -- see that function for
    why the fallback is one-way. ``RunFacts`` gains no field for it: the
    journal is Markdown and the summary is Markdown, so ``mail`` renders it
    unchanged.

    ``attempts`` is the one thing here that is not about *this* run: a job that
    was rate-limited twice finished on its third attempt, and a mail describing
    only the third describes three minutes of a ten-hour night (§8.4).

    The ``sched`` hold is passed on only while it is *live*. ``blocked_until``
    is never cleared when it expires -- the scheduler simply stops honouring
    it (``blocked_until > now``, §3.1) -- so the row outlives the hold by
    however long it has been since the last rate limit. ``mail`` reads no
    clock by contract, and cannot tell the difference: job 2's mail announced
    "the queue is blocked until 2026-08-21 05:00" on the 28th, quoting a hold
    that had lapsed a week earlier. ``finished_at`` is the comparison, not a
    fresh clock reading -- it is the instant this mail describes, and every
    other time in it is keyed to the same one.
    """
    sched = db.get_sched(database)
    blocked_until = sched.get("blocked_until")
    live_block = blocked_until is not None and blocked_until > finished_at
    return mail.RunFacts(
        job_id=job["id"],
        name=job["name"],
        job_state=job_state,
        outcome=outcome,
        summary=transcript.final_summary(events) or _journal_summary(job_dir),
        result=result_event,
        live_tasks=[dict(task) for task in live_tasks],
        delta_pct=delta_pct,
        permission_denials=permission_denials,
        blocked_until=blocked_until if live_block else None,
        blocked_reason=sched.get("blocked_reason") if live_block else None,
        has_conversation=bool(conversation_id),
        usage=_usage_rows(database, reading, at=finished_at, config=config),
        # Read after this run's own row has been updated, so the history the
        # mail prints ends with the run the mail is about.
        attempts=_attempts(db.list_runs(database, job_id=job["id"])),
    )


def _compose_email(facts: "mail.RunFacts", channel: str | None) -> "EmailMessage | None":
    """The ``multipart/alternative`` completion mail, for the email channel only.

    ``None`` for every other channel (ntfy sends the one-liner, "none" sends
    nothing), and ``None`` again if composing it raises: a body that would not
    render must not take down a run that already happened, which is the same
    rule §8.4 sets for the send itself. The plain title/body then goes instead.
    """
    if notify.split_channel(channel)[0] != "email":
        return None
    try:
        return mail.build_message(facts)
    except Exception:  # noqa: BLE001 -- a notification must never fail the job (§8.4)
        return None


def _notify_job(
    database: "db.sqlite_utils.Database",
    job: dict[str, Any],
    facts: "mail.RunFacts",
    *,
    ts: str,
    config: Mapping[str, Any] | None,
    notify_fn: Callable[..., "notify.NotifyResult"],
) -> None:
    """Send the job's completion notification and record the attempt (§8.4).

    Only terminal states send *this* mail -- a requeued job hasn't finished,
    and the most a rate-limited pause earns is :func:`_notify_rate_limit`'s
    one-liner. The result is written to ``events`` whether it succeeded or not:
    §8.4's contract is that a failed notification is *logged* and never fails
    the job, and :func:`windowsill.notify.notify` already guarantees the second
    half.

    A channel that was on but had nowhere to send to is neither a send nor a
    failure, so it gets its own ``notify_skipped`` event: silently dropping the
    notification a job asked for is exactly the kind of thing you only discover
    the morning you needed it.
    """
    title, body = notify.compose(
        job_id=facts.job_id,
        name=facts.name,
        state=facts.job_state,
        summary=_summary(facts.result),
        delta_pct=facts.delta_pct,
        permission_denials=facts.permission_denials,
        live_tasks=len(facts.live_tasks),
    )
    outcome = notify_fn(job["notify"], title, body, config=config or {}, message=_compose_email(facts, job["notify"]))
    _record_notification(database, job["id"], outcome, ts=ts)


def _record_notification(
    database: "db.sqlite_utils.Database",
    job_id: int,
    outcome: "notify.NotifyResult",
    *,
    ts: str,
    what: str = "",
) -> None:
    """Write one notification attempt to ``events`` (§8.4).

    Three kinds and no more, so anything reading the audit trail keeps reading
    it: which notification it was goes in ``what``, not in a fourth kind.
    """
    if outcome.channel == "none":
        return
    if outcome.skipped:
        kind = "notify_skipped"
    else:
        kind = "notified" if outcome.ok else "notify_failed"
    detail = f"{outcome.channel}{'' if outcome.ok else ': ' + outcome.detail}"
    db.log_event(database, ts=ts, job_id=job_id, kind=kind, detail=f"{what}{detail}" if what else detail)


def _notify_rate_limit(
    database: "db.sqlite_utils.Database",
    job: dict[str, Any],
    *,
    ts: str,
    blocked_until: str | None,
    reading: "usage.ParsedUsage | None",
    config: Mapping[str, Any] | None,
    notify_fn: Callable[..., "notify.NotifyResult"],
) -> None:
    """The ``[notify].on_rate_limit`` ping: "still alive, back at 02:00" (§8.4).

    Off unless asked for, and this function is where "off" has to be genuinely
    silent: a job with ``--retry 3`` that keeps hitting the ceiling would send
    one of these per attempt, which is why the default is ``False`` (see
    :mod:`windowsill.config`).

    It changes nothing about the requeue. The job is still not terminal, still
    gets no summary mail, and still keeps its retry count -- this only breaks
    the silence between the run that stopped at 21:21 and the one that finishes
    at 07:03, because from the outside that silence is indistinguishable from a
    tick that died.
    """
    if not (config or {}).get("notify", {}).get("on_rate_limit"):
        return
    channel = job.get("notify")
    name = notify.split_channel(channel)[0]
    if name == "none":
        return
    title, body = mail.rate_limit_notice(
        job_id=job["id"],
        name=job["name"],
        blocked_until=blocked_until,
        # §2.3: the rate limit itself consumed none of these, which is the
        # point the mail makes rather than leaving the reader to subtract.
        retries_left=max(0, (job["retry_max"] or 0) - (job["retry_count"] or 0)),
        usage=_usage_rows(database, reading, at=ts, config=config),
    )
    message = mail.build_rate_limit_message(job["id"], title, body) if name == "email" else None
    outcome = notify_fn(channel, title, body, config=config or {}, message=message)
    _record_notification(database, job["id"], outcome, ts=ts, what="rate limit: ")


def run_job(
    database: "db.sqlite_utils.Database",
    job_id: int,
    *,
    root: Path,
    on_complete: Sequence[str],
    claude_cmd: Sequence[str] = ("claude",),
    env: dict[str, str] | None = None,
    grace_seconds: float = 30.0,
    now: Callable[[], str] = _iso_now,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.05,
    config: Mapping[str, Any] | None = None,
    probe: Callable[[], "usage.ParsedUsage"] = _default_probe,
    notify_fn: Callable[..., "notify.NotifyResult"] = notify.notify,
) -> RunOutcome:
    """Run one already-selected job to completion: build its invocation,
    spawn it detached under ``root``'s job directory, block until it
    exits (this wrapper is the child's real parent, so a genuine
    ``wait()``-style block is correct here -- unlike ``windowsill tick``, which
    never blocks on a running job per §3.1), enforce its ``--timeout``,
    persist the outcome to ``runs``/``jobs``/``events``, and finally hand
    off to ``on_complete`` detached (Popen-and-exit, §3.1: "never
    recursive in-process, or a fast-failing job becomes a fork bomb").

    Every non-DB dependency (which command to exec, how to spawn it, the
    clock, the on-complete command) is an injectable keyword argument;
    ``database`` is a plain :mod:`windowsill.db`-opened handle so tests point it
    at a ``tmp_path`` DB, never the real ``~/.windowsill/windowsill.db``.
    """
    job = db.get_job(database, job_id)
    if job is None:
        raise ValueError(f"no job with id {job_id}")

    dir_ = job_dir(root, job_id, job["name"])
    dir_.mkdir(parents=True, exist_ok=True)

    # §3.3: safety.allowed_tools is a default, not a policy override -- a job
    # naming its own tool-permission flag in its pass-through directives wins,
    # and build_argv silently omits the injection in that case. Log the yield
    # here (§3.3 "silent-but-visible") so the run's own record shows why no
    # allowlist went in, using the same idiom as every other run event.
    allowed_tools = list((config or {}).get("safety", {}).get("allowed_tools") or [])

    # §3.2: the same default/yield shape for the appended system prompt. The
    # prior runs are read *before* this dispatch's own run row is inserted
    # below, so `prior_runs` is genuinely "attempts that already happened" --
    # which is what decides whether this is a fresh-context re-execution
    # (`--retry`/`--no-resume-on-limit`: a new conversation and the original
    # body) and therefore whether the agent is told to go read the journal.
    prompt_plan = resolve_system_prompt(
        job["passthrough"],
        config=config,
        job_dir=dir_,
        is_anchor=bool(job.get("is_anchor")),
        prior_runs=db.list_runs(database, job_id=job_id),
        resume_id=job.get("resume_id"),
    )

    argv = build_argv(
        job["passthrough"],
        resume=job.get("resume_id"),
        claude_cmd=claude_cmd,
        allowed_tools=allowed_tools,
        system_prompt=prompt_plan.text,
        add_dirs=[str(prompt_plan.add_dir)] if prompt_plan.add_dir else (),
    )
    write_cmd_json(dir_, argv=argv, cwd=job["cwd"], env=env)

    started_at = now()
    if allowed_tools and names_tool_permission_flag(job["passthrough"]):
        db.log_event(
            database,
            ts=started_at,
            job_id=job_id,
            kind="allowed_tools_yielded",
            detail="pass-through already names a tool-permission flag; safety.allowed_tools not injected",
        )
    if prompt_plan.yielded:
        db.log_event(
            database,
            ts=started_at,
            job_id=job_id,
            kind="system_prompt_yielded",
            detail="pass-through already names a system-prompt flag; windowsill's prompt and journal not injected",
        )

    # §4.3: probe immediately before and after the run; the delta is what the
    # job cost in the only unit that binds. The "before" reading doubles as the
    # first read of a window this job's dispatch just opened, so it completes
    # that ledger row's reset_at (§5.3). Approximate: another surface can move
    # the number during the run -- acceptable for a single user who is mostly
    # asleep.
    before = _safe_probe(probe)
    if before is not None:
        window.record_probe(database, before, now_str=started_at, opened_by=job_id)

    run_id = db.insert_run(database, job_id=job_id, started_at=started_at, pct_before=before.pct_used if before else None)
    db.log_event(database, ts=started_at, job_id=job_id, kind="dispatched", detail=f"run_id={run_id}")

    process = spawn(argv, job["body"], cwd=job["cwd"], dir_=dir_, env=env, popen=popen)
    db.update_job(database, job_id, state="running", started_at=started_at, pid=process.pid)

    timeout_seconds = parse_timeout_seconds(job.get("timeout"))
    timed_out = wait_with_timeout(
        process,
        timeout_seconds=timeout_seconds,
        grace_seconds=grace_seconds,
        clock=clock,
        sleep=sleep,
        poll_interval=poll_interval,
    )

    result_event = parse_result_event(dir_ / "stream.jsonl")
    if result_event is not None:
        write_result_json(dir_, result_event)

    # Render the human-readable transcript (§6.2) from whatever the run left
    # behind -- even a killed/partial stream is worth reading back -- and copy
    # it to the job's --log destination if it named one (§2.3).
    transcript.write_transcript(
        dir_,
        prompt=job["body"],
        extra_path=log_destination(job.get("log_path"), job_id=job_id, name=job["name"], cwd=job["cwd"]),
    )

    # §5.4: "limit reached" is a schedulable event, not a failure, so it is
    # decided from the stream before the ordinary outcome mapping.
    events = list(transcript.iter_events(dir_ / "stream.jsonl")) if (dir_ / "stream.jsonl").exists() else []
    limit = usage.detect_rate_limit(events)

    # §7 "blocked": the main thread ended while work it dispatched was still in
    # flight. Nothing in the result envelope says so -- only the `system` task
    # events do.
    live_tasks = transcript.live_background_tasks(events)

    outcome = "rate_limited" if limit else outcome_from_result(result_event, timed_out=timed_out, live_tasks=live_tasks)
    finished_at = now()
    conversation_id = result_event.get("session_id") if result_event else None

    # design/09-findings.md, "Settled 2026-07-28, second sitting": a denied
    # permission does not fail the run -- the envelope can read subtype
    # "success", is_error False, with refusals only visible in this list. Count
    # them per run so a job that wrote nothing doesn't just read "done".
    permission_denials = len(result_event.get("permission_denials") or []) if result_event is not None else None

    after = _safe_probe(probe)
    if after is not None:
        window.record_probe(database, after, now_str=finished_at)
    pct_before = before.pct_used if before else None
    pct_after = after.pct_used if after else None
    delta_pct = pct_after - pct_before if pct_before is not None and pct_after is not None else None

    db.update_run(
        database,
        run_id,
        finished_at=finished_at,
        outcome=outcome,
        pct_after=pct_after,
        num_turns=(result_event.get("num_turns") if result_event else None),
        duration_ms=(result_event.get("duration_ms") if result_event else None),
        permission_denials=permission_denials,
        conversation_id=conversation_id,
        # Every path writes this, not just the terminal one. The job dir holds
        # exactly one stream.jsonl and the next attempt overwrites it, so a
        # rate-limited run's last words survive only if they are copied out
        # now -- and those are precisely the ones the multi-run summary mail
        # goes looking for hours later (§8.4).
        summary=_summary(result_event),
    )

    if conversation_id:
        write_resume_sh(dir_, cwd=job["cwd"], claude_cmd=claude_cmd, conversation_id=conversation_id)

    if limit is not None:
        job_state = _requeue_after_limit(
            database,
            job,
            limit,
            after,
            conversation_id=conversation_id,
            finished_at=finished_at,
            window_length=str((config or {}).get("window", {}).get("length", _DEFAULT_WINDOW_LENGTH)),
        )
        _notify_rate_limit(
            database,
            job,
            ts=finished_at,
            # The queue's own answer, not the signal's: _requeue_after_limit
            # may have preferred a fresh probe's reset time over the error's,
            # and the ping must name the time the scheduler will actually wake.
            blocked_until=db.get_sched(database).get("blocked_until"),
            reading=after,
            config=config,
            notify_fn=notify_fn,
        )
    elif outcome not in ("done", "incomplete") and job["retry_count"] < job["retry_max"]:
        # §2.3: retries are for ordinary failure. A rate limit never consumes
        # one -- it didn't fail, it ran out of window. Nor does "incomplete":
        # re-running the body from the top would start a second copy of work
        # whose first copy may still be live, and the job needs a human
        # anyway (§7 "blocked"), which is what a retry would paper over.
        job_state = "ready"
        db.update_job(
            database,
            job_id,
            state=job_state,
            retry_count=job["retry_count"] + 1,
            pid=None,
            exit_code=process.returncode,
            conversation_id=conversation_id,
        )
        db.log_event(
            database,
            ts=finished_at,
            job_id=job_id,
            kind="retry",
            detail=f"{outcome}; attempt {job['retry_count'] + 1} of {job['retry_max']}",
        )
    else:
        job_state = JOB_STATE_FOR_OUTCOME.get(outcome, "failed")
        db.update_job(
            database,
            job_id,
            state=job_state,
            finished_at=finished_at,
            exit_code=process.returncode,
            conversation_id=conversation_id,
        )
        db.log_event(database, ts=finished_at, job_id=job_id, kind=job_state, detail=outcome)
        _notify_job(
            database,
            job,
            _run_facts(
                database,
                job,
                job_state,
                outcome=outcome,
                result_event=result_event,
                events=events,
                live_tasks=live_tasks,
                delta_pct=delta_pct,
                permission_denials=permission_denials,
                conversation_id=conversation_id,
                reading=after,
                finished_at=finished_at,
                config=config,
                job_dir=dir_,
            ),
            ts=finished_at,
            config=config,
            notify_fn=notify_fn,
        )

    # Chain dispatch (§3.1): hand off to the next tick detached, never a
    # recursive in-process call, regardless of this run's outcome.
    popen(list(on_complete), start_new_session=True)

    return RunOutcome(
        job_id=job_id,
        run_id=run_id,
        pid=process.pid,
        exit_code=process.returncode,
        outcome=outcome,
        result=result_event,
        delta_pct=delta_pct,
        job_state=job_state,
        live_tasks=live_tasks,
    )
