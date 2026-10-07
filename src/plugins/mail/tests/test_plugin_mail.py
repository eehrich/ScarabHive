"""The send tool against a fake SMTP server (smtplib.SMTP / SMTP_SSL replaced): through the real dispatch
(call_with_status -> status bus), nothing reaches the network."""
from __future__ import annotations

import smtplib
import ssl

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.tools.status import StatusPhase, get_status_bus
from plugins.mail import server as mail

SETTINGS = {"host": "smtp.example.org", "username": "bot@example.org", "password": "secret",
            "recipients": ["me@example.org", "Team@Example.org"]}


class FakeSMTP:
    """One SMTP session: what the tool did in it, in order. `refuse` maps an address to the server's no for it;
    `login_fails` and `down` make the login or the connection fail."""
    sessions: list["FakeSMTP"] = []
    refuse: dict = {}
    login_fails = down = False

    def __init__(self, host, port, timeout=None, context=None):
        if FakeSMTP.down:
            raise ConnectionRefusedError(10061, "refused")
        self.host, self.port, self.timeout, self.context = host, port, timeout, context    # SMTP_SSL's
        self.steps, self.message = [], None
        FakeSMTP.sessions.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.steps.append("quit")

    def starttls(self, context=None):
        self.steps.append("starttls")
        self.context = context

    def login(self, user, password):
        self.steps.append(("login", user, password))
        ("\0%s\0%s" % (user, password)).encode("ascii")           # what smtplib's AUTH PLAIN does
        if FakeSMTP.login_fails:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, message):
        self.steps.append("send")
        self.message = message
        to = [a.strip() for a in message["To"].split(",")]
        if all(a in FakeSMTP.refuse for a in to):
            raise smtplib.SMTPRecipientsRefused({a: FakeSMTP.refuse[a] for a in to})
        return {a: FakeSMTP.refuse[a] for a in to if a in FakeSMTP.refuse}


@pytest.fixture(autouse=True)
def smtp(monkeypatch):
    FakeSMTP.sessions, FakeSMTP.refuse, FakeSMTP.login_fails, FakeSMTP.down = [], {}, False, False
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", lambda *a, **k: FakeSMTP(*a, **k))
    return FakeSMTP


def server(**settings):
    return mail.MailServer("mail", AgentSystemConfig(), ToolServerConfig(type="mail", **{**SETTINGS, **settings}))


async def call(s, params):
    """One call through the real dispatch: the answer and the line that closed the status scope."""
    bus = get_status_bus()
    queue = await bus.subscribe(server="mail.send()")
    try:
        result = await s.call_with_status("mail_send", params)
    finally:
        bus.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    closing = [e for e in events if e.phase in (StatusPhase.END, StatusPhase.ERROR)]
    assert len(closing) == 1, events
    return result, closing[0].message


async def test_a_mail_goes_to_every_configured_recipient_over_starttls(smtp):
    result, line = await call(server(), {"subject": "Video fertig: Übersicht", "body": "Bitte hochladen.\nDatei: x.mp4"})
    assert result == {"status": "success", "sent_to": ["me@example.org", "Team@Example.org"]}, result
    assert "2 recipient(s)" in line and "Video fertig" in line, line
    (session,) = smtp.sessions
    assert (session.host, session.port, session.timeout) == ("smtp.example.org", 587, 30.0)
    assert session.steps == ["starttls", ("login", "bot@example.org", "secret"), "send", "quit"], session.steps
    m = session.message
    assert (m["From"], m["To"], m["Subject"]) == ("bot@example.org", "me@example.org, Team@Example.org",
                                                  "Video fertig: Übersicht")
    assert m.get_content() == "Bitte hochladen.\nDatei: x.mp4\n" and m["Date"] and m["Message-ID"].endswith("@example.org>")


async def test_to_picks_configured_recipients_only(smtp):
    result, _ = await call(server(), {"subject": "s", "body": "b", "to": ["team@example.org"]})
    assert result["sent_to"] == ["Team@Example.org"], "matched without case, sent as configured"
    for stranger in (["someone@else.org"], ["me@example.org", "someone@else.org"], [3], "x"):
        result, line = await call(server(), {"subject": "s", "body": "b", "to": stranger})
        assert result["status"] == "error" and "only configured recipients" in result["error"], (stranger, result)
    assert len(smtp.sessions) == 1, "a refused address opens no SMTP session"


@pytest.mark.parametrize("settings, params, said", [
    ({"host": ""}, {}, "no SMTP server configured"),
    ({"recipients": []}, {}, "no recipients configured"),
    ({"sender": "", "username": ""}, {}, "no sender address configured"),
    ({"security": "tls"}, {}, "security: one of"),
    ({}, {"subject": "a\r\nBcc: someone@else.org"}, "one line"),
    ({}, {"subject": "a Bcc: someone@else.org"}, "one line"),
    ({}, {"subject": "a\ud800"}, "one line"),
    ({}, {"body": "a\ud800"}, "body is required"),
    ({}, {"subject": " "}, "subject is required"),
    ({}, {"subject": "x" * 201}, "subject is required"),
    ({}, {"body": ""}, "body is required"),
    ({}, {"body": "x" * (mail.BODY_MAX + 1)}, "body is required"),
])
async def test_what_cannot_go_out_is_refused_before_any_connection(smtp, settings, params, said):
    result, _ = await call(server(**settings), {"subject": "s", "body": "b", **params})
    assert result["status"] == "error" and said in result["error"], result
    assert not smtp.sessions


@pytest.mark.parametrize("security, port, steps", [
    ("ssl", 465, [("login", "bot@example.org", "secret"), "send", "quit"]),
    ("none", 25, [("login", "bot@example.org", "secret"), "send", "quit"]),
])
async def test_ssl_and_plain_take_their_own_port_and_no_starttls(smtp, security, port, steps):
    result, _ = await call(server(security=security), {"subject": "s", "body": "b"})
    assert result["status"] == "success", result
    (session,) = smtp.sessions
    assert session.port == port and session.steps == steps, (session.port, session.steps)


async def test_a_port_and_no_login_as_configured(smtp):
    await call(server(port=2525, username="", sender="Bot <bot@example.org>"), {"subject": "s", "body": "b"})
    (session,) = smtp.sessions
    assert session.port == 2525 and session.steps == ["starttls", "send", "quit"], session.steps
    assert session.message["From"] == "Bot <bot@example.org>"


@pytest.mark.parametrize("security", ["starttls", "ssl"])
async def test_the_server_s_certificate_is_checked(smtp, security):
    """A context that verifies nothing hands the login to anyone in between: smtplib's own default does that."""
    await call(server(security=security), {"subject": "s", "body": "b"})
    (session,) = smtp.sessions
    context = session.context
    assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


async def test_a_password_smtp_cannot_send_is_not_echoed(smtp):
    result, _ = await call(server(password="Passwört"), {"subject": "s", "body": "b"})
    assert result["status"] == "error" and "ASCII" in result["error"] and "Passw" not in result["error"], result


async def test_failures_say_what_happened(smtp):
    smtp.login_fails = True
    result, _ = await call(server(), {"subject": "s", "body": "b"})
    assert "refused the login (535)" in result["error"] and "secret" not in result["error"], result
    smtp.login_fails, smtp.down = False, True
    result, _ = await call(server(), {"subject": "s", "body": "b"})
    assert result["error"].startswith("mail not sent: ConnectionRefusedError"), result
    smtp.down, smtp.refuse = False, {"me@example.org": (550, b"no such user"),
                                     "Team@Example.org": (550, b"no such user")}
    result, _ = await call(server(), {"subject": "s", "body": "b"})
    assert "refused every recipient" in result["error"], result


async def test_a_recipient_the_server_refused_is_named_beside_the_others(smtp):
    smtp.refuse = {"me@example.org": (550, b"no such user")}
    result, line = await call(server(), {"subject": "s", "body": "b"})
    assert result == {"status": "success", "sent_to": ["Team@Example.org"], "refused": ["me@example.org"]}, result
    assert "1 recipient(s)" in line, line


async def test_the_tool_is_offered_as_mail_send():
    names = [t["function"]["name"] for t in server().get_tools()]
    assert names == ["mail_send"], names
