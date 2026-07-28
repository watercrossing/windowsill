"""Tests for windowsill.notify -- ntfy + sendmail (design/08-interface.md §8.4).

No network and no sendmail: the HTTP POST and the sendmail invocation are
both injectable seams the tests replace with spies. The load-bearing contract
is that **a failed notification is logged and never fails the job** (§8.4), so
several tests assert that a raising transport yields a not-ok result rather
than an exception.
"""

from __future__ import annotations

from typing import Any

from windowsill import notify

CONFIG: dict[str, Any] = {
    "notify": {
        "default": "email",
        "ntfy": {"url": "https://ntfy.sh", "topic": "ccq-nightly"},
        "email": {"to": "you@example.org", "from": "noreply@example.org", "sendmail": "/usr/sbin/sendmail"},
    }
}


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
    assert "ccq resume 42" in body
    assert "12%" in body
    assert "green" in body


def test_compose_body_omits_delta_when_absent() -> None:
    _, body = notify.compose(job_id=7, name="j", state="failed", delta_pct=None)
    assert "Δ" not in body
    assert "ccq resume 7" in body


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
    assert endpoint == "https://ntfy.sh/ccq-nightly"
    assert data == b"the body"
    assert headers.get("Title") == "the title"


def test_email_pipes_a_message_to_sendmail_dash_t() -> None:
    mail = SendmailSpy()
    result = notify.notify("email", "Subj", "Line one", config=CONFIG, http_post=PostSpy(), run_sendmail=mail)
    assert result.ok is True
    sendmail, message = mail.calls[0]
    assert sendmail == "/usr/sbin/sendmail"
    assert "To: you@example.org" in message
    assert "From: noreply@example.org" in message
    assert "Subject: Subj" in message
    assert "Line one" in message


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
