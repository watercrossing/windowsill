"""Notifications: ntfy and email (design/08-interface.md §8.4).

Exactly two channels. **ntfy** POSTs to ``<url>/<topic>`` with the job name +
state as the title and a one-line body carrying the result, the Δ window %,
and the ``ccq resume <id>`` command that is the morning-after path. **email**
pipes an RFC-822 message to ``sendmail -t`` from the configured noreply
address. Nothing else.

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
    ``detail`` explains a failure (or the skip reason for ``none``)."""

    ok: bool
    channel: str
    detail: str = ""


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
# transports (injectable; stdlib defaults)
# ---------------------------------------------------------------------------


def _default_http_post(endpoint: str, data: bytes, headers: dict) -> None:
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 -- configured ntfy url
        response.read()


def _default_run_sendmail(sendmail: str, message: str) -> None:
    subprocess.run([sendmail, "-t"], input=message, text=True, check=True, timeout=20)


def _send_ntfy(cfg: Mapping[str, Any], title: str, body: str, *, http_post: Callable[[str, bytes, dict], None]) -> None:
    endpoint = cfg["url"].rstrip("/") + "/" + str(cfg["topic"]).lstrip("/")
    http_post(endpoint, body.encode("utf-8"), {"Title": title})


def _send_email(cfg: Mapping[str, Any], title: str, body: str, *, run_sendmail: Callable[[str, str], None]) -> None:
    message = f"To: {cfg['to']}\nFrom: {cfg['from']}\nSubject: {title}\n\n{body}\n"
    run_sendmail(cfg["sendmail"], message)


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
    ``"email"``; the relevant ``[notify.<channel>]`` sub-table is read from
    ``config``. Any transport failure -- unreachable ntfy, missing sendmail --
    comes back as ``ok=False`` with the reason in ``detail``.
    """
    if channel in (None, "", "none"):
        return NotifyResult(True, "none", "no channel")

    notify_cfg = config.get("notify", {})
    try:
        if channel == "ntfy":
            _send_ntfy(notify_cfg["ntfy"], title, body, http_post=http_post)
        elif channel == "email":
            _send_email(notify_cfg["email"], title, body, run_sendmail=run_sendmail)
        else:
            return NotifyResult(False, channel, f"unknown channel {channel!r}")
    except Exception as exc:  # noqa: BLE001 -- a notification must never fail the job (§8.4)
        return NotifyResult(False, channel, f"{type(exc).__name__}: {exc}")
    return NotifyResult(True, channel)
