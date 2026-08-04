"""windowsill command line interface (design/08-interface.md §8.1).

Thin glue over the already-built sibling modules -- this module owns no
parsing, execution, or scheduling logic of its own:

- :mod:`windowsill.config` -- ``~/.windowsill/config.toml``.
- :mod:`windowsill.db` -- the SQLite state layer and identifier resolution.
- :mod:`windowsill.jobfile` -- job-file directive parsing.
- :mod:`windowsill.runner` -- argv building and the job-directory layout.
- :mod:`windowsill.scheduler` -- the ``windowsill tick`` dispatch step.

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

This module is imported on *every* ``windowsill`` invocation -- and will soon
be imported on every TAB press too, once Click shell completion ships (it
re-invokes the program per keystroke to list completions). ``sqlite_utils``
and the sibling ``windowsill`` modules above are not free to import
(``sqlite_utils`` alone costs tens of ms; ``windowsill.runner`` and
``windowsill.scheduler`` each transitively pull in ``windowsill.notify``'s
``urllib.request`` and ``windowsill.usage``'s ``zoneinfo``), so none of them
are imported here at module scope. Instead, every command (and helper)
imports exactly what it needs at the top of its own body -- a plain
``from windowsill import db`` costs nothing once something else has already
imported it, so the repetition below is not wasted work. The three
production defaults that used to be bound directly (``_spawn_run``,
``_run_job``, ``_probe``) are ``None`` sentinels for the same reason; the
``_resolve_*`` functions next to them are where the deferred import and the
"nothing monkeypatched it" fallback both happen.
"""

from __future__ import annotations

import os
import signal
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Mapping

import click

if TYPE_CHECKING:
    # Only used in type annotations, which `from __future__ import
    # annotations` above turns into unevaluated strings -- a real import
    # here would undo the whole point of deferring everything else (see
    # module docstring).
    import sqlite_utils

    from windowsill import config

# ---------------------------------------------------------------------------
# injectable seams -- see module docstring. Production defaults here;
# tests monkeypatch the module attribute directly.
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _default_terminate(pid: int) -> None:
    """SIGTERM a process group (§8.1 ``windowsill del``: "SIGTERM the process
    group if running"). Best-effort -- a pid that's already gone is not
    an error."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


_terminate = _default_terminate

#: The claude invocation prefix ``windowsill resume`` execs into. Same seam name
#: as ``windowsill.runner``'s ``claude_cmd``.
_claude_cmd: tuple[str, ...] = ("claude",)


def _default_exec(argv: list[str], cwd: str) -> None:
    """``exec`` into ``argv`` after ``chdir``-ing to ``cwd`` (§8.1
    ``windowsill resume``). Replaces the current process image -- never returns
    on success."""
    os.chdir(cwd)
    os.execvp(argv[0], argv)


_exec = _default_exec

#: Production ``spawn_run`` handed to :func:`windowsill.scheduler.tick` -- Popens
#: ``windowsill run <id>`` detached. ``None`` means "nothing has monkeypatched this
#: yet"; binding ``scheduler._default_spawn_run`` here directly (as before)
#: would force importing :mod:`windowsill.scheduler` at module load (see
#: module docstring). ``_resolve_spawn_run`` below does the deferred import
#: and picks the real default when this is still ``None``. Tests replace
#: ``_spawn_run`` wholesale with a spy so no real process is ever spawned.
_spawn_run = None


def _resolve_spawn_run():
    if _spawn_run is not None:
        return _spawn_run
    from windowsill import scheduler  # deferred: see module docstring

    return scheduler._default_spawn_run


#: The per-job run entrypoint ``windowsill run`` drives (:func:`windowsill.runner.run_job`).
#: Same ``None``-sentinel seam style as ``_spawn_run``: tests monkeypatch it
#: so a fake ``claude`` (via ``_claude_cmd``) is spawned instead of the real
#: binary, and no chained ``windowsill tick`` is ever Popened.
_run_job = None


def _resolve_run_job():
    if _run_job is not None:
        return _run_job
    from windowsill import runner  # deferred: see module docstring

    return runner.run_job


#: The ``/usage`` oracle handed to :func:`windowsill.scheduler.tick` (§4) and read
#: directly by ``windowsill usage --probe``. Same ``None``-sentinel seam style
#: again: tests replace it so no real ``claude`` is ever probed.
_probe = None


def _resolve_probe():
    if _probe is not None:
        return _probe
    from windowsill import runner  # deferred: see module docstring

    return runner._default_probe


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
    ``windowsill --home X stat`` and ``ccstat --home X`` land on the same queue.
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
    from windowsill import config, db  # deferred: see module docstring

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
    from windowsill import db  # deferred: see module docstring

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
    from windowsill import notify  # deferred: see module docstring

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
# windowsill sub
# ---------------------------------------------------------------------------


@main.command()
@click.argument("file", type=click.File("r"))
@click.option("--now", "run_now", is_flag=True, help="Bypass --at; eligible as soon as a tick runs.")
@home_option
@click.pass_context
def sub(ctx: click.Context, file, run_now: bool, home: Path | None) -> None:
    """Submit a job file. ``-`` reads stdin. Prints the new job id."""
    from windowsill import db, jobfile, runner  # deferred: see module docstring

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
            warm=d.warm,
            warm_lead=d.warm_lead,
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
# windowsill stat
# ---------------------------------------------------------------------------


@main.command()
@click.option("-a", "--all", "show_all", is_flag=True, help="Include terminal jobs from the last 7 days.")
@click.option("--with-timezone", is_flag=True, help="Show times with their UTC offset instead of bare system local time.")
@home_option
@click.pass_context
def stat(ctx: click.Context, show_all: bool, with_timezone: bool, home: Path | None) -> None:
    """id, name, state, model, when, cwd, elapsed. Plus window state and 30d count."""
    from windowsill import db  # deferred: see module docstring

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
# windowsill usage
# ---------------------------------------------------------------------------

#: §4.2: "Rate-limit to 1 probe per 10s". A probe is free but not polite.
PROBE_MIN_INTERVAL_SECONDS = 10


@main.command("usage")
@click.option("--probe", "force_probe", is_flag=True, help="Read /usage now instead of the cached window state.")
@click.option("--with-timezone", is_flag=True, help="Show times with their UTC offset instead of bare system local time.")
@click.pass_context
def usage_(ctx: click.Context, force_probe: bool, with_timezone: bool) -> None:
    """Cached window state. ``--probe`` forces a fresh read."""
    from windowsill import db  # deferred: see module docstring

    with _session(_home(ctx)) as (database, cfg, _root):
        now_str = _iso_now()
        window_row = db.current_window(database, now_str)

        if force_probe:
            # §4.2: cheap, but not free of manners -- refuse to hammer it.
            last = (window_row or {}).get("probed_at")
            if last and (_parse_iso(now_str) - _parse_iso(last)).total_seconds() < PROBE_MIN_INTERVAL_SECONDS:
                raise click.ClickException(f"probed less than {PROBE_MIN_INTERVAL_SECONDS}s ago; showing cache is `windowsill usage`")
            from windowsill import window  # deferred: only needed when actually probing

            parsed = _resolve_probe()()
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
# windowsill report
# ---------------------------------------------------------------------------


def _since_cutoff(now_str: str, since: str) -> str:
    """``--since 24h`` as an absolute timestamp comparable with the DB's."""
    from windowsill import runner  # deferred: see module docstring

    seconds = runner.parse_timeout_seconds(since)
    if seconds is None:
        raise click.ClickException(f"unparseable --since: {since!r}")
    return (_parse_iso(now_str) - timedelta(seconds=seconds)).isoformat()


def _delta_pct(database: sqlite_utils.Database, job_id: int) -> int | None:
    """Δ window % of a job's last run (§4.3), when both probes landed."""
    from windowsill import db  # deferred: see module docstring

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
    from windowsill import db  # deferred: see module docstring

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
    from windowsill import db  # deferred: see module docstring

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
                f"{job['id']:<5} {job['name']:<24} {job['state']:<10} {_elapsed(job, now_str):<9} "
                f"{delta_text:<7} windowsill resume {job['id']}{denial_text}"
            )

        if not jobs:
            click.echo("(nothing ran)")

        click.echo("")
        window_row = db.current_window(database, now_str)
        window_text = "closed" if window_row is None else f"open until {_local(window_row.get('reset_at'), with_timezone=with_timezone)}"
        click.echo(f"{len(jobs)} jobs · Δ {total_delta}% · window: {window_text} · {_window_count_30d(database, now_str)} windows in 30d")


# ---------------------------------------------------------------------------
# windowsill show
# ---------------------------------------------------------------------------


@main.command()
@click.argument("identifier")
@click.pass_context
def show(ctx: click.Context, identifier: str) -> None:
    """Directives, resolved argv, state, conversation id, delta%, transcript tail."""
    from windowsill import db, runner  # deferred: see module docstring

    with _session(_home(ctx)) as (database, cfg, root):
        job_id = _resolve(database, identifier)
        job = db.get_job(database, job_id)
        runs = db.list_runs(database, job_id=job_id)
        dir_ = runner.job_dir(root, job_id, job["name"])
        # §3.3/§3.2: reflect the same allowed_tools *and* system-prompt
        # default/yield behaviour run_job actually applies, so "resolved argv"
        # here matches what would dispatch. `runs` is every run so far, which
        # for a job about to be re-dispatched is exactly the `prior_runs` the
        # next dispatch would see -- so the re-execution line shows up here
        # before it is ever sent.
        prompt_plan = runner.resolve_system_prompt(
            job["passthrough"],
            config=cfg,
            job_dir=dir_,
            is_anchor=bool(job["is_anchor"]),
            prior_runs=runs,
            resume_id=job.get("resume_id"),
        )
        argv = runner.build_argv(
            job["passthrough"],
            resume=job.get("resume_id"),
            allowed_tools=cfg["safety"]["allowed_tools"],
            system_prompt=prompt_plan.text,
            add_dirs=[str(prompt_plan.add_dir)] if prompt_plan.add_dir else (),
        )
        # §8.1's one-screen constraint: windowsill's own prompt is ~950
        # characters of prose a reader already knows the content of. Elide it
        # to its length -- the flag being *there*, and the --add-dir beside
        # it, is what they came to check. A job's own prompt is never elided:
        # that one they did not write in this repo.
        if prompt_plan.text:
            argv = [f"<windowsill system prompt, {len(prompt_plan.text)} chars>" if a == prompt_plan.text else a for a in argv]

        last_run = runs[-1] if runs else None
        delta = None
        if last_run and last_run.get("pct_before") is not None and last_run.get("pct_after") is not None:
            delta = last_run["pct_after"] - last_run["pct_before"]
        denials = last_run.get("permission_denials") if last_run else None

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
        # Not a directive -- the scheduler writes it (§5.5) -- so it sits up
        # here with the other run state rather than under `directives:`. It is
        # the answer to "my --warm job has been ready for an hour, is anything
        # actually happening": a job id to `windowsill show` in turn.
        click.echo(f"warmed_by: {job['warmed_by'] or '-'}")
        click.echo(f"delta%: {delta if delta is not None else 'n/a'}")
        click.echo(f"permission denials (last run): {denials if denials is not None else 'n/a'}")
        # §3.2: whether the job actually kept the journal windowsill asked for.
        # "absent" on a finished job is the signal that the prompt is not
        # landing -- the thing worth knowing before the next overnight run.
        click.echo(f"journal: {'present' if runner.read_journal(dir_) else 'absent'}")
        click.echo("directives:")
        click.echo(f"  window: {job['window_policy']}")
        click.echo(f"  anchor: {bool(job['is_anchor'])}")
        click.echo(f"  warm: {bool(job['warm'])}")
        # The effective lead, not the raw column: the column is NULL for every
        # job that didn't override it, and printing "-" there would read as
        # "no lead" when the real answer is whatever `[warm].lead` says --
        # which is the number the reader is checking before they trust the
        # job to start at 02:30 rather than 22:00.
        warm_lead = job["warm_lead"] or f"{cfg['warm']['lead']} (from [warm].lead)"
        click.echo(f"  warm_lead: {warm_lead}")
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
# windowsill log
# ---------------------------------------------------------------------------


@main.command()
@click.argument("identifier")
@click.option("-f", "--follow", is_flag=True, help="Follow a running job's transcript.")
@click.pass_context
def log(ctx: click.Context, identifier: str, follow: bool) -> None:
    """Stream the transcript; -f follows a running job."""
    from windowsill import db, runner  # deferred: see module docstring

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
# windowsill del
# ---------------------------------------------------------------------------


@main.command("del")
@click.argument("identifier")
@home_option
@click.pass_context
def del_(ctx: click.Context, identifier: str, home: Path | None) -> None:
    """Cancel; SIGTERM the process group if running."""
    from windowsill import db  # deferred: see module docstring

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
        db.log_event(database, ts=now_str, job_id=job_id, kind="cancelled", detail="windowsill del")
        click.echo(f"cancelled {job_id}")


# ---------------------------------------------------------------------------
# windowsill hold / windowsill release
# ---------------------------------------------------------------------------


@main.command()
@click.argument("identifier")
@click.pass_context
def hold(ctx: click.Context, identifier: str) -> None:
    """Hold a queued/ready job; needs ``windowsill release``."""
    from windowsill import db  # deferred: see module docstring

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
        db.log_event(database, ts=now_str, job_id=job_id, kind="held", detail="windowsill hold")
        click.echo(f"held {job_id}")


@main.command()
@click.argument("identifier")
@click.pass_context
def release(ctx: click.Context, identifier: str) -> None:
    """Release a held job back to ``queued``."""
    from windowsill import db  # deferred: see module docstring

    with _session(_home(ctx)) as (database, _cfg, _root):
        job_id = _resolve(database, identifier)
        job = db.get_job(database, job_id)

        if job["state"] != "held":
            raise click.ClickException(f"job {job_id} is not held (state {job['state']!r})")

        now_str = _iso_now()
        db.update_job(database, job_id, state="queued")
        db.log_event(database, ts=now_str, job_id=job_id, kind="queued", detail="windowsill release")
        click.echo(f"released {job_id}")


# ---------------------------------------------------------------------------
# windowsill resume
# ---------------------------------------------------------------------------


@main.command()
@click.argument("identifier")
@home_option
@click.pass_context
def resume(ctx: click.Context, identifier: str, home: Path | None) -> None:
    """exec into ``claude --resume <conversation_id>`` in the job's cwd."""
    from windowsill import db  # deferred: see module docstring

    with _session(_home(ctx, home)) as (database, _cfg, _root):
        job_id = _resolve(database, identifier)
        job = db.get_job(database, job_id)

        conversation_id = job.get("conversation_id")
        if not conversation_id:
            raise click.ClickException(f"job {job_id} has no conversation to resume")

        argv = [*_claude_cmd, "--resume", conversation_id]
        _exec(argv, job["cwd"])


# ---------------------------------------------------------------------------
# windowsill run
# ---------------------------------------------------------------------------


@main.command("run")
@click.argument("job_id", type=int)
@click.pass_context
def run_(ctx: click.Context, job_id: int) -> None:
    """Internal; execute one dispatched job to completion (spawned by tick)."""
    from windowsill import db, scheduler  # deferred: see module docstring

    with _session(_home(ctx)) as (database, cfg, root):
        if db.get_job(database, job_id) is None:
            raise click.ClickException(f"no job with id {job_id}")
        _resolve_run_job()(
            database,
            job_id,
            root=root,
            on_complete=scheduler.tick_argv(home=_home(ctx)),
            claude_cmd=_claude_cmd,
            config=cfg,
        )


# ---------------------------------------------------------------------------
# windowsill tick
# ---------------------------------------------------------------------------


@main.command()
@click.option("-q", "--quiet", is_flag=True, help="Suppress the summary line (for cron, which mails any output).")
@click.pass_context
def tick(ctx: click.Context, quiet: bool) -> None:
    """Internal; run one scheduler pass -- promote due jobs and dispatch ready ones (used by cron and the run chain)."""
    from windowsill import scheduler  # deferred: see module docstring

    home = _home(ctx)
    with _session(home) as (database, cfg, root):
        # Bind --home into the dispatch: the detached `windowsill run` must come back
        # to *this* queue, not to whichever job holds that id in ~/.windowsill.
        result = scheduler.tick(
            database,
            root=root,
            config=cfg,
            now=_iso_now,
            spawn_run=partial(_resolve_spawn_run(), home=home),
            probe=_resolve_probe(),
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


# ---------------------------------------------------------------------------
# windowsill completions
# ---------------------------------------------------------------------------


@main.group("completions")
def completions_group() -> None:
    """Shell completion: emit a script, or install one for every entry point."""


def _emit_completion(shell: str) -> None:
    from windowsill import completions  # deferred: see module docstring

    click.echo(completions.render(shell, "windowsill"))


@completions_group.command("bash")
def completions_bash() -> None:
    """Emit the bash completion script for ``windowsill`` to stdout."""
    _emit_completion("bash")


@completions_group.command("zsh")
def completions_zsh() -> None:
    """Emit the zsh completion script for ``windowsill`` to stdout."""
    _emit_completion("zsh")


@completions_group.command("fish")
def completions_fish() -> None:
    """Emit the fish completion script for ``windowsill`` to stdout."""
    _emit_completion("fish")


@completions_group.command("install")
@click.option(
    "--shell",
    "shell_opt",
    default="auto",
    help="bash, zsh, fish, or auto to detect from $SHELL.",
)
def completions_install(shell_opt: str) -> None:
    """Detect the shell, and write completion files for all five entry points.

    Deliberately has no ``--home``: that option means the windowsill *state*
    directory everywhere else in this CLI, a different thing from the real
    ``$HOME`` completion files belong under -- tests fake the latter by
    setting the ``HOME`` env var, not by threading a flag through here.
    """
    from windowsill import completions  # deferred: see module docstring

    shell = completions.detect_shell() if shell_opt == "auto" else shell_opt
    if shell not in completions.SHELLS:
        raise click.ClickException(f"unsupported shell: {shell_opt!r} (expected bash, zsh, fish, or auto)")

    result = completions.install(shell, Path.home())
    for path in result.written:
        click.echo(f"wrote {path}")
    for link, target in result.linked:
        click.echo(f"linked {link} -> {target.name}")
    click.echo(result.note)


if __name__ == "__main__":  # pragma: no cover
    main()
