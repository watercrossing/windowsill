"""Notifications: ntfy and email (design/08-interface.md §8.4).

Exactly two channels. **ntfy** POSTs to ``<url>/<topic>`` with the job name +
state as the title and a one-line body carrying the result, the Δ window %,
and the ``ccq resume <id>`` command that is the morning-after path. **email**
pipes an RFC-822 message to ``sendmail -t`` from the configured noreply
address. Nothing else.

Notifications are **opt-in and never guessed** (§8.4). Nothing is sent unless
``[notify].default`` or a job's ``--notify`` names a channel, and a named
channel with no destination configured sends nothing rather than falling back
to a placeholder: there is no default recipient and none is derived from
``$USER``/hostname. A guessed address either bounces or reaches a stranger --
the placeholder this module used to ship was mailed for real and refused by
the postmaster. That case is a *skip*, not a failure of the transport, and
:class:`NotifyResult` distinguishes them so the caller can record it (§8.4:
logged, never silent, never fatal).

The non-negotiable contract (§8.4): **a failed notification is logged and
never fails the job.** So :func:`notify` catches everything a transport can
throw and returns a :class:`NotifyResult` the caller records to ``events`` --
it never raises.

Both transports are injectable seams (``http_post``, ``run_sendmail``) with
stdlib defaults (:mod:`urllib.request`, ``sendmail`` via :mod:`subprocess`),
so tests exercise composition and the failure contract without a network or a
mailer.
"""

from __future__ import annotations

import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass
class NotifyResult:
    """Outcome of one notification attempt. ``ok`` is whether it was sent;
    ``detail`` explains a failure (or the skip reason for ``none``).

    ``skipped`` marks the one case that is neither: the channel was on but had
    nowhere to send to, so nothing was attempted and nothing failed (§8.4).
    """

    ok: bool
    channel: str
    detail: str = ""
    skipped: bool = False


# ---------------------------------------------------------------------------
# message composition (§8.4)
# ---------------------------------------------------------------------------


def compose(
    *,
    job_id: int,
    name: str,
    state: str,
    summary: str | None = None,
    delta_pct: int | None = None,
    permission_denials: int | None = None,
) -> tuple[str, str]:
    """Build the ``(title, body)`` for a job's completion notification.

    Title is ``"<name>: <state>"``; body is the one-line result, the Δ window %
    when known, and ``ccq resume <id>`` -- each on its own line so the resume
    command is tappable.

    ``permission_denials`` (design/09-findings.md, "Settled 2026-07-28, second
    sitting") only earns a line when non-zero: a run can read ``state ==
    "done"`` while having written nothing because every ``Write``/``Edit`` was
    refused, and that is exactly the case the morning-after reader must not
    breeze past.
    """
    title = f"{name}: {state}"
    lines: list[str] = []
    if summary:
        lines.append(summary)
    if delta_pct is not None:
        lines.append(f"Δ {delta_pct}%")
    if permission_denials:
        lines.append(f"{permission_denials} permission denial{'s' if permission_denials != 1 else ''}")
    lines.append(f"ccq resume {job_id}")
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# where a notification goes (§8.4: configured or not at all)
# ---------------------------------------------------------------------------

#: The ``[notify.<channel>]`` key naming *where* that channel sends. Neither
#: has a default (see :mod:`windowsill.config`), which is why every send goes
#: through :func:`destination` and why ``None`` there means "send nothing".
DESTINATION_KEY: dict[str, str] = {"email": "to", "ntfy": "topic"}


def split_channel(value: str | None) -> tuple[str, str | None]:
    """Split a ``--notify`` value into ``(channel, destination override)``.

    §2.3's ``--notify`` names a channel and may append ``:<destination>`` --
    the recipient for ``email``, the topic for ``ntfy``. One grammar for both,
    because a job file that carries its own address is the only way to be
    notified about *this* job on a machine whose global config names nobody:
    ``#$ --notify email:me@example.com``.

    An empty or absent value is the ``"none"`` channel, so "no notify
    configured" and "notify explicitly off" travel the same path.
    """
    if not value:
        return "none", None
    channel, _, destination = value.strip().partition(":")
    return channel.strip() or "none", destination.strip() or None


def destination(channel: str, config: Mapping[str, Any], override: str | None = None) -> str | None:
    """Where ``channel`` would actually send, or ``None`` if nowhere (§8.4).

    An ``override`` from the job file wins over ``[notify.<channel>]`` in the
    global config. There is no further fallback: not a placeholder, not
    ``$USER@$(hostname)``. ``None`` is the answer, and the caller sends nothing.
    """
    if override:
        return override
    key = DESTINATION_KEY.get(channel)
    if key is None:
        return None
    value = (config.get("notify", {}) or {}).get(channel, {}) or {}
    resolved = value.get(key)
    return str(resolved) if resolved else None


# ---------------------------------------------------------------------------
# transports (injectable; stdlib defaults)
# ---------------------------------------------------------------------------


def _default_http_post(endpoint: str, data: bytes, headers: dict) -> None:
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 -- configured ntfy url
        response.read()


def _default_run_sendmail(sendmail: str, message: str) -> None:
    subprocess.run([sendmail, "-t"], input=message, text=True, check=True, timeout=20)


def _send_ntfy(cfg: Mapping[str, Any], title: str, body: str, *, topic: str, http_post: Callable[[str, bytes, dict], None]) -> None:
    endpoint = cfg["url"].rstrip("/") + "/" + topic.lstrip("/")
    http_post(endpoint, body.encode("utf-8"), {"Title": title})


def _send_email(cfg: Mapping[str, Any], title: str, body: str, *, to: str, run_sendmail: Callable[[str, str], None]) -> None:
    # `From` is optional: unset, sendmail stamps the invoking user's own
    # address, which is the one address on the machine guaranteed to exist --
    # strictly better than a placeholder domain an MTA may refuse outright.
    headers = [f"To: {to}"]
    sender = cfg.get("from")
    if sender:
        headers.append(f"From: {sender}")
    headers.append(f"Subject: {title}")
    run_sendmail(cfg["sendmail"], "\n".join(headers) + f"\n\n{body}\n")


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------


def notify(
    channel: str | None,
    title: str,
    body: str,
    *,
    config: Mapping[str, Any],
    http_post: Callable[[str, bytes, dict], None] = _default_http_post,
    run_sendmail: Callable[[str, str], None] = _default_run_sendmail,
) -> NotifyResult:
    """Send one notification on ``channel``. Never raises (§8.4).

    ``channel`` is ``"none"`` (or empty/``None``) to skip, ``"ntfy"``, or
    ``"email"``, each optionally carrying its own destination as
    ``"<channel>:<destination>"`` (:func:`split_channel`); the relevant
    ``[notify.<channel>]`` sub-table is read from ``config``. A channel with no
    destination anywhere sends nothing and comes back ``skipped``. Any
    transport failure -- unreachable ntfy, missing sendmail -- comes back as
    ``ok=False`` with the reason in ``detail``.
    """
    name, override = split_channel(channel)
    if name == "none":
        return NotifyResult(True, "none", "no channel")

    notify_cfg = config.get("notify", {}) or {}
    where = destination(name, config, override)
    if name in DESTINATION_KEY and where is None:
        # Notify is on but nothing says where to. Sending nothing is the only
        # safe answer (§8.4); saying so is the caller's job.
        key = DESTINATION_KEY[name]
        return NotifyResult(False, name, f"no destination configured (notify.{name}.{key} or --notify {name}:<{key}>)", skipped=True)

    try:
        if name == "ntfy":
            _send_ntfy(notify_cfg.get("ntfy", {}) or {}, title, body, topic=str(where), http_post=http_post)
        elif name == "email":
            _send_email(notify_cfg.get("email", {}) or {}, title, body, to=str(where), run_sendmail=run_sendmail)
        else:
            return NotifyResult(False, name, f"unknown channel {name!r}")
    except Exception as exc:  # noqa: BLE001 -- a notification must never fail the job (§8.4)
        return NotifyResult(False, name, f"{type(exc).__name__}: {exc}")
    return NotifyResult(True, name)
