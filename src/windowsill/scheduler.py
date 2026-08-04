"""windowsill dispatch scheduler: the ``windowsill tick`` step (design/03-execution-model.md §3.1)
and the §7 job-lifecycle state machine.

``tick`` is chain-driven and cron-floored (§3.1). One tick:

1. acquires the dispatch lock (:func:`windowsill.db.dispatch_lock`, a
   ``BEGIN IMMEDIATE``);
2. **reaps** ``running`` jobs whose process died *without recording a
   terminal state* -- the :func:`windowsill.runner.reap` safety net for a machine
   reboot or a run wrapper that was itself killed;
3. **evaluates** the queue: expire past-deadline jobs, then promote due
   ``queued`` jobs whose ``--hold-jid`` / ``--resume-from`` dependencies
   are satisfied to ``ready`` (§7);
4. **dispatches** up to ``max_parallel`` ready jobs, lowest ``--priority``
   first, subject to each job's ``--window`` policy (§5.1, decided from at
   most one ``/usage`` probe per tick), the anchor rule (§5.2), the 30d
   ceiling (§5.3) and -- for a ``--warm`` job only -- the warming gate
   (§5.5, :func:`warm_gate`); and
5. commits and exits, **never blocking on a running job**.

``tick`` must never call :func:`windowsill.runner.run_job` inline -- ``run_job``
*blocks* (it spawns the ``claude`` child and waits out its ``--timeout``).
Instead each dispatch is handed to a *detached* process via the injectable
``spawn_run(job_id)``.

Dispatch is **claim-then-spawn**:

* Inside the lock a to-be-dispatched job is CLAIMED by marking it
  ``running``. A racing tick that blocked on ``BEGIN IMMEDIATE`` then
  acquires the lock, sees the committed claim, counts it toward
  ``max_parallel``, and no-ops. This is how concurrent ticks serialise
  (§3.1) without a fast-failing job fork-bombing through recursive
  in-process ticks. ``run_job`` re-asserting ``running`` is idempotent.
* The detached run is spawned **after** the lock commits, so the run's own
  DB writes never contend with the tick's transaction.

Chain seam (the next module, ``cli.py``, owns this): ``spawn_run``'s
production default Popens ``windowsill run <id>`` (:data:`RUN_ARGV`) detached in
its own session. That ``windowsill run`` subcommand must call
``runner.run_job(..., on_complete=<the ``windowsill tick`` argv>)``
(:data:`TICK_ARGV`) so a finished run chain-dispatches the next tick
(§3.1). It is kept fully behind the ``spawn_run`` injectable so the test
suite spawns no process.

Everything a test must fake -- ``now``, ``spawn_run``, ``alive_check``,
``config`` -- is an injectable keyword with a real default, so the suite
reads no real clock and touches no real ``~/.windowsill``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import sqlite_utils

from windowsill import config as config_mod
from windowsill import db, runner, transcript, usage, window

# ---------------------------------------------------------------------------
# the `windowsill run` / `windowsill tick` entrypoint contract (cli.py implements these)
# ---------------------------------------------------------------------------

#: argv prefix of the detached per-job run entrypoint. ``spawn_run``'s
#: default Popens ``[*RUN_ARGV, str(job_id)]`` -- i.e. ``windowsill run <id>`` --
#: which cli.py must wire to :func:`windowsill.runner.run_job`.
RUN_ARGV: tuple[str, ...] = ("windowsill", "run")

#: argv of the next-tick chain command. ``windowsill run <id>`` must pass this to
#: ``run_job(on_complete=...)`` so a finished run kicks the next tick (§3.1).
TICK_ARGV: tuple[str, ...] = ("windowsill", "tick")


def _with_home(argv: tuple[str, ...], home: Path | None) -> list[str]:
    """``windowsill --home <path> <subcommand>``, or plain ``windowsill <subcommand>``.

    ``--home`` has to survive every hop of the chain. It doesn't just select a
    directory: a tick that dispatches against one home and a run that wakes up
    against another are operating on *different queues*, and the one the run
    picks is the real ``~/.windowsill``. That mis-hop dispatches whichever job holds
    the same id there.
    """
    head, *tail = argv
    prefix = [head] if home is None else [head, "--home", str(home)]
    return [*prefix, *tail]


def run_argv(job_id: int, *, home: Path | None = None) -> list[str]:
    """The argv the production ``spawn_run`` Popens: ``windowsill run <id>``."""
    return [*_with_home(RUN_ARGV, home), str(job_id)]


def tick_argv(*, home: Path | None = None) -> list[str]:
    """The argv of the chained next tick, handed to ``run_job(on_complete=)``."""
    return _with_home(TICK_ARGV, home)


def _default_spawn_run(
    job_id: int,
    *,
    home: Path | None = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> None:
    """Production ``spawn_run``: Popen ``windowsill run <id>`` detached, own session.

    Popen-and-exit only (§3.1): the tick never awaits it, so a fast-failing
    run cannot fork-bomb. Kept behind the ``spawn_run`` injectable so tests
    never actually spawn it.
    """
    popen(
        run_argv(job_id, home=home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_now(now: "str | Callable[[], str]") -> str:
    return now() if callable(now) else now


def _setting(config: Mapping[str, Any] | None, table: str, key: str) -> Any:
    """One ``[table].key`` from the config, falling back to :data:`windowsill.config.DEFAULTS`.

    ``tick``'s ``config`` is optional and may be a bare dict in tests, so every
    read goes through here rather than assuming a fully-populated mapping.
    """
    section = (config or {}).get(table, {})
    if key in section:
        return section[key]
    return config_mod.DEFAULTS[table][key]


def _max_parallel(config: Mapping[str, Any] | None) -> int:
    """``[queue].max_parallel`` (§8.3), defaulting to 1 (§3.1)."""
    return int(_setting(config, "queue", "max_parallel"))


def _parse_dt(value: str) -> datetime:
    """A stored ISO-8601 timestamp as a UTC-aware datetime.

    Everything in the DB is written UTC-aware (see :mod:`windowsill.cli`), but a
    hand-inserted or legacy naïve value must not silently become local time
    in a window-policy comparison, so it is read as UTC.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# dependencies (§2.3 --hold-jid / --resume-from, §7 "deps met")
# ---------------------------------------------------------------------------


def dependency_status(database: sqlite_utils.Database, job: Mapping[str, Any]) -> tuple[str, str]:
    """Evaluate one job's ``--hold-jid`` / ``--resume-from`` dependencies.

    Returns ``("ready", "")`` when everything it waits on has reached ``done``
    (§2.3), ``("wait", why)`` while one is still in flight, and
    ``("dead", why)`` when one reached a terminal state that is *not* ``done``
    -- a dependent whose dependency failed can never run, so leaving it queued
    forever would be a job that silently never happens.

    ``--resume-from`` is a dependency as well as a resume source: there is no
    conversation to continue until the job that produces it has finished.
    """
    targets = [*(job["hold_jid"] or [])]
    if job["resume_from"]:
        targets.append(job["resume_from"])

    for target in targets:
        try:
            target_id = db.resolve_identifier(database, target, include_terminal=True)
        except db.IdentifierError as exc:
            return "dead", str(exc)
        dependency = db.get_job(database, target_id)
        if dependency["state"] == "done":
            continue
        if dependency["state"] in db.TERMINAL_STATES:
            return "dead", f"{target} is {dependency['state']}"
        return "wait", f"{target} is {dependency['state']}"
    return "ready", ""


def resolve_resume(database: sqlite_utils.Database, job: Mapping[str, Any]) -> str | None:
    """The conversation id this job should be dispatched with, if any (§2.3).

    An explicit ``--resume`` wins outright. ``--resume-from`` is resolved
    *here*, at dispatch, because the conversation it names does not exist
    until the job that produces it has run. A dependency that finished
    without ever emitting one (a job whose ``claude`` died before the result
    event) yields ``None``: dispatch the body as written rather than refuse
    to run.
    """
    if job["resume_id"]:
        return job["resume_id"]
    if not job["resume_from"]:
        return None
    try:
        target_id = db.resolve_identifier(database, job["resume_from"], include_terminal=True)
    except db.IdentifierError:
        return None
    dependency = db.get_job(database, target_id)
    return dependency["conversation_id"] if dependency else None


# ---------------------------------------------------------------------------
# tick result (what the tick decided -- for the caller, tests, and the log)
# ---------------------------------------------------------------------------


@dataclass
class TickResult:
    """The dispositions a single :func:`tick` made, in evaluation order."""

    reaped: list[int] = field(default_factory=list)
    expired: list[int] = field(default_factory=list)
    promoted: list[int] = field(default_factory=list)
    dispatched: list[int] = field(default_factory=list)
    #: Jobs this tick took out of the queue for good: an anchor that found a
    #: window already open (§5.2), or a job whose dependency can never finish.
    skipped: list[int] = field(default_factory=list)
    #: Ready jobs a ``--window`` policy held back (§5.1). Not an error and not
    #: terminal -- a later tick asks again.
    deferred: list[int] = field(default_factory=list)
    #: Whether this tick spent a ``/usage`` probe (§4.2: only when a dispatch
    #: decision actually depended on window state).
    probed: bool = False
    #: Things worth saying out loud but never worth blocking on -- currently
    #: the §5.3 ceiling warning on a real job.
    warnings: list[str] = field(default_factory=list)


def _skip(
    database: sqlite_utils.Database,
    result: TickResult,
    job: Mapping[str, Any],
    now_str: str,
    reason: str,
) -> None:
    """Take a job out of the queue as ``skipped`` (§7), recording why."""
    db.update_job(database, job["id"], state="skipped", finished_at=now_str)
    db.log_event(database, ts=now_str, job_id=job["id"], kind="skipped", detail=reason)
    result.skipped.append(job["id"])


# ---------------------------------------------------------------------------
# window warming (§5.5)
# ---------------------------------------------------------------------------
#
# A subscription window is five hours long and starts at your *first*
# request, so a job dispatched at 22:00 owns 22:00-03:00 and spends one
# window on it. Warming splits that in two: a throwaway prompt opens the
# window at 22:00, the real job sits on its hands until shortly before the
# reset, and the run then spends the tail of window one, crosses the reset,
# and rolls straight into window two -- close to two windows of quota
# back-to-back for one job. `[warm].lead` is how much of the tail to claim
# (see the comment on it in `windowsill.config`: it is really "how much
# quota to burn first", expressed as time).
#
# Three properties this implementation is built around, in the order they
# cost you if they are wrong:
#
# 1. **The warmer is an ordinary anchor job.** Nothing here dispatches
#    anything. Warming inserts a `jobs` row with `is_anchor=1` and leaves
#    it to the §5.2 anchor path in `tick` to run -- which already probes,
#    already no-ops if a window turned out to be open after all, already
#    refuses to fire over the 30d ceiling, and already writes the §5.3
#    ledger row. A second dispatch path here would be a second copy of all
#    four of those rules, and the copy is the one that would rot. The cost
#    of that choice is that the warmer starts on the *next* tick rather
#    than this one -- this tick's candidate list was fixed before the
#    warmer existed -- so the window opens up to one cron floor (15
#    minutes, per the README's crontab line) after the parent's `--at`.
#    That delay slides the whole schedule later by the same amount and
#    spends nothing: the tail is still a tail, and the reset still lands
#    where the job is waiting for it.
# 2. **The end of the window is `reset_at` from `/usage`, never
#    `opened_at + [window].length`.** The ledger's `opened_at` is *our
#    dispatch*, which is not when the window began if anything else touched
#    the account first, and the "5h" in config is a guess about Anthropic's
#    policy rather than a reading of it. Aiming the tail at a derived time
#    that is even ten minutes late means the job starts after the reset and
#    the whole exercise has bought nothing.
# 3. **Every warming decision that is not "go" is a defer.** A warm job
#    that finds no room left in the window it warmed has not failed and
#    must not be skipped: the right answer is to wait for a window with
#    room in it, which is exactly what leaving it `ready` does.

#: The ``--timeout`` given to a spawned warmer. It exists to say one word,
#: and it holds a `max_parallel` slot -- the very slot its parent is waiting
#: for -- while it does. Five minutes is far past "the API is answering" and
#: far short of a stuck child sitting on the queue until morning. Not
#: configurable on purpose: nothing about a warmer is worth tuning, and the
#: knob that matters (`[warm].lead`) already exists.
WARMER_TIMEOUT = "5m"

#: Terminal warmer states from which spawning a *replacement* warmer is the
#: right move. The distinction is a runaway guard, not fussiness. `tick` is
#: chain-driven as well as cron-driven, so "the parent is still ready and
#: no window is open" recurs within seconds; a rule of "no live warmer ⇒
#: spawn one" turns a warmer that fails immediately (a broken `claude`, a
#: revoked login) into a loop that submits and burns a job per tick.
#: `done` means the ping worked and the window it opened has since closed --
#: warming again is precisely what is wanted. `skipped` means the anchor
#: path declined it for a reason that has since changed (it found a window
#: open; that window is now closed). `failed`, `expired` and `cancelled`
#: are all "this did not work / a human said no", and repeating them faster
#: than a human can read the log is the failure mode, so those stop.
REWARMABLE_WARMER_STATES: frozenset[str] = frozenset({"done", "skipped"})


def warmer_name(parent_name: str) -> str:
    """The derived name of the warmer spawned for ``parent_name``.

    Derived rather than fixed so `windowsill stat` answers "what is this
    ``warm-nightly-audit`` row" without a lookup, and so two warm jobs
    queued at once do not both produce a job called ``warmer``, which §6.1
    name resolution would then refuse to resolve as ambiguous.
    """
    return f"warm-{parent_name}"


def warm_lead_seconds(job: Mapping[str, Any], config: Mapping[str, Any] | None) -> float:
    """How far before ``reset_at`` this job may go, in seconds.

    ``--warm-lead`` on the job wins over ``[warm].lead``; both are ordinary
    duration strings, parsed by the one parser this codebase has
    (:func:`windowsill.runner.parse_timeout_seconds`, which also reads
    ``--timeout`` and ``[window].length``).

    An unparseable value falls through to the next source rather than
    raising. This is called inside the dispatch lock: a ``ValueError`` here
    would roll back the whole tick, so one job file with ``--warm-lead
    thirty minutes`` in it would stop *every* job in the queue from
    dispatching, on every tick, until someone noticed.
    """
    sources = (job.get("warm_lead"), _setting(config, "warm", "lead"), config_mod.DEFAULTS["warm"]["lead"])
    for candidate in sources:
        if not candidate:
            continue
        try:
            seconds = runner.parse_timeout_seconds(str(candidate))
        except ValueError:
            continue
        if seconds is not None:
            return seconds
    return 0.0


def _log_warm(database: sqlite_utils.Database, job_id: int, now_str: str, detail: str) -> None:
    """Record a warming decision, unless it is the one already recorded.

    A warm job waits out the *body* of a window on purpose -- four and a
    half hours of it, with the default lead -- and `tick` runs on a cron
    floor of a minute plus every time a run finishes. Logging "still
    waiting for the tail" on each of those turns the one event a reader
    actually wants (the decision changing) into a haystack. The dedupe is
    on the exact text, and the text names the *time* being waited for, so a
    new window -- a different tail -- says so again immediately.
    """
    previous = db.list_events(database, job_id=job_id)
    if previous and previous[-1]["kind"] == "warm" and previous[-1]["detail"] == detail:
        return
    db.log_event(database, ts=now_str, job_id=job_id, kind="warm", detail=detail)


def _warm_defer(
    database: sqlite_utils.Database,
    result: TickResult,
    job: Mapping[str, Any],
    now_str: str,
    reason: str,
) -> None:
    """Hold a warm job back for this tick, saying why. Never terminal (§5.5)."""
    _log_warm(database, job["id"], now_str, reason)
    result.deferred.append(job["id"])


def insert_warmer(
    database: sqlite_utils.Database,
    job: Mapping[str, Any],
    *,
    now_str: str,
    config: Mapping[str, Any] | None,
) -> int:
    """Insert the anchor job whose dispatch will open the window ``job`` waits for.

    ``state="ready"`` because there is nothing left to wait for: the parent's
    ``--at`` has already arrived (that is what put it in the candidate list),
    and a warmer that had to be promoted first would lose a tick doing it.

    ``[anchor].body`` and ``[anchor].model`` are the prompt and the model,
    per §5.2 -- the cheapest thing that counts as a request, on a model that
    touches neither weekly bucket. The model is written into **both** the
    ``model`` column and the pass-through argv, because those two are not
    the same thing: the column is display-only (§6, "mirrored from
    pass-through"), and only the ``--model`` in the argv actually reaches
    ``claude``. Writing just the column would warm the window on Opus and
    charge the parent's own quota for the privilege.

    The parent's ``cwd`` (so the ping runs somewhere that exists, and its
    job dir sits beside work it belongs to) and the parent's ``--priority``
    (the warmer is standing in for the parent, so it should queue where the
    parent queues -- at ``max_parallel`` 1 an unrelated ordinary job would
    otherwise take the slot every time and the warmer would never fire).
    """
    model = _setting(config, "anchor", "model")
    return db.insert_job(
        database,
        name=warmer_name(job["name"]),
        state="ready",
        submitted_at=now_str,
        cwd=job["cwd"],
        body=str(_setting(config, "anchor", "body")),
        passthrough=["--model", str(model)] if model else [],
        model=str(model) if model else None,
        is_anchor=True,
        priority=job["priority"],
        timeout=WARMER_TIMEOUT,
    )


def _ensure_warmer(
    database: sqlite_utils.Database,
    result: TickResult,
    job: Mapping[str, Any],
    *,
    now_str: str,
    config: Mapping[str, Any] | None,
    at_ceiling: bool,
    max_sessions: int,
) -> None:
    """No window is open, so the warm job needs one opened *for* it.

    Either spawn the warmer or explain why not; either way the parent is
    deferred, because dispatching it now is the thing warming exists to
    prevent -- it would open the window itself and own exactly one.
    """
    existing_id = job.get("warmed_by")
    existing = db.get_job(database, existing_id) if existing_id else None

    if existing is not None and existing["state"] not in db.TERMINAL_STATES:
        # The one already in flight has not opened a window yet -- it may not
        # even have been dispatched (it is queued behind this very job in the
        # same candidate list). Spawning another would double the anchors,
        # and §5.2 wants at most one.
        _warm_defer(database, result, job, now_str, f"warmer job {existing_id} is {existing['state']}; waiting for it to open a window")
        return

    if existing is not None and existing["state"] not in REWARMABLE_WARMER_STATES:
        reason = f"warmer job {existing_id} is {existing['state']}; not spawning another"
        _warm_defer(database, result, job, now_str, reason)
        return

    if at_ceiling:
        # §5.3: a warmer is an anchor, and an anchor is the first thing to
        # give up at the ceiling. Refusing here rather than letting the
        # anchor path refuse it also closes a loop: a skipped warmer is
        # re-warmable, so spawning one that is certain to be skipped would
        # submit a fresh job on every tick for as long as the ceiling holds.
        _warm_defer(database, result, job, now_str, f"30d window ceiling ({max_sessions}) reached; not warming (a warmer is an anchor)")
        return

    warmer_id = insert_warmer(database, job, now_str=now_str, config=config)
    db.update_job(database, job["id"], warmed_by=warmer_id)
    db.log_event(database, ts=now_str, job_id=warmer_id, kind="submitted", detail=f"warmer for job {job['id']}")
    _warm_defer(database, result, job, now_str, f"spawned warmer job {warmer_id} to open a window")


def warm_gate(
    database: sqlite_utils.Database,
    result: TickResult,
    job: Mapping[str, Any],
    *,
    now_str: str,
    now_dt: datetime,
    window_open: bool,
    probed: "usage.ParsedUsage | None",
    config: Mapping[str, Any] | None,
    at_ceiling: bool,
    max_sessions: int,
) -> bool:
    """Whether a ``--warm`` job may dispatch now (§5.5). Handles it if not.

    Returns ``True`` only for the one case that is worth a dispatch: a
    window is open, ``now`` has reached ``reset_at - lead``, and enough of
    that window is still unspent to be worth crossing the reset for. Every
    other answer defers the job and records why; none of them is terminal.

    This is a gate on top of the existing ones, never instead of them --
    the caller has already applied ``blocked_until``, ``max_parallel``, the
    dependencies and the ceiling, and applies ``--window`` afterwards. A
    ``True`` from here is permission to *keep going*, not permission to run.
    """
    if not window_open:
        _ensure_warmer(database, result, job, now_str=now_str, config=config, at_ceiling=at_ceiling, max_sessions=max_sessions)
        return False

    row = db.current_window(database, now_str)
    if row is None or not row.get("reset_at"):
        # A window this tick's own earlier dispatch opened: real, but its
        # ledger row cannot carry a `reset_at` yet (§5.3 -- nothing knows one
        # until the run's pre-probe). Guessing the end of it is exactly the
        # mistake this feature turns on not making, so wait a tick and read it.
        _warm_defer(database, result, job, now_str, "window is open but its reset time is not known yet")
        return False

    tail_start = _parse_dt(row["reset_at"]) - timedelta(seconds=warm_lead_seconds(job, config))
    if now_dt < tail_start:
        _warm_defer(database, result, job, now_str, f"waiting for the window tail at {tail_start.isoformat()}")
        return False

    # The live reading, not the cached one: the whole decision is "is there
    # anything left in this window worth burning", and a `pct_used` from an
    # hour ago cannot answer that. `probed` is always present here -- a warm
    # candidate forces the tick's probe for this reason -- and the ledger row
    # is only a fallback for a reading that arrived without a percentage.
    pct_used = probed.pct_used if probed is not None and probed.pct_used is not None else row.get("pct_used")
    min_pct_free = int(_setting(config, "warm", "min_pct_free"))
    if pct_used is None:
        _warm_defer(database, result, job, now_str, "window usage is unknown; not spending the tail blind")
        return False
    free = 100 - int(pct_used)
    if free < min_pct_free:
        # Not a failure and not a skip. The window got eaten by something
        # else, so there is no tail left to burn -- and starting a long job
        # one minute before a rate limit is worse than starting it late.
        _warm_defer(database, result, job, now_str, f"only {free}% of the window free, under [warm].min_pct_free ({min_pct_free})")
        return False
    return True


# ---------------------------------------------------------------------------
# the tick
# ---------------------------------------------------------------------------


def tick(
    database: sqlite_utils.Database,
    *,
    root: Path,
    config: Mapping[str, Any] | None = None,
    now: "str | Callable[[], str]" = _iso_now,
    spawn_run: Callable[[int], None] = _default_spawn_run,
    alive_check: Callable[[int], bool] = runner.is_alive,
    probe: Callable[[], usage.ParsedUsage] = usage.probe,
) -> TickResult:
    """Run one ``windowsill tick`` (§3.1): reap, evaluate the queue, dispatch up to
    ``max_parallel`` -- all under the dispatch lock -- then spawn the claimed
    runs detached, never blocking on a running job.

    ``now`` is an ISO-8601 timestamp string or a callable returning one (all
    comparisons are lexicographic ISO string comparisons, as elsewhere in
    the codebase). ``spawn_run(job_id)`` launches the detached run for a
    claimed job; ``alive_check`` is the pid-liveness predicate passed to
    :func:`windowsill.runner.reap`; ``probe`` is the ``/usage`` oracle (§4), called
    at most once per tick and only when a candidate's ``--window`` policy or
    ``--anchor`` status makes window state part of the decision (§4.2).
    ``config`` supplies ``[queue].max_parallel``, ``[queue].max_sessions_per_month``,
    ``[window].peak_band``, and -- only ever consulted for a ``--warm`` job
    (§5.5) -- ``[warm].lead``, ``[warm].min_pct_free`` and the ``[anchor]``
    model/body its warmer is built from.
    """
    now_str = _resolve_now(now)
    now_dt = _parse_dt(now_str)
    max_parallel = _max_parallel(config)
    max_sessions = int(_setting(config, "queue", "max_sessions_per_month"))
    peak_band = _setting(config, "window", "peak_band")
    result = TickResult()
    to_spawn: list[int] = []

    with db.dispatch_lock(database):
        # 1. Reap: a `running` job whose process is gone but which never
        #    recorded a terminal state (reboot / killed run wrapper). A
        #    normally-finished run_job has already left a terminal state, so
        #    it won't appear here. pid is NULL for a job only just claimed --
        #    its run has not recorded a pid yet, so there is nothing to reap.
        for job in db.list_jobs(database, state="running"):
            pid = job["pid"]
            if pid is None:
                continue
            dir_ = runner.job_dir(root, job["id"], job["name"])
            status = runner.reap(dir_, pid, alive_check=alive_check)
            if status.alive:
                continue
            # "killed"/"timeout"/"failed" run outcomes are all the terminal job
            # state "failed" (§7); "done" is "done"; "incomplete" (background
            # work still live) is "blocked". Read straight off the runner's
            # table rather than restated here -- a second copy of this mapping
            # is a second thing to forget to update.
            job_state = runner.JOB_STATE_FOR_OUTCOME.get(status.outcome or "", "failed")
            conversation_id = status.result.get("session_id") if status.result else None
            transcript.write_transcript(
                dir_,
                prompt=job["body"],
                extra_path=runner.log_destination(job.get("log_path"), job_id=job["id"], name=job["name"], cwd=job["cwd"]),
            )
            db.update_job(
                database,
                job["id"],
                state=job_state,
                finished_at=now_str,
                conversation_id=conversation_id,
            )
            db.log_event(
                database,
                ts=now_str,
                job_id=job["id"],
                kind=job_state,
                detail=f"reaped:{status.outcome}",
            )
            result.reaped.append(job["id"])

        # 2. Expire: a queued/ready job whose --before deadline has passed can
        #    never run in time -> expired (§7; do not silently run stale work).
        for state_name in ("queued", "ready"):
            for job in db.list_jobs(database, state=state_name):
                before_time = job["before_time"]
                if before_time is not None and before_time <= now_str:
                    db.update_job(database, job["id"], state="expired", finished_at=now_str)
                    db.log_event(
                        database,
                        ts=now_str,
                        job_id=job["id"],
                        kind="expired",
                        detail=f"before_time {before_time} passed",
                    )
                    result.expired.append(job["id"])

        # The global block gate (§3.1): a live `blocked_until` suppresses both
        # promotion and dispatch until it expires (cron/chain catches that).
        blocked_until = db.get_sched(database).get("blocked_until")
        blocked = blocked_until is not None and blocked_until > now_str

        if not blocked:
            # 3. Promote: queued -> ready once --at is due (or unset) and every
            #    --hold-jid / --resume-from dependency is done (§7). Not held
            #    (a distinct, CLI-controlled state), not blocked (above).
            for job in db.list_jobs(database, state="queued"):
                at_time = job["at_time"]
                if at_time is not None and at_time > now_str:
                    continue

                status, why = dependency_status(database, job)
                if status == "wait":
                    continue
                if status == "dead":
                    # The dependency reached a terminal state that isn't done,
                    # so this job's precondition can never be met.
                    db.update_job(database, job["id"], state="skipped", finished_at=now_str)
                    db.log_event(database, ts=now_str, job_id=job["id"], kind="skipped", detail=f"dependency: {why}")
                    result.skipped.append(job["id"])
                    continue

                db.update_job(database, job["id"], state="ready")
                db.log_event(database, ts=now_str, job_id=job["id"], kind="ready", detail="due")
                result.promoted.append(job["id"])

            # 4. Dispatch: claim up to the free slots' worth of ready jobs,
            #    lowest --priority first and FIFO within it (§2.3). The claim
            #    (state=running) IS the ready->running transition and the
            #    concurrency mutex; the detached run is spawned after the lock
            #    commits.
            active = len(db.list_jobs(database, state="running"))
            slots = max_parallel - active
            probed: usage.ParsedUsage | None = None
            # A window this tick's own dispatch just opened. The ledger row we
            # write for it has no reset_at yet (we won't know one until the
            # run's pre-probe, §4.3), so db.current_window can't see it -- and
            # a second dispatch in the same tick would count a second window.
            opened_this_tick = False

            candidates = sorted(db.list_jobs(database, state="ready"), key=lambda job: (job["priority"], job["id"]))
            for job in candidates:
                if slots <= 0:
                    break

                policy = job["window_policy"] or "share"
                is_anchor = bool(job["is_anchor"])
                warm = bool(job["warm"])

                # §4.2: probe only when the decision depends on window state,
                # and at most once per tick -- the reading is account-global.
                # A `--warm` job forces one for the same reason an anchor does,
                # and one more: its go/no-go includes live `pct_used` (§5.5's
                # `min_pct_free`), which nothing cached can supply.
                if probed is None and (window.policy_needs_probe(policy) or is_anchor or warm):
                    probed = probe()
                    result.probed = True
                    window.record_probe(database, probed, now_str=now_str)

                if opened_this_tick:
                    window_open = True
                elif probed is not None:
                    window_open = probed.is_open
                else:
                    window_open = db.current_window(database, now_str) is not None
                at_ceiling = window.at_or_over_ceiling(database, now_dt, max_sessions=max_sessions)

                if is_anchor:
                    # §5.2: an anchor exists to move a window boundary. A window
                    # that is already open has no boundary left to move, so the
                    # anchor no-ops rather than spending one for nothing.
                    if window_open:
                        _skip(database, result, job, now_str, "window already open")
                        continue
                    # §5.3: an anchor is discretionary; over the ceiling it is
                    # the first thing to give up.
                    if at_ceiling:
                        _skip(database, result, job, now_str, f"30d window ceiling ({max_sessions}) reached")
                        continue
                elif at_ceiling and not window_open and not warm:
                    # §5.3: warn loudly on real work, never silently block it.
                    # Not for a warm job with no window open, though: that one
                    # is about to be deferred rather than dispatched, so it
                    # opens nothing and the warning would be false. (Its
                    # warmer would open the window -- and the warm gate below
                    # refuses to spawn one at the ceiling.)
                    message = f"job {job['id']} opens a window with the 30d count already at the ceiling ({max_sessions})"
                    result.warnings.append(message)
                    db.log_event(database, ts=now_str, job_id=job["id"], kind="warning", detail=message)

                # §5.5: the warming gate. Defaults off -- a job without
                # `--warm` never enters this branch and takes exactly the path
                # it took before the feature existed.
                if warm and not warm_gate(
                    database,
                    result,
                    job,
                    now_str=now_str,
                    now_dt=now_dt,
                    window_open=window_open,
                    probed=probed,
                    config=config,
                    at_ceiling=at_ceiling,
                    max_sessions=max_sessions,
                ):
                    continue

                if not window.policy_permits(policy, now=now_dt, peak_band=peak_band, window_open=window_open):
                    result.deferred.append(job["id"])
                    continue

                # §2.3: --resume-from names a conversation that did not exist
                # when the job was submitted, so it resolves here.
                resume_id = resolve_resume(database, job)
                if resume_id and resume_id != job["resume_id"]:
                    db.update_job(database, job["id"], resume_id=resume_id)

                # §5.3: the ledger. A dispatch made while no window was open is
                # the dispatch that opened one.
                if not window_open:
                    db.insert_window(
                        database,
                        opened_at=now_str,
                        probed_at=now_str if probed is not None else None,
                        pct_used=probed.pct_used if probed is not None else None,
                        opened_by=job["id"],
                    )
                    opened_this_tick = True

                db.update_job(database, job["id"], state="running")  # CLAIM
                db.log_event(
                    database,
                    ts=now_str,
                    job_id=job["id"],
                    kind="claimed",
                    detail="dispatch",
                )
                result.dispatched.append(job["id"])
                to_spawn.append(job["id"])
                slots -= 1

    # 5. Spawn the claimed runs detached, OUTSIDE the lock (§3.1: Popen-and-
    #    exit, never an inline run_job -- that would block the tick).
    for job_id in to_spawn:
        spawn_run(job_id)

    return result
