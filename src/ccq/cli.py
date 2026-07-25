"""ccq command line interface (design/08-interface.md §8.1).

Thin glue over the already-built sibling modules -- this module owns no
parsing, execution, or scheduling logic of its own:

- :mod:`ccq.config` -- ``~/.ccq/config.toml``.
- :mod:`ccq.db` -- the SQLite state layer and identifier resolution.
- :mod:`ccq.jobfile` -- job-file directive parsing.
- :mod:`ccq.runner` -- argv building and the job-directory layout.
- :mod:`ccq.scheduler` -- the ``ccq tick`` dispatch step.

Every side-effecting dependency a test would otherwise have to fake for
real (the clock, sleeping while following a log, sending a signal,
spawning the next tick, ``exec``-ing into ``claude``) is a private
module-level seam -- ``_iso_now``, ``_sleep``, ``_terminate``, ``_exec``,
``_spawn_run`` -- referenced by name from inside each command body (never
bound as a default parameter value), so tests can ``monkeypatch.setattr``
them directly. This mirrors the injectable-keyword seams
:mod:`ccq.runner` and :mod:`ccq.scheduler` already use; Click commands
invoked through ``CliRunner`` can't pass extra Python kwargs, so a
module-level attribute is the equivalent seam here.

Only the M1 command set (design/08-interface.md §8.1) is implemented: ``sub``, ``stat``,
``show``, ``log``, ``del``, ``hold``, ``release``, ``resume``, ``tick``.
``report``, ``usage``, ``gc``, ``doctor``, and ``sub -e`` are later
milestones and are intentionally absent.
"""

from __future__ import annotations

import os
import signal
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import click
import sqlite_utils

from ccq import config, db, jobfile, runner, scheduler

# ---------------------------------------------------------------------------
# injectable seams -- see module docstring. Production defaults here;
# tests monkeypatch the module attribute directly.
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _default_terminate(pid: int) -> None:
    """SIGTERM a process group (§8.1 ``ccq del``: "SIGTERM the process
    group if running"). Best-effort -- a pid that's already gone is not
    an error."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


_terminate = _default_terminate

#: The claude invocation prefix ``ccq resume`` execs into. Same seam name
#: as ``ccq.runner``'s ``claude_cmd``.
_claude_cmd: tuple[str, ...] = ("claude",)


def _default_exec(argv: list[str], cwd: str) -> None:
    """``exec`` into ``argv`` after ``chdir``-ing to ``cwd`` (§8.1
    ``ccq resume``). Replaces the current process image -- never returns
    on success."""
    os.chdir(cwd)
    os.execvp(argv[0], argv)


_exec = _default_exec

#: Production ``spawn_run`` handed to :func:`ccq.scheduler.tick` -- Popens
#: ``ccq run <id>`` detached. Tests replace this wholesale with a spy so
#: no real process is ever spawned.
_spawn_run = scheduler._default_spawn_run

#: The per-job run entrypoint ``ccq run`` drives (:func:`ccq.runner.run_job`).
#: Same module-attribute seam style as ``_spawn_run``/``_exec``: tests
#: monkeypatch it so a fake ``claude`` (via ``_claude_cmd``) is spawned
#: instead of the real binary, and no chained ``ccq tick`` is ever Popened.
_run_job = runner.run_job


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


@contextmanager
def _session(home: Path | None) -> Iterator[tuple[sqlite_utils.Database, config.Config, Path]]:
    """Open the DB + config for one command, and the job-directory root.

    ``home`` overrides ``~/.ccq``'s parent (the ``--home`` group option);
    ``None`` means the real ``Path.home()``, mirroring how
    :mod:`ccq.config` / :mod:`ccq.db` resolve it themselves.
    """
    root = (home if home is not None else Path.home()) / ".ccq"
    root.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)  # cron redirects tick output here (README)
    cfg = config.load_config(home=home)
    database = db.connect(home=home)
    try:
        yield database, cfg, root
    finally:
        database.close()


def _resolve(database: sqlite_utils.Database, identifier: str) -> int:
    try:
        return db.resolve_identifier(database, identifier)
    except db.IdentifierError as exc:
        raise click.ClickException(str(exc)) from exc


def _default_name(source: str) -> str:
    """§2.3 ``--name``'s default: the filename stem. Stdin has no
    filename, so it falls back to a fixed label."""
    if source in ("-", "<stdin>"):
        return "stdin"
    return Path(source).stem


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize_at(value: str) -> str:
    """Normalize an ``--at`` value into the same UTC ISO form as :func:`_iso_now`.

    ``--at`` is accepted in any ISO-8601 form -- most often a naïve local
    wall-clock like ``2026-07-23T09:54`` (no offset). Stored verbatim it does
    not share ``_iso_now``'s ``...+00:00`` representation, and the scheduler's
    ``at_time <= now`` promotion test is a *lexicographic* string compare: on a
    machine at UTC+1, naïve local ``2026-07-23T09:54`` sorts *after* the UTC
    ``2026-07-23T08:54:55+00:00`` it actually precedes, so a due job never
    promotes. Normalizing here puts both sides in one representation, making
    that string compare a correct chronological one (the compare itself is
    left untouched -- see :mod:`ccq.scheduler`).

    A naïve value is interpreted in the system local zone (``.astimezone``
    presumes local for a naïve datetime); an offset-bearing one is respected.
    Either way the result is converted to UTC and serialized with
    ``.isoformat()``.
    """
    return _parse_iso(value).astimezone(timezone.utc).isoformat()


def _cutoff(now_str: str, *, days: int) -> str:
    """``now_str`` minus ``days``, formatted so plain string comparison
    against other timestamps in this codebase stays valid (all of them
    are lexicographically-ordered ISO-8601, per :mod:`ccq.scheduler`)."""
    return (_parse_iso(now_str) - timedelta(days=days)).isoformat()


def _format_duration(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _when(job: dict) -> str:
    return job.get("started_at") or job.get("at_time") or job.get("submitted_at") or "-"


def _elapsed(job: dict, now_str: str) -> str:
    start = job.get("started_at") or job.get("submitted_at")
    if not start:
        return "-"
    end = job.get("finished_at") or now_str
    return _format_duration(_parse_iso(end) - _parse_iso(start))


def _window_count_30d(database: sqlite_utils.Database, now_str: str) -> int:
    cutoff = _cutoff(now_str, days=30)
    rows = database["window"].rows_where("opened_at >= ?", [cutoff])
    return len(list(rows))


def _tail(path: Path, n: int = 10) -> list[str]:
    if not path.exists():
        return []
    return path.read_text().splitlines()[-n:]


def _read_new_bytes(path: Path, pos: int) -> tuple[int, bytes]:
    if not path.exists():
        return pos, b""
    with path.open("rb") as f:
        f.seek(pos)
        chunk = f.read()
    return pos + len(chunk), chunk


# ---------------------------------------------------------------------------
# the group
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--home",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the ccq home directory (parent of .ccq/); mainly for tests.",
)
@click.pass_context
def main(ctx: click.Context, home: Path | None) -> None:
    """ccq -- a local batch queue for Claude Code."""
    ctx.ensure_object(dict)
    ctx.obj["home"] = home


# ---------------------------------------------------------------------------
# ccq sub
# ---------------------------------------------------------------------------


@main.command()
@click.argument("file", type=click.File("r"))
@click.option("--now", "run_now", is_flag=True, help="Bypass --at; eligible as soon as a tick runs.")
@click.pass_context
def sub(ctx: click.Context, file, run_now: bool) -> None:
    """Submit a job file. ``-`` reads stdin. Prints the new job id."""
    with _session(ctx.obj["home"]) as (database, cfg, _root):
        text = file.read()
        jf = jobfile.parse_jobfile(text)
        d = jf.directives

        name = d.name or _default_name(file.name)
        cwd = str(Path(d.cwd).expanduser()) if d.cwd else str(Path.cwd())
        isolate = d.isolate or cfg["safety"]["default_isolate"]
        notify = d.notify or cfg["notify"]["default"]
        at_time = None if run_now or d.at is None else _normalize_at(d.at)
        state = "held" if d.hold else "queued"
        submitted_at = _iso_now()

        job_id = db.insert_job(
            database,
            name=name,
            state=state,
            submitted_at=submitted_at,
            cwd=cwd,
            model=jf.model,
            body=jf.body,
            passthrough=jf.passthrough,
            is_anchor=d.anchor,
            at_time=at_time,
            before_time=d.before,
            window_policy=d.window,
            resume_id=d.resume,
            resume_from=d.resume_from,
            hold_jid=d.hold_jid,
            retry_max=d.retry,
            priority=d.priority,
            isolate=isolate,
            allow_dirty=d.allow_dirty,
            notify=notify,
            timeout=d.timeout,
            no_resume_on_limit=d.no_resume_on_limit,
        )
        db.log_event(database, ts=submitted_at, job_id=job_id, kind="submitted", detail=state)
        click.echo(str(job_id))


# ---------------------------------------------------------------------------
# ccq stat
# ---------------------------------------------------------------------------


@main.command()
@click.option("-a", "--all", "show_all", is_flag=True, help="Include terminal jobs from the last 7 days.")
@click.pass_context
def stat(ctx: click.Context, show_all: bool) -> None:
    """id, name, state, model, when, cwd, elapsed. Plus window state and 30d count."""
    with _session(ctx.obj["home"]) as (database, _cfg, _root):
        now_str = _iso_now()
        jobs = db.list_jobs(database)

        if show_all:
            cutoff = _cutoff(now_str, days=7)
            jobs = [job for job in jobs if job["state"] not in db.TERMINAL_STATES or (job.get("finished_at") or job.get("submitted_at") or "") >= cutoff]
        else:
            jobs = [job for job in jobs if job["state"] not in db.TERMINAL_STATES]

        click.echo(f"{'ID':<5} {'NAME':<20} {'STATE':<10} {'MODEL':<20} {'WHEN':<32} {'CWD':<30} ELAPSED")
        for job in jobs:
            click.echo(
                f"{job['id']:<5} {job['name']:<20} {job['state']:<10} "
                f"{(job['model'] or '-'):<20} {_when(job):<32} {job['cwd']:<30} {_elapsed(job, now_str)}"
            )

        window = db.current_window(database, now_str)
        if window is None:
            click.echo("window: closed")
        else:
            click.echo(f"window: open (pct_used={window.get('pct_used')}, reset_at={window.get('reset_at')})")
        click.echo(f"30d sessions: {_window_count_30d(database, now_str)}")


# ---------------------------------------------------------------------------
# ccq show
# ---------------------------------------------------------------------------


@main.command()
@click.argument("identifier")
@click.pass_context
def show(ctx: click.Context, identifier: str) -> None:
    """Directives, resolved argv, state, conversation id, delta%, transcript tail."""
    with _session(ctx.obj["home"]) as (database, _cfg, root):
        job_id = _resolve(database, identifier)
        job = db.get_job(database, job_id)
        argv = runner.build_argv(job["passthrough"], resume=job.get("resume_id"))

        runs = db.list_runs(database, job_id=job_id)
        last_run = runs[-1] if runs else None
        delta = None
        if last_run and last_run.get("pct_before") is not None and last_run.get("pct_after") is not None:
            delta = last_run["pct_after"] - last_run["pct_before"]

        dir_ = runner.job_dir(root, job_id, job["name"])
        tail = _tail(dir_ / "stream.jsonl")

        click.echo(f"id: {job['id']}")
        click.echo(f"name: {job['name']}")
        click.echo(f"state: {job['state']}")
        click.echo(f"cwd: {job['cwd']}")
        click.echo(f"model: {job['model'] or '-'}")
        click.echo(f"conversation_id: {job['conversation_id'] or '-'}")
        click.echo(f"delta%: {delta if delta is not None else 'n/a'}")
        click.echo("directives:")
        click.echo(f"  window: {job['window_policy']}")
        click.echo(f"  anchor: {bool(job['is_anchor'])}")
        click.echo(f"  resume_id: {job['resume_id'] or '-'}")
        click.echo(f"  resume_from: {job['resume_from'] or '-'}")
        click.echo(f"  hold_jid: {job['hold_jid'] or []}")
        click.echo(f"  retry_max: {job['retry_max']}")
        click.echo(f"  priority: {job['priority']}")
        click.echo(f"  isolate: {job['isolate'] or '-'}")
        click.echo(f"  allow_dirty: {bool(job['allow_dirty'])}")
        click.echo(f"  notify: {job['notify'] or '-'}")
        click.echo(f"  timeout: {job['timeout']}")
        click.echo(f"  no_resume_on_limit: {bool(job['no_resume_on_limit'])}")
        click.echo(f"argv: {' '.join(argv)}")
        click.echo("transcript tail:")
        for line in tail:
            click.echo(line)


# ---------------------------------------------------------------------------
# ccq log
# ---------------------------------------------------------------------------


@main.command()
@click.argument("identifier")
@click.option("-f", "--follow", is_flag=True, help="Follow a running job's transcript.")
@click.pass_context
def log(ctx: click.Context, identifier: str, follow: bool) -> None:
    """Stream the transcript; -f follows a running job."""
    with _session(ctx.obj["home"]) as (database, _cfg, root):
        job_id = _resolve(database, identifier)
        job = db.get_job(database, job_id)
        path = runner.job_dir(root, job_id, job["name"]) / "stream.jsonl"

        pos, chunk = _read_new_bytes(path, 0)
        if chunk:
            click.echo(chunk.decode(errors="replace"), nl=False)

        if not follow:
            return

        while True:
            current = db.get_job(database, job_id)
            if current is None or current["state"] != "running":
                break
            _sleep(0.5)
            pos, chunk = _read_new_bytes(path, pos)
            if chunk:
                click.echo(chunk.decode(errors="replace"), nl=False)

        # Final drain: bytes written in the gap between the last liveness
        # check and the job actually going terminal shouldn't be lost.
        pos, chunk = _read_new_bytes(path, pos)
        if chunk:
            click.echo(chunk.decode(errors="replace"), nl=False)


# ---------------------------------------------------------------------------
# ccq del
# ---------------------------------------------------------------------------


@main.command("del")
@click.argument("identifier")
@click.pass_context
def del_(ctx: click.Context, identifier: str) -> None:
    """Cancel; SIGTERM the process group if running."""
    with _session(ctx.obj["home"]) as (database, _cfg, _root):
        job_id = _resolve(database, identifier)
        job = db.get_job(database, job_id)

        if job["state"] in db.TERMINAL_STATES:
            click.echo(f"job {job_id} already {job['state']}; nothing to cancel")
            return

        now_str = _iso_now()
        if job["state"] == "running" and job.get("pid") is not None:
            _terminate(job["pid"])

        db.update_job(database, job_id, state="cancelled", finished_at=now_str)
        db.log_event(database, ts=now_str, job_id=job_id, kind="cancelled", detail="ccq del")
        click.echo(f"cancelled {job_id}")


# ---------------------------------------------------------------------------
# ccq hold / ccq release
# ---------------------------------------------------------------------------


@main.command()
@click.argument("identifier")
@click.pass_context
def hold(ctx: click.Context, identifier: str) -> None:
    """Hold a queued/ready job; needs ``ccq release``."""
    with _session(ctx.obj["home"]) as (database, _cfg, _root):
        job_id = _resolve(database, identifier)
        job = db.get_job(database, job_id)

        if job["state"] == "held":
            click.echo(f"job {job_id} already held")
            return
        if job["state"] not in ("queued", "ready"):
            raise click.ClickException(f"cannot hold job {job_id} in state {job['state']!r}")

        now_str = _iso_now()
        db.update_job(database, job_id, state="held")
        db.log_event(database, ts=now_str, job_id=job_id, kind="held", detail="ccq hold")
        click.echo(f"held {job_id}")


@main.command()
@click.argument("identifier")
@click.pass_context
def release(ctx: click.Context, identifier: str) -> None:
    """Release a held job back to ``queued``."""
    with _session(ctx.obj["home"]) as (database, _cfg, _root):
        job_id = _resolve(database, identifier)
        job = db.get_job(database, job_id)

        if job["state"] != "held":
            raise click.ClickException(f"job {job_id} is not held (state {job['state']!r})")

        now_str = _iso_now()
        db.update_job(database, job_id, state="queued")
        db.log_event(database, ts=now_str, job_id=job_id, kind="queued", detail="ccq release")
        click.echo(f"released {job_id}")


# ---------------------------------------------------------------------------
# ccq resume
# ---------------------------------------------------------------------------


@main.command()
@click.argument("identifier")
@click.pass_context
def resume(ctx: click.Context, identifier: str) -> None:
    """exec into ``claude --resume <conversation_id>`` in the job's cwd."""
    with _session(ctx.obj["home"]) as (database, _cfg, _root):
        job_id = _resolve(database, identifier)
        job = db.get_job(database, job_id)

        conversation_id = job.get("conversation_id")
        if not conversation_id:
            raise click.ClickException(f"job {job_id} has no conversation to resume")

        argv = [*_claude_cmd, "--resume", conversation_id]
        _exec(argv, job["cwd"])


# ---------------------------------------------------------------------------
# ccq run
# ---------------------------------------------------------------------------


@main.command("run")
@click.argument("job_id", type=int)
@click.pass_context
def run_(ctx: click.Context, job_id: int) -> None:
    """Internal; execute one dispatched job to completion (spawned by tick)."""
    with _session(ctx.obj["home"]) as (database, _cfg, root):
        if db.get_job(database, job_id) is None:
            raise click.ClickException(f"no job with id {job_id}")
        _run_job(
            database,
            job_id,
            root=root,
            on_complete=list(scheduler.TICK_ARGV),
            claude_cmd=_claude_cmd,
        )


# ---------------------------------------------------------------------------
# ccq tick
# ---------------------------------------------------------------------------


@main.command()
@click.option("-q", "--quiet", is_flag=True, help="Suppress the summary line (for cron, which mails any output).")
@click.pass_context
def tick(ctx: click.Context, quiet: bool) -> None:
    """Internal; run one scheduler pass -- promote due jobs and dispatch ready ones (used by cron and the run chain)."""
    with _session(ctx.obj["home"]) as (database, cfg, root):
        result = scheduler.tick(database, root=root, config=cfg, now=_iso_now, spawn_run=_spawn_run)
        if not quiet:
            click.echo(f"reaped={len(result.reaped)} expired={len(result.expired)} promoted={len(result.promoted)} dispatched={len(result.dispatched)}")


if __name__ == "__main__":  # pragma: no cover
    main()
