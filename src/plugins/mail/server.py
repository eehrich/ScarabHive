"""Mail: a plain-text message over SMTP, to the recipients the server entry names.

The recipients are an allowlist in the configuration, never the model's choice: a model talked into mailing someone
else -- by a web page it read -- gets an error. The password is a setting, best `${MAIL_PASSWORD}` from secrets.env.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from agent_system.tools.schema_based import SchemaBasedToolServer

logger = logging.getLogger(__name__)

PORTS = {"starttls": 587, "ssl": 465, "none": 25}
SUBJECT_MAX = 200
BODY_MAX = 100_000          # characters: a report, not an attachment


def _error(message: str) -> dict:
    return {"status": "error", "error": message}


def _sendable(text: str) -> bool:
    """Text a mail can carry: no lone surrogate (a JSON escape like \\ud800), which no encoding takes."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _addresses(value) -> list[str]:
    """One address, a comma-separated string of them, or a list."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    return [str(a).strip() for a in items if str(a).strip()]


class MailServer(SchemaBasedToolServer):
    """One tool, `send`: a plain-text mail to some or all of the configured recipients."""

    def __init__(self, name, system_config, server_config):
        super().__init__(name, system_config, server_config)
        self.host = str(getattr(server_config, "host", "") or "")
        self.security = str(getattr(server_config, "security", "starttls") or "starttls").strip().lower()
        self.port = int(getattr(server_config, "port", 0) or PORTS.get(self.security, 587))
        self.username = str(getattr(server_config, "username", "") or "")
        self.password = str(getattr(server_config, "password", "") or "")
        self.sender = str(getattr(server_config, "sender", "") or self.username)
        self.recipients = _addresses(getattr(server_config, "recipients", None))
        self.timeout = float(getattr(server_config, "timeout_s", 30))

    def _deliver(self, message: EmailMessage) -> dict:
        """The SMTP conversation; the recipients the server refused while taking the others."""
        context = ssl.create_default_context()
        if self.security == "ssl":
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        with smtp:
            if self.security == "starttls":
                smtp.starttls(context=context)
            if self.username:
                self._login(smtp)
            return smtp.send_message(message)

    def _login(self, smtp: smtplib.SMTP) -> None:
        """One login attempt. smtplib.login tries the next method after a refusal, and a server that hangs up on
        the second (Gmail) hides the first answer -- "534 Application-specific password required" came out as
        "Connection unexpectedly closed". smtplib's order (CRAM-MD5 sends no password); none of them offered:
        smtplib's own way, for its error."""
        smtp.ehlo_or_helo_if_needed()             # STARTTLS forgets the server's features: they come anew
        offered = str(smtp.esmtp_features.get("auth", "")).upper().split()
        method = next((m for m in ("CRAM-MD5", "PLAIN", "LOGIN") if m in offered), None)
        if method is None:
            smtp.login(self.username, self.password)
            return
        smtp.user, smtp.password = self.username, self.password
        smtp.auth(method, getattr(smtp, f"auth_{method.lower().replace('-', '_')}"))

    async def send(self, params: dict) -> dict:
        """Tool "<name>_send"."""
        subject, body, to = params.get("subject"), params.get("body"), params.get("to")
        if not self.host:
            return _error("no SMTP server configured: set host in the mail server entry")
        if self.security not in PORTS:
            return _error(f"security: one of {', '.join(PORTS)} in the mail server entry, not {self.security!r}")
        if "@" not in parseaddr(self.sender)[1]:
            return _error("no sender address configured: set sender (or username) in the mail server entry")
        if not self.recipients:
            return _error("no recipients configured: set recipients in the mail server entry")
        if not isinstance(subject, str) or not subject.strip() or len(subject) > SUBJECT_MAX \
                or subject.splitlines() != [subject] or not _sendable(subject):    # every line break, not \n alone
            return _error(f"subject is required: one line of at most {SUBJECT_MAX} characters")
        if not isinstance(body, str) or not body.strip() or len(body) > BODY_MAX or not _sendable(body):
            return _error(f"body is required: plain text of at most {BODY_MAX} characters")
        if to in (None, "", []):
            chosen = list(self.recipients)
        else:
            wanted = _addresses(to) if isinstance(to, (str, list)) else None
            known = {a.lower(): a for a in self.recipients}
            if not wanted or not all(isinstance(a, str) and a.lower() in known for a in wanted):
                return _error("to: only configured recipients can be written to; leave it out to write to all of them")
            chosen = list(dict.fromkeys(known[a.lower()] for a in wanted))
        cancel = params.get("_cancellation_token")
        if cancel and cancel.is_cancelled:
            return {"error": "Tool 'send' was cancelled.", "cancelled": True, "forced": False}

        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, ", ".join(chosen), subject.strip()
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=parseaddr(self.sender)[1].rpartition("@")[2])
        message.set_content(body)
        try:
            refused = await asyncio.to_thread(self._deliver, message)
        except smtplib.SMTPAuthenticationError as e:     # the server's own words say what it wants
            said = e.smtp_error.decode("utf-8", "replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            said = " ".join(said.split())[:200]
            return _error(f"the SMTP server refused the login ({e.smtp_code} {said}): check username and password")
        except smtplib.SMTPRecipientsRefused as e:
            return _error(f"the SMTP server refused every recipient: {', '.join(e.recipients)}")
        except UnicodeError:          # never the exception's text: smtplib's login error carries the password
            return _error("mail not sent: SMTP sends the username, the password and the addresses as ASCII")
        except (smtplib.SMTPException, OSError) as e:      # OSError: unreachable, timeout, TLS
            return _error(f"mail not sent: {type(e).__name__}: {e}")
        sent = [a for a in chosen if a not in refused]
        status = params.get("_status")
        if status:
            await status.end(f"Mail to {len(sent)} recipient(s): {subject.strip()[:100]}")
        return {"status": "success", "sent_to": sent, **({"refused": sorted(refused)} if refused else {})}
