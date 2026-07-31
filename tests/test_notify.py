"""Tests for windowsill.notify -- ntfy + sendmail (design/08-interface.md §8.4).

No network and no sendmail: the HTTP POST and the sendmail invocation are
both injectable seams the tests replace with spies. The load-bearing contract
is that **a failed notification is logged and never fails the job** (§8.4), so
several tests assert that a raising transport yields a not-ok result rather
than an exception.

The second contract, added after a live run mailed the shipped placeholder
address and had it refused by the postmaster: **nothing is sent unless a
destination is configured.** Off by default, no fallback recipient, no guess.
"""

from __future__ import annotations

from typing import Any

from windowsill import notify
from windowsill.config import DEFAULTS

CONFIG: dict[str, Any] = {
    "notify": {
        "default": "email",
        "ntfy": {"url": "https://ntfy.sh", "topic": "windowsill-nightly"},
        "email": {"to": "you@real.example", "from": "noreply@real.example", "sendmail": "/usr/sbin/sendmail"},
    }
}

#: A config that turned a channel on and named nowhere to send -- i.e. the
#: shipped defaults with `default` flipped.
NO_DESTINATION: dict[str, Any] = {"notify": {"default": "email", "ntfy": {"url": "https://ntfy.sh"}, "email": {"sendmail": "/usr/sbin/sendmail"}}}


class PostSpy:
    def __init__(self, boom: bool = False) -> None:
        self.calls: list[tuple[str, bytes, dict]] = []
        self.boom = boom

    def __call__(self, endpoint: str, data: bytes, headers: dict) -> None:
        if self.boom:
            raise ConnectionError("network down")
        self.calls.append((endpoint, data, headers))


class SendmailSpy:
    def __init__(self, boom: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.boom = boom

    def __call__(self, sendmail: str, message: str) -> None:
        if self.boom:
            raise FileNotFoundError("no sendmail")
        self.calls.append((sendmail, message))


# ---------------------------------------------------------------------------
# compose (§8.4 body shape)
# ---------------------------------------------------------------------------


def test_compose_title_is_name_and_state() -> None:
    title, _ = notify.compose(job_id=42, name="refactor-auth", state="done")
    assert title == "refactor-auth: done"


def test_compose_body_has_resume_command_and_delta() -> None:
    _, body = notify.compose(job_id=42, name="refactor-auth", state="done", summary="green", delta_pct=12)
    assert "windowsill resume 42" in body
    assert "12%" in body
    assert "green" in body


def test_compose_body_omits_delta_when_absent() -> None:
    _, body = notify.compose(job_id=7, name="j", state="failed", delta_pct=None)
    assert "Δ" not in body
    assert "windowsill resume 7" in body


def test_compose_body_flags_nonzero_permission_denials() -> None:
    """§9 "Settled 2026-07-28, second sitting": a run can read "done" with
    every write refused -- the notification is where the reader would
    otherwise trust that "done" blind."""
    _, body = notify.compose(job_id=42, name="refactor-auth", state="done", permission_denials=2)
    assert "2 permission denials" in body


def test_compose_body_singular_for_one_denial() -> None:
    _, body = notify.compose(job_id=42, name="j", state="done", permission_denials=1)
    assert "1 permission denial" in body
    assert "1 permission denials" not in body


def test_compose_body_omits_permission_denials_when_zero() -> None:
    _, body = notify.compose(job_id=42, name="j", state="done", permission_denials=0)
    assert "permission denial" not in body


def test_compose_body_omits_permission_denials_when_absent() -> None:
    _, body = notify.compose(job_id=42, name="j", state="done")
    assert "permission denial" not in body


def test_compose_flags_live_background_work_on_a_blocked_job() -> None:
    """§7 "blocked": the summary above this line is the run's own closing
    message, which describes work that had not finished."""
    title, body = notify.compose(job_id=16, name="windowsill-updates-1", state="blocked", summary="Two agents are running.", live_tasks=2)
    assert title == "windowsill-updates-1: blocked"
    assert "2 background tasks still running" in body


def test_compose_body_singular_for_one_live_task() -> None:
    _, body = notify.compose(job_id=16, name="j", state="blocked", live_tasks=1)
    assert "1 background task still running" in body
    assert "1 background tasks" not in body


def test_compose_body_omits_live_tasks_when_none() -> None:
    _, body = notify.compose(job_id=42, name="j", state="done", live_tasks=0)
    assert "background task" not in body


# ---------------------------------------------------------------------------
# channel dispatch
# ---------------------------------------------------------------------------


def test_none_channel_sends_nothing() -> None:
    post, mail = PostSpy(), SendmailSpy()
    result = notify.notify("none", "t", "b", config=CONFIG, http_post=post, run_sendmail=mail)
    assert result.ok is True
    assert not post.calls and not mail.calls


def test_ntfy_posts_to_url_slash_topic_with_title_header() -> None:
    post = PostSpy()
    result = notify.notify("ntfy", "the title", "the body", config=CONFIG, http_post=post, run_sendmail=SendmailSpy())
    assert result.ok is True
    endpoint, data, headers = post.calls[0]
    assert endpoint == "https://ntfy.sh/windowsill-nightly"
    assert data == b"the body"
    assert headers.get("Title") == "the title"


def test_email_pipes_a_message_to_sendmail_dash_t() -> None:
    mail = SendmailSpy()
    result = notify.notify("email", "Subj", "Line one", config=CONFIG, http_post=PostSpy(), run_sendmail=mail)
    assert result.ok is True
    sendmail, message = mail.calls[0]
    assert sendmail == "/usr/sbin/sendmail"
    assert "To: you@real.example" in message
    assert "From: noreply@real.example" in message
    assert "Subject: Subj" in message
    assert "Line one" in message


# ---------------------------------------------------------------------------
# opt-in, and no destination is ever invented (§8.4)
# ---------------------------------------------------------------------------


def test_notify_is_off_by_default() -> None:
    """The shipped config sends nothing at all until the user opts in."""
    assert DEFAULTS["notify"]["default"] == "none"
    post, mail = PostSpy(), SendmailSpy()

    result = notify.notify(DEFAULTS["notify"]["default"], "t", "b", config={"notify": DEFAULTS["notify"]}, http_post=post, run_sendmail=mail)

    assert result.channel == "none"
    assert not post.calls and not mail.calls


def test_no_placeholder_recipient_ships_in_the_defaults() -> None:
    # The bug this guards: `you@example.org` was mailed for real and bounced.
    assert "to" not in DEFAULTS["notify"]["email"]
    assert "topic" not in DEFAULTS["notify"]["ntfy"]


def test_email_with_no_recipient_anywhere_sends_nothing() -> None:
    mail = SendmailSpy()

    result = notify.notify("email", "t", "b", config=NO_DESTINATION, run_sendmail=mail, http_post=PostSpy())

    assert not mail.calls
    assert result.ok is False
    assert result.skipped is True
    assert "notify.email.to" in result.detail


def test_ntfy_with_no_topic_anywhere_sends_nothing() -> None:
    """A default ntfy topic would be a *public* one -- same bug, louder."""
    post = PostSpy()

    result = notify.notify("ntfy", "t", "b", config=NO_DESTINATION, http_post=post, run_sendmail=SendmailSpy())

    assert not post.calls
    assert result.skipped is True


def test_email_recipient_from_the_global_config_is_used() -> None:
    mail = SendmailSpy()

    result = notify.notify("email", "t", "b", config=CONFIG, run_sendmail=mail, http_post=PostSpy())

    assert result.ok is True
    assert "To: you@real.example" in mail.calls[0][1]


def test_email_recipient_from_the_job_file_alone_is_enough() -> None:
    """§2.3 `--notify email:<address>`: the global config names nobody."""
    mail = SendmailSpy()

    result = notify.notify("email:me@real.example", "t", "b", config=NO_DESTINATION, run_sendmail=mail, http_post=PostSpy())

    assert result.ok is True
    assert result.channel == "email"
    assert "To: me@real.example" in mail.calls[0][1]


def test_job_file_recipient_overrides_the_global_config() -> None:
    mail = SendmailSpy()

    notify.notify("email:job@real.example", "t", "b", config=CONFIG, run_sendmail=mail, http_post=PostSpy())

    message = mail.calls[0][1]
    assert "To: job@real.example" in message
    assert "you@real.example" not in message


def test_job_file_topic_overrides_the_global_ntfy_topic() -> None:
    post = PostSpy()

    notify.notify("ntfy:job-topic", "t", "b", config=CONFIG, http_post=post, run_sendmail=SendmailSpy())

    assert post.calls[0][0] == "https://ntfy.sh/job-topic"


def test_email_omits_the_from_header_when_unconfigured() -> None:
    """No placeholder sender either: unset, sendmail stamps the local user."""
    mail = SendmailSpy()

    notify.notify("email:me@real.example", "Subj", "b", config=NO_DESTINATION, run_sendmail=mail, http_post=PostSpy())

    message = mail.calls[0][1]
    assert "From:" not in message
    assert "To: me@real.example" in message
    assert "Subject: Subj" in message


def test_split_channel_splits_channel_from_destination() -> None:
    assert notify.split_channel("email:me@real.example") == ("email", "me@real.example")
    assert notify.split_channel("email") == ("email", None)
    assert notify.split_channel("none") == ("none", None)
    assert notify.split_channel(None) == ("none", None)
    assert notify.split_channel("") == ("none", None)
    assert notify.split_channel("  email : me@real.example ") == ("email", "me@real.example")


def test_destination_never_guesses() -> None:
    assert notify.destination("email", NO_DESTINATION) is None
    assert notify.destination("email", {}) is None
    assert notify.destination("carrier-pigeon", CONFIG) is None
    assert notify.destination("email", CONFIG) == "you@real.example"
    assert notify.destination("email", CONFIG, "override@real.example") == "override@real.example"


# ---------------------------------------------------------------------------
# failure never raises (§8.4)
# ---------------------------------------------------------------------------


def test_ntfy_failure_is_reported_not_raised() -> None:
    result = notify.notify("ntfy", "t", "b", config=CONFIG, http_post=PostSpy(boom=True), run_sendmail=SendmailSpy())
    assert result.ok is False
    assert "network down" in result.detail


def test_email_failure_is_reported_not_raised() -> None:
    result = notify.notify("email", "t", "b", config=CONFIG, http_post=PostSpy(), run_sendmail=SendmailSpy(boom=True))
    assert result.ok is False
    assert result.channel == "email"


def test_unknown_channel_is_not_ok() -> None:
    result = notify.notify("carrier-pigeon", "t", "b", config=CONFIG, http_post=PostSpy(), run_sendmail=SendmailSpy())
    assert result.ok is False
