"""ccq dispatch scheduler: the ``ccq tick`` step (design/03-execution-model.md §3.1)
and the §7 job-lifecycle state machine.

``tick`` is chain-driven and cron-floored (§3.1). One tick:

1. acquires the dispatch lock (:func:`ccq.db.dispatch_lock`, a
   ``BEGIN IMMEDIATE``);
2. **reaps** ``running`` jobs whose process died *without recording a
   terminal state* -- the :func:`ccq.runner.reap` safety net for a machine
   reboot or a run wrapper that was itself killed;
3. **evaluates** the queue: expire past-deadline jobs, then promote due
   ``queued`` jobs whose ``--hold-jid`` / ``--resume-from`` dependencies
   are satisfied to ``ready`` (§7);
4. **dispatches** up to ``max_parallel`` ready jobs, lowest ``--priority``
   first, subject to each job's ``--window`` policy (§5.1, decided from at
   most one ``/usage`` probe per tick), the anchor rule (§5.2) and the 30d
   ceiling (§5.3); and
5. commits and exits, **never blocking on a running job**.

``tick`` must never call :func:`ccq.runner.run_job` inline -- ``run_job``
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
production default Popens ``ccq run <id>`` (:data:`RUN_ARGV`) detached in
its own session. That ``ccq run`` subcommand must call
``runner.run_job(..., on_complete=<the ``ccq tick`` argv>)``
(:data:`TICK_ARGV`) so a finished run chain-dispatches the next tick
(§3.1). It is kept fully behind the ``spawn_run`` injectable so the test
suite spawns no process.

Everything a test must fake -- ``now``, ``spawn_run``, ``alive_check``,
``config`` -- is an injectable keyword with a real default, so the suite
reads no real clock and touches no real ``~/.ccq``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import sqlite_utils

from ccq import config as config_mod
from ccq import db, runner, transcript, usage, window

# ---------------------------------------------------------------------------
# the `ccq run` / `ccq tick` entrypoint contract (cli.py implements these)
# ---------------------------------------------------------------------------

#: argv prefix of the detached per-job run entrypoint. ``spawn_run``'s
#: default Popens ``[*RUN_ARGV, str(job_id)]`` -- i.e. ``ccq run <id>`` --
#: which cli.py must wire to :func:`ccq.runner.run_job`.
RUN_ARGV: tuple[str, ...] = ("ccq", "run")

#: argv of the next-tick chain command. ``ccq run <id>`` must pass this to
#: ``run_job(on_complete=...)`` so a finished run kicks the next tick (§3.1).
TICK_ARGV: tuple[str, ...] = ("ccq", "tick")


def _with_home(argv: tuple[str, ...], home: Path | None) -> list[str]:
    """``ccq --home <path> <subcommand>``, or plain ``ccq <subcommand>``.

    ``--home`` has to survive every hop of the chain. It doesn't just select a
    directory: a tick that dispatches against one home and a run that wakes up
    against another are operating on *different queues*, and the one the run
    picks is the real ``~/.ccq``. That mis-hop dispatches whichever job holds
    the same id there.
    """
    head, *tail = argv
    prefix = [head] if home is None else [head, "--home", str(home)]
    return [*prefix, *tail]


def run_argv(job_id: int, *, home: Path | None = None) -> list[str]:
    """The argv the production ``spawn_run`` Popens: ``ccq run <id>``."""
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
    """Production ``spawn_run``: Popen ``ccq run <id>`` detached, own session.

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
    """One ``[table].key`` from the config, falling back to :data:`ccq.config.DEFAULTS`.

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

    Everything in the DB is written UTC-aware (see :mod:`ccq.cli`), but a
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
    """Run one ``ccq tick`` (§3.1): reap, evaluate the queue, dispatch up to
    ``max_parallel`` -- all under the dispatch lock -- then spawn the claimed
    runs detached, never blocking on a running job.

    ``now`` is an ISO-8601 timestamp string or a callable returning one (all
    comparisons are lexicographic ISO string comparisons, as elsewhere in
    the codebase). ``spawn_run(job_id)`` launches the detached run for a
    claimed job; ``alive_check`` is the pid-liveness predicate passed to
    :func:`ccq.runner.reap`; ``probe`` is the ``/usage`` oracle (§4), called
    at most once per tick and only when a candidate's ``--window`` policy or
    ``--anchor`` status makes window state part of the decision (§4.2).
    ``config`` supplies ``[queue].max_parallel``, ``[queue].max_sessions_per_month``
    and ``[window].peak_band``.
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
            # "killed"/"timeout"/"failed" run outcomes are all the terminal
            # job state "failed" (§7); only "done" is "done" -- mirrors
            # runner._JOB_STATE_FOR_OUTCOME.
            job_state = "done" if status.outcome == "done" else "failed"
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

                # §4.2: probe only when the decision depends on window state,
                # and at most once per tick -- the reading is account-global.
                if probed is None and (window.policy_needs_probe(policy) or is_anchor):
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
                elif at_ceiling and not window_open:
                    # §5.3: warn loudly on real work, never silently block it.
                    message = f"job {job['id']} opens a window with the 30d count already at the ceiling ({max_sessions})"
                    result.warnings.append(message)
                    db.log_event(database, ts=now_str, job_id=job["id"], kind="warning", detail=message)

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
