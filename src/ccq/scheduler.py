"""ccq dispatch scheduler: the ``ccq tick`` step (design/03-execution-model.md §3.1) and the
slice of the §7 job-lifecycle state machine that M1 needs.

``tick`` is chain-driven and cron-floored (§3.1). One tick:

1. acquires the dispatch lock (:func:`ccq.db.dispatch_lock`, a
   ``BEGIN IMMEDIATE``);
2. **reaps** ``running`` jobs whose process died *without recording a
   terminal state* -- the :func:`ccq.runner.reap` safety net for a machine
   reboot or a run wrapper that was itself killed;
3. **evaluates** the queue (expire past-deadline jobs; promote due
   ``queued`` jobs to ``ready``);
4. **dispatches** up to ``max_parallel`` ready jobs; and
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

from ccq import db, runner

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


def run_argv(job_id: int) -> list[str]:
    """The argv the production ``spawn_run`` Popens: ``ccq run <id>``."""
    return [*RUN_ARGV, str(job_id)]


def _default_spawn_run(
    job_id: int,
    *,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> None:
    """Production ``spawn_run``: Popen ``ccq run <id>`` detached, own session.

    Popen-and-exit only (§3.1): the tick never awaits it, so a fast-failing
    run cannot fork-bomb. Kept behind the ``spawn_run`` injectable so tests
    never actually spawn it.
    """
    popen(
        run_argv(job_id),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_now(now: "str | Callable[[], str]") -> str:
    return now() if callable(now) else now


def _max_parallel(config: Mapping[str, Any] | None) -> int:
    """``[queue].max_parallel`` (§8.3), defaulting to 1 (§3.1)."""
    if not config:
        return 1
    return int(config.get("queue", {}).get("max_parallel", 1))


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
) -> TickResult:
    """Run one ``ccq tick`` (§3.1): reap, evaluate the queue, dispatch up to
    ``max_parallel`` -- all under the dispatch lock -- then spawn the claimed
    runs detached, never blocking on a running job.

    ``now`` is an ISO-8601 timestamp string or a callable returning one (all
    comparisons are lexicographic ISO string comparisons, as elsewhere in
    the codebase). ``spawn_run(job_id)`` launches the detached run for a
    claimed job; ``alive_check`` is the pid-liveness predicate passed to
    :func:`ccq.runner.reap`. ``config`` supplies ``[queue].max_parallel``.
    """
    now_str = _resolve_now(now)
    max_parallel = _max_parallel(config)
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
            # 3. Promote: queued -> ready once --at is due (or unset). Not
            #    held (a distinct, CLI-controlled state), not blocked (above).
            for job in db.list_jobs(database, state="queued"):
                at_time = job["at_time"]
                if at_time is None or at_time <= now_str:
                    db.update_job(database, job["id"], state="ready")
                    db.log_event(database, ts=now_str, job_id=job["id"], kind="ready", detail="due")
                    result.promoted.append(job["id"])

            # 4. Dispatch: claim up to the free slots' worth of ready jobs
            #    (FIFO by id). The claim (state=running) IS the ready->running
            #    transition and the concurrency mutex; the detached run is
            #    spawned after the lock commits.
            active = len(db.list_jobs(database, state="running"))
            slots = max_parallel - active
            if slots > 0:
                for job in db.list_jobs(database, state="ready")[:slots]:
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

    # 5. Spawn the claimed runs detached, OUTSIDE the lock (§3.1: Popen-and-
    #    exit, never an inline run_job -- that would block the tick).
    for job_id in to_spawn:
        spawn_run(job_id)

    return result
