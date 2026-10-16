"""Second security review: a reply goes only to addresses the allowlist checked as SMTP will use them, and one
hostile mail (nested a thousand levels deep) never breaks listing or reading the mailbox. Tools through the real
dispatch with the fake IMAP/SMTP of test_plugin_mail_imap."""
from __future__ import annotations

import base64
import email.utils

import pytest

from plugins.mail import message as mails
from plugins.mail.tests.test_plugin_mail_imap import FakeIMAP, FakeSMTP, call, imap, make_mail, server  # noqa: F401


def _envelope(message) -> list[str]:
    """The recipients smtplib.send_message derives from the headers."""
    fields = [f for f in (message["To"], message["Cc"], message["Bcc"]) if f is not None]
    return [address for _name, address in email.utils.getaddresses(fields)]


async def test_an_encoded_reply_to_cannot_smuggle_a_second_recipient_past_a_domain_rule(imap):
    word = "=?utf-8?b?" + base64.b64encode(b"attacker@evil.com, friend@example.org").decode() + "?="
    raw = make_mail(sender="friend@example.org").replace(
        b"From: friend@example.org", b"From: friend@example.org\nReply-To: " + word.encode())
    FakeIMAP.mails = {1: raw}
    result, _line = await call(server(reply_to=["@example.org"]), "reply", {"id": 1, "body": "ok"})
    sent = [a for session in FakeSMTP.sessions for a in _envelope(session.message)]
    assert "attacker@evil.com" not in sent, (result, sent)
    assert result.get("status") == "error" and "names no sender address" in result["error"], result


async def test_a_plain_reply_to_under_a_domain_rule_is_still_answered(imap):
    FakeIMAP.mails = {1: make_mail(sender="friend@example.org", reply_to="Desk <desk@example.org>")}
    result, _line = await call(server(reply_to=["@example.org"]), "reply", {"id": 1, "body": "ok"})
    assert result["status"] == "success" and result["sent_to"] == ["desk@example.org"], result
    assert _envelope(FakeSMTP.sessions[-1].message) == ["desk@example.org"]


async def test_a_reply_address_that_the_written_header_changes_is_not_sent(imap):
    # parses back as itself, but writing the To header re-quotes it: smtplib would mail an address nobody checked
    word = "=?utf-8?b?" + base64.b64encode(b"x\\@example.org").decode() + "?="
    raw = make_mail(sender="friend@example.org").replace(
        b"From: friend@example.org", b"From: friend@example.org\nReply-To: " + word.encode())
    FakeIMAP.mails = {1: raw}
    result, _line = await call(server(reply_to=["@example.org"]), "reply", {"id": 1, "body": "ok"})
    assert not FakeSMTP.sessions, [_envelope(s.message) for s in FakeSMTP.sessions]
    assert result.get("status") == "error" and "names no sender address" in result["error"], result


def _nested(depth: int) -> bytes:
    head = b"From: x@evil.example\r\nSubject: deep\r\nContent-Type: multipart/mixed; boundary=b0\r\n\r\n"
    body = b"".join(b"--b%d\r\nContent-Type: multipart/mixed; boundary=b%d\r\n\r\n" % (i, i + 1) for i in range(depth))
    return head + body + b"--b%d\r\nContent-Type: application/pdf\r\n\r\nhi\r\n" % depth


async def test_one_mail_nested_too_deep_is_listed_with_its_headers_and_never_breaks_the_inbox(imap):
    FakeIMAP.mails = {1: make_mail(), 2: _nested(1200)}
    s = server()
    result, _line = await call(s, "inbox", {})
    assert result["status"] == "success", result
    deep = next(m for m in result["mails"] if m["id"] == 2)
    assert deep["from"] == "x@evil.example" and deep["subject"] == "deep", deep
    assert deep["snippet"].startswith("(This mail could not be read") and deep["attachments"] == 0, deep
    assert [m["id"] for m in result["mails"]] == [2, 1]

    read, _line = await call(s, "read", {"id": 2})
    assert read["status"] == "success" and read["text"] == mails.UNREADABLE and read["attachments"] == [], read
    again, _line = await call(s, "inbox", {})
    assert [m["id"] for m in again["mails"]] == [1], again        # opened: unread_only leaves it out
    saved, _line = await call(s, "attachment", {"id": 2, "index": 1})
    assert saved["status"] == "error" and "no attachments" in saved["error"], saved


@pytest.mark.parametrize("depth, readable", [(mails.NESTING_MAX - 2, True), (mails.NESTING_MAX + 5, False)])
def test_the_nesting_cap_keeps_real_mails_and_stops_deep_ones(depth, readable):
    message = mails.parse(_nested(depth))
    assert (mails.body_text(message) != mails.UNREADABLE) is readable
    assert len(mails.attachments(message)) == (1 if readable else 0)
