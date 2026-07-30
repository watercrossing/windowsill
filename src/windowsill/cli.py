"""windowsill command line interface (design/08-interface.md §8.1).

Thin glue over the already-built sibling modules -- this module owns no
parsing, execution, or scheduling logic of its own:

- :mod:`windowsill.config` -- ``~/.windowsill/config.toml``.
- :mod:`windowsill.db` -- the SQLite state layer and identifier resolution.
- :mod:`windowsill.jobfile` -- job-file directive parsing.
- :mod:`windowsill.runner` -- argv building and the job-directory layout.
- :mod:`windowsill.scheduler` -- the ``ccq tick`` dispatch step.

Every side-effecting dependency a test would otherwise have to fake for
real (the clock, sleeping while following a log, sending a signal,
spawning the next tick, ``exec``-ing into ``claude``) is a private
module-level seam -- ``_iso_now``, ``_sleep``, ``_terminate``, ``_exec``,
``_spawn_run`` -- referenced by name from inside each command body (never
bound as a default parameter value), so tests can ``monkeypatch.setattr``
them directly. This mirrors the injectable-keyword seams
:mod:`windowsill.runner` and :mod:`windowsill.scheduler` already use; Click commands
invoked through ``CliRunner`` can't pass extra Python kwargs, so a
module-level attribute is the equivalent seam here.

The M1 and M2 command set (design/08-interface.md §8.1) is implemented:
``sub``, ``stat``, ``show``, ``log``, ``del``, ``hold``, ``release``,
``resume``, ``usage``, ``report``, ``run``, ``tick``. ``gc``, ``doctor``,
and ``sub -e`` are M3 and are intentionally absent.

Every timestamp this module prints goes through :func:`_local` (§8.1):
stored UTC, displayed as bare system local time, with ``--with-timezone``
as the opt-in for the offset-bearing form.
"""

from __future__ import annotations

import os
import signal
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Iterator, Mapping

import click
import sqlite_utils

from windowsill import config, db, jobfile, notify, runner, scheduler, window

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
#: as ``windowsill.runner``'s ``claude_cmd``.
_claude_cmd: tuple[str, ...] = ("claude",)


def _default_exec(argv: list[str], cwd: str) -> None:
    """``exec`` into ``argv`` after ``chdir``-ing to ``cwd`` (§8.1
    ``ccq resume``). Replaces the current process image -- never returns
    on success."""
    os.chdir(cwd)
    os.execvp(argv[0], argv)


_exec = _default_exec

#: Production ``spawn_run`` handed to :func:`windowsill.scheduler.tick` -- Popens
#: ``ccq run <id>`` detached. Tests replace this wholesale with a spy so
#: no real process is ever spawned.
_spawn_run = scheduler._default_spawn_run

#: The per-job run entrypoint ``ccq run`` drives (:func:`windowsill.runner.run_job`).
#: Same module-attribute seam style as ``_spawn_run``/``_exec``: tests
#: monkeypatch it so a fake ``claude`` (via ``_claude_cmd``) is spawned
#: instead of the real binary, and no chained ``ccq tick`` is ever Popened.
_run_job = runner.run_job

#: The ``/usage`` oracle handed to :func:`windowsill.scheduler.tick` (§4). Same seam
#: style again: tests replace it so no real ``claude`` is ever probed.
_probe = runner._default_probe


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


_HOME_HELP = "Override the windowsill home directory (parent of .windowsill/); mainly for tests."


def home_option(f):
    """Give a command its own ``--home``.

    The four short verbs are console scripts in their own right
    (``ccsub``, ``ccstat``, ``ccdel``, ``ccresume`` -- design/08-interface.md §8.1), so
    the ``main`` group's callback never runs for them and ``ctx.obj`` is
    ``None``. Carrying the option on the command is what makes
    ``ccsub --home /tmp/scratch job.md`` work at all, and pointing at a
    throwaway home is the only safe way to exercise a queue that may be
    running the very job you are typing into.
    """
    return click.option("--home", type=click.Path(path_type=Path), default=None, help=_HOME_HELP)(f)


def _home(ctx: click.Context, home: Path | None = None) -> Path | None:
    """The home this invocation should use.

    A command's own ``--home`` wins over the group's, so both
    ``ccq --home X stat`` and ``ccstat --home X`` land on the same queue.
    ``ctx.obj`` is ``None`` when a command was invoked as its own script.
    """
    if home is not None:
        return home
    return (ctx.obj or {}).get("home")


@contextmanager
def _session(home: Path | None) -> Iterator[tuple[sqlite_utils.Database, config.Config, Path]]:
    """Open the DB + config for one command, and the job-directory root.

    ``home`` overrides ``~/.windowsill``'s parent (the ``--home`` option);
    ``None`` means the real ``Path.home()``, mirroring how
    :mod:`windowsill.config` / :mod:`windowsill.db` resolve it themselves.
    """
    root = config.state_dir(home)
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


def _expand_log(value: str | None) -> str | None:
    """Expand ``~`` in a ``--log`` value, keeping any trailing separator.

    The trailing ``/`` is load-bearing (§2.3: it means "a directory", even one
    that does not exist yet) and :meth:`Path.expanduser` normalises it away.
    """
    if not value:
        return None
    expanded = str(Path(value).expanduser())
    if value.endswith(("/", os.sep)) and not expanded.endswith(("/", os.sep)):
        expanded += os.sep
    return expanded


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
    left untouched -- see :mod:`windowsill.scheduler`).

    A naïve value is interpreted in the system local zone (``.astimezone``
    presumes local for a naïve datetime); an offset-bearing one is respected.
    Either way the result is converted to UTC and serialized with
    ``.isoformat()``.
    """
    return _parse_iso(value).astimezone(timezone.utc).isoformat()


#: Column widths for a displayed timestamp, bare and with the offset shown.
_WHEN_WIDTH = {False: 16, True: 25}


def _local(value: str | None, *, with_timezone: bool = False) -> str:
    """Render a stored timestamp for a human (§8.1).

    Everything is stored UTC; everything is displayed in the system's own
    zone, and by default with no offset at all -- on a single-user machine
    "02:00" means the 02:00 on your wall, and a trailing "+01:00" only invites
    the reader to do arithmetic that is already done. ``with_timezone``
    restores the full offset-bearing ISO form, for comparing against a clock
    that isn't this machine's.

    A value that doesn't parse is passed through untouched rather than hidden
    behind a placeholder -- if something ever writes a non-ISO timestamp, the
    display is where it should be visible.
    """
    if not value:
        return "-"
    try:
        moment = _parse_iso(value).astimezone()
    except ValueError:
        return value
    if with_timezone:
        return moment.isoformat(timespec="seconds")
    return moment.strftime("%Y-%m-%d %H:%M")


def _notify_display(value: str | None, cfg: Mapping[str, object]) -> str:
    """A job's ``--notify`` plus the destination it would actually reach (§8.4).

    Notifications are opt-in and have no default destination, so ``email`` on
    its own is not evidence that anything will arrive -- and a surface that
    prints the channel and stops reads as if it were. Resolve it here instead:
    either the address/topic, or the fact that there isn't one.
    """
    channel, override = notify.split_channel(value)
    if channel == "none":
        return "none"
    where = notify.destination(channel, cfg, override)
    if where is None:
        key = notify.DESTINATION_KEY.get(channel)
        if key is None:
            return f"{channel} (unknown channel)"
        return f"{channel} (no destination configured; set notify.{channel}.{key})"
    return f"{channel} -> {where}"


def _cutoff(now_str: str, *, days: int) -> str:
    """``now_str`` minus ``days``, formatted so plain string comparison
    against other timestamps in this codebase stays valid (all of them
    are lexicographically-ordered ISO-8601, per :mod:`windowsill.scheduler`)."""
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
    help=_HOME_HELP,
)
@click.pass_context
def main(ctx: click.Context, home: Path | None) -> None:
    """windowsill -- a local batch queue for Claude Code."""
    ctx.ensure_object(dict)
    ctx.obj["home"] = home


# ---------------------------------------------------------------------------
# ccq sub
# ---------------------------------------------------------------------------


@main.command()
@click.argument("file", type=click.File("r"))
@click.option("--now", "run_now", is_flag=True, help="Bypass --at; eligible as soon as a tick runs.")
@home_option
@click.pass_context
def sub(ctx: click.Context, file, run_now: bool, home: Path | None) -> None:
    """Submit a job file. ``-`` reads stdin. Prints the new job id."""
    with _session(_home(ctx, home)) as (database, cfg, root):
        text = file.read()
        jf = jobfile.parse_jobfile(text)
        d = jf.directives

        # §2.3: mutually exclusive. The parser deliberately doesn't reject this
        # (pass-through beats validation, §0.4) -- the scheduler side does, and
        # failing at submit beats failing at 02:00.
        if d.resume and d.resume_from:
            raise click.ClickException("--resume and --resume-from are mutually exclusive")

        name = d.name or _default_name(file.name)
        cwd = str(Path(d.cwd).expanduser()) if d.cwd else str(Path.cwd())
        log_path = _expand_log(d.log)
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
            log_path=log_path,
            no_resume_on_limit=d.no_resume_on_limit,
        )

        # job.md -- the verbatim submission (§6.2). Written here rather than
        # at dispatch so the job dir holds what was submitted even for a job
        # that is cancelled, expires, or never runs at all.
        dir_ = runner.job_dir(root, job_id, name)
        dir_.mkdir(parents=True, exist_ok=True)
        runner.write_job_md(dir_, text)

        db.log_event(database, ts=submitted_at, job_id=job_id, kind="submitted", detail=state)
        click.echo(str(job_id))


# ---------------------------------------------------------------------------
# ccq stat
# ---------------------------------------------------------------------------


@main.command()
@click.option("-a", "--all", "show_all", is_flag=True, help="Include terminal jobs from the last 7 days.")
@click.option("--with-timezone", is_flag=True, help="Show times with their UTC offset instead of bare system local time.")
@home_option
@click.pass_context
def stat(ctx: click.Context, show_all: bool, with_timezone: bool, home: Path | None) -> None:
    """id, name, state, model, when, cwd, elapsed. Plus window state and 30d count."""
    with _session(_home(ctx, home)) as (database, _cfg, _root):
        now_str = _iso_now()
        jobs = db.list_jobs(database)

        if show_all:
            cutoff = _cutoff(now_str, days=7)
            jobs = [job for job in jobs if job["state"] not in db.TERMINAL_STATES or (job.get("finished_at") or job.get("submitted_at") or "") >= cutoff]
        else:
            jobs = [job for job in jobs if job["state"] not in db.TERMINAL_STATES]

        when_width = _WHEN_WIDTH[with_timezone]
        click.echo(f"{'ID':<5} {'NAME':<20} {'STATE':<10} {'MODEL':<20} {'WHEN':<{when_width}} {'CWD':<30} ELAPSED")
        for job in jobs:
            when = _local(_when(job), with_timezone=with_timezone)
            click.echo(
                f"{job['id']:<5} {job['name']:<20} {job['state']:<10} "
                f"{(job['model'] or '-'):<20} {when:<{when_width}} {job['cwd']:<30} {_elapsed(job, now_str)}"
            )

        window = db.current_window(database, now_str)
        if window is None:
            click.echo("window: closed")
        else:
            reset_at = _local(window.get("reset_at"), with_timezone=with_timezone)
            click.echo(f"window: open (pct_used={window.get('pct_used')}, resets {reset_at})")
        click.echo(f"30d sessions: {_window_count_30d(database, now_str)}")


# ---------------------------------------------------------------------------
# ccq usage
# ---------------------------------------------------------------------------

#: §4.2: "Rate-limit to 1 probe per 10s". A probe is free but not polite.
PROBE_MIN_INTERVAL_SECONDS = 10


@main.command("usage")
@click.option("--probe", "force_probe", is_flag=True, help="Read /usage now instead of the cached window state.")
@click.option("--with-timezone", is_flag=True, help="Show times with their UTC offset instead of bare system local time.")
@click.pass_context
def usage_(ctx: click.Context, force_probe: bool, with_timezone: bool) -> None:
    """Cached window state. ``--probe`` forces a fresh read."""
    with _session(_home(ctx)) as (database, cfg, _root):
        now_str = _iso_now()
        window_row = db.current_window(database, now_str)

        if force_probe:
            # §4.2: cheap, but not free of manners -- refuse to hammer it.
            last = (window_row or {}).get("probed_at")
            if last and (_parse_iso(now_str) - _parse_iso(last)).total_seconds() < PROBE_MIN_INTERVAL_SECONDS:
                raise click.ClickException(f"probed less than {PROBE_MIN_INTERVAL_SECONDS}s ago; showing cache is `ccq usage`")
            parsed = _probe()
            window.record_probe(database, parsed, now_str=now_str)
            window_row = db.current_window(database, now_str)

        if window_row is None:
            click.echo("window: closed")
        else:
            pct = window_row.get("pct_used")
            resets = _local(window_row.get("reset_at"), with_timezone=with_timezone)
            click.echo(f"window: open · {pct if pct is not None else '?'}% used · resets {resets}")
            if window_row.get("weekly_pct") is not None:
                weekly_reset = _local(window_row.get("weekly_reset_at"), with_timezone=with_timezone)
                click.echo(f"week (all models): {window_row['weekly_pct']}% · resets {weekly_reset}")
            if window_row.get("extra_bucket"):
                click.echo(f"week ({window_row['extra_bucket']}): {window_row['extra_pct']}%")
            click.echo(f"last probe: {_local(window_row.get('probed_at'), with_timezone=with_timezone)}")

        ceiling = cfg["queue"]["max_sessions_per_month"]
        click.echo(f"30d windows: {_window_count_30d(database, now_str)} / {ceiling}")

        blocked_until = db.get_sched(database).get("blocked_until")
        if blocked_until and blocked_until > now_str:
            click.echo(f"blocked until {_local(blocked_until, with_timezone=with_timezone)}")


# ---------------------------------------------------------------------------
# ccq report
# ---------------------------------------------------------------------------


def _since_cutoff(now_str: str, since: str) -> str:
    """``--since 24h`` as an absolute timestamp comparable with the DB's."""
    seconds = runner.parse_timeout_seconds(since)
    if seconds is None:
        raise click.ClickException(f"unparseable --since: {since!r}")
    return (_parse_iso(now_str) - timedelta(seconds=seconds)).isoformat()


def _delta_pct(database: sqlite_utils.Database, job_id: int) -> int | None:
    """Δ window % of a job's last run (§4.3), when both probes landed."""
    runs = db.list_runs(database, job_id=job_id)
    if not runs:
        return None
    last = runs[-1]
    if last.get("pct_before") is None or last.get("pct_after") is None:
        return None
    return last["pct_after"] - last["pct_before"]


def _permission_denials(database: sqlite_utils.Database, job_id: int) -> int | None:
    """Permission denials of a job's last run, or ``None`` when there is no
    run yet / the run predates this column (§9 "Settled 2026-07-28, second
    sitting"): a run can read ``done`` having written nothing because every
    write was refused, and nothing else in the output flags that."""
    runs = db.list_runs(database, job_id=job_id)
    if not runs:
        return None
    return runs[-1].get("permission_denials")


@main.command()
@click.option("--since", default="24h", show_default=True, help="How far back to report (e.g. 24h, 7d, 90m).")
@click.option("--with-timezone", is_flag=True, help="Show times with their UTC offset instead of bare system local time.")
@click.pass_context
def report(ctx: click.Context, since: str, with_timezone: bool) -> None:
    """Digest: what ran, delta%, duration, state -- one line each."""
    with _session(_home(ctx)) as (database, _cfg, _root):
        now_str = _iso_now()
        cutoff = _since_cutoff(now_str, since)

        jobs = [job for job in db.list_jobs(database) if max(job.get("started_at") or "", job.get("finished_at") or "") >= cutoff]

        click.echo(f"since {_local(cutoff, with_timezone=with_timezone)} ({since})")
        click.echo("")

        total_delta = 0
        for job in jobs:
            delta = _delta_pct(database, job["id"])
            if delta is not None:
                total_delta += delta
            delta_text = f"Δ {delta}%" if delta is not None else "Δ -"
            denials = _permission_denials(database, job["id"])
            # Terse and silent when zero/unknown (§8.1 one-screen constraint) --
            # only a non-zero count is worth a reader's attention (§9
            # "Settled 2026-07-28, second sitting").
            denial_text = f" denied={denials}" if denials else ""
            click.echo(
                f"{job['id']:<5} {job['name']:<24} {job['state']:<10} {_elapsed(job, now_str):<9} {delta_text:<7} ccq resume {job['id']}{denial_text}"
            )

        if not jobs:
            click.echo("(nothing ran)")

        click.echo("")
        window_row = db.current_window(database, now_str)
        window_text = "closed" if window_row is None else f"open until {_local(window_row.get('reset_at'), with_timezone=with_timezone)}"
        click.echo(f"{len(jobs)} jobs · Δ {total_delta}% · window: {window_text} · {_window_count_30d(database, now_str)} windows in 30d")


# ---------------------------------------------------------------------------
# ccq show
# ---------------------------------------------------------------------------


@main.command()
@click.argument("identifier")
@click.pass_context
def show(ctx: click.Context, identifier: str) -> None:
    """Directives, resolved argv, state, conversation id, delta%, transcript tail."""
    with _session(_home(ctx)) as (database, cfg, root):
        job_id = _resolve(database, identifier)
        job = db.get_job(database, job_id)
        argv = runner.build_argv(job["passthrough"], resume=job.get("resume_id"))

        runs = db.list_runs(database, job_id=job_id)
        last_run = runs[-1] if runs else None
        delta = None
        if last_run and last_run.get("pct_before") is not None and last_run.get("pct_after") is not None:
            delta = last_run["pct_after"] - last_run["pct_before"]
        denials = last_run.get("permission_denials") if last_run else None

        dir_ = runner.job_dir(root, job_id, job["name"])
        # Prefer the rendered, human-readable transcript (§6.2); fall back to
        # the raw stream for a run that hasn't produced one yet.
        transcript_path = dir_ / "transcript.md"
        tail = _tail(transcript_path if transcript_path.exists() else dir_ / "stream.jsonl")

        click.echo(f"id: {job['id']}")
        click.echo(f"name: {job['name']}")
        click.echo(f"state: {job['state']}")
        click.echo(f"cwd: {job['cwd']}")
        click.echo(f"model: {job['model'] or '-'}")
        click.echo(f"conversation_id: {job['conversation_id'] or '-'}")
        click.echo(f"delta%: {delta if delta is not None else 'n/a'}")
        click.echo(f"permission denials (last run): {denials if denials is not None else 'n/a'}")
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
        click.echo(f"  notify: {_notify_display(job['notify'], cfg)}")
        click.echo(f"  timeout: {job['timeout']}")
        click.echo(f"  log: {job['log_path'] or '-'}")
        click.echo(f"  no_resume_on_limit: {bool(job['no_resume_on_limit'])}")
        click.echo(f"argv: {' '.join(argv)}")
        if transcript_path.exists():
            click.echo(f"transcript: {transcript_path}")
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
    with _session(_home(ctx)) as (database, _cfg, root):
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
@home_option
@click.pass_context
def del_(ctx: click.Context, identifier: str, home: Path | None) -> None:
    """Cancel; SIGTERM the process group if running."""
    with _session(_home(ctx, home)) as (database, _cfg, _root):
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
    with _session(_home(ctx)) as (database, _cfg, _root):
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
    with _session(_home(ctx)) as (database, _cfg, _root):
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
@home_option
@click.pass_context
def resume(ctx: click.Context, identifier: str, home: Path | None) -> None:
    """exec into ``claude --resume <conversation_id>`` in the job's cwd."""
    with _session(_home(ctx, home)) as (database, _cfg, _root):
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
    with _session(_home(ctx)) as (database, cfg, root):
        if db.get_job(database, job_id) is None:
            raise click.ClickException(f"no job with id {job_id}")
        _run_job(
            database,
            job_id,
            root=root,
            on_complete=scheduler.tick_argv(home=_home(ctx)),
            claude_cmd=_claude_cmd,
            config=cfg,
        )


# ---------------------------------------------------------------------------
# ccq tick
# ---------------------------------------------------------------------------


@main.command()
@click.option("-q", "--quiet", is_flag=True, help="Suppress the summary line (for cron, which mails any output).")
@click.pass_context
def tick(ctx: click.Context, quiet: bool) -> None:
    """Internal; run one scheduler pass -- promote due jobs and dispatch ready ones (used by cron and the run chain)."""
    home = _home(ctx)
    with _session(home) as (database, cfg, root):
        # Bind --home into the dispatch: the detached `ccq run` must come back
        # to *this* queue, not to whichever job holds that id in ~/.windowsill.
        result = scheduler.tick(
            database,
            root=root,
            config=cfg,
            now=_iso_now,
            spawn_run=partial(_spawn_run, home=home),
            probe=_probe,
        )
        # §5.3's ceiling warning survives -q on purpose: "warn loudly" is the
        # whole point, and under cron stdout is the only thing that reaches you.
        for warning in result.warnings:
            click.echo(f"warning: {warning}", err=True)
        if not quiet:
            click.echo(
                f"reaped={len(result.reaped)} expired={len(result.expired)} promoted={len(result.promoted)} "
                f"dispatched={len(result.dispatched)} skipped={len(result.skipped)} deferred={len(result.deferred)}"
            )


if __name__ == "__main__":  # pragma: no cover
    main()
