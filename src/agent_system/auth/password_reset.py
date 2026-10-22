"""Forgot password: a link by mail to the account's own address, good once and for auth.password_reset.link_minutes.

The link carries a token signed with a key of its own, derived from the signing key -- it signs nobody in, and a
login token sets no password. The token names the account's password generation: setting the password raises it,
so the link is spent once used, and dead once the password changed any other way. It also carries a fingerprint
of the account as it was (address, password hash -- salted, so no two accounts share it): a database made anew that
gives another account the same id, or a changed address, does not match it.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import smtplib
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Any, Optional
from urllib.parse import urlsplit

from jose import JWTError, jwt

from agent_system.auth import security
from agent_system.config.models import AuthConfig, MailConfig

logger = logging.getLogger(__name__)

PURPOSE = "password_reset"
PORTS = {"starttls": 587, "ssl": 465, "none": 25}
#: The request is open to anyone and the mailbox is not theirs: an account gets a mail at most this often,
#: and at most PER_DAY a day.
COOLDOWN_SECONDS = 120
PER_DAY = 5
_DAY = 24 * 3600
# ponytail: per process, forgotten on restart; a store of its own if requests come from several processes
_sent: dict[int, list[float]] = {}
_sent_lock = threading.Lock()  # the sending runs in the thread pool


def reset_problem(auth: AuthConfig) -> str:
    """Why this server sends no reset mail; "" when it does."""
    if not auth.enabled:
        return "sign-in is off"
    if not auth.mail.host:
        return "no mail server: set auth.mail.host"
    if "@" not in parseaddr(auth.mail.sender)[1]:
        return "no sender address: set auth.mail.sender"
    url = urlsplit(auth.password_reset.public_url)
    if url.scheme not in ("http", "https") or not url.netloc:
        return "no public address: set auth.password_reset.public_url"
    return ""


def _key() -> str:
    return hashlib.sha256(f"{security.SECRET_KEY}:{PURPOSE}".encode()).hexdigest()


def fingerprint(user: Any) -> str:
    """The account as it is now: its address and its password hash."""
    account = f"{user.id}|{user.email}|{user.hashed_password}"
    return hmac.new(_key().encode(), account.encode(), hashlib.sha256).hexdigest()[:32]


def reset_token(user: Any, generation: int, minutes: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    claims = {"purpose": PURPOSE, "uid": user.id, "gen": generation, "fp": fingerprint(user), "exp": expires}
    return jwt.encode(claims, _key(), "HS256")


def read_reset_token(token: str) -> Optional[tuple[int, int, str]]:
    """(account id, password generation, fingerprint) of a valid, unexpired reset token; None for anything else."""
    try:
        claims = jwt.decode(token, _key(), algorithms=["HS256"])
    except JWTError:
        return None
    user_id, generation, print_ = claims.get("uid"), claims.get("gen"), claims.get("fp")
    if (claims.get("purpose") != PURPOSE or not isinstance(user_id, int) or not isinstance(generation, int)
            or not isinstance(print_, str)):
        return None
    return user_id, generation, print_


def may_send(user_id: int) -> bool:
    """Whether this account may get a mail now -- and if so, it counts."""
    now = time.monotonic()
    with _sent_lock:
        recent = [at for at in _sent.get(user_id, []) if now - at < _DAY]
        allowed = len(recent) < PER_DAY and (not recent or now - recent[-1] >= COOLDOWN_SECONDS)
        _sent[user_id] = recent + [now] if allowed else recent
    return allowed


def reset_link(auth: AuthConfig, token: str) -> str:
    # In the fragment: a browser never sends it, so no server or proxy log holds it.
    return f"{auth.password_reset.public_url.rstrip('/')}/login#reset={token}"


def reset_mail(auth: AuthConfig, username: str, address: str, link: str) -> EmailMessage:
    # The default policy: smtplib turns to SMTPUTF8 by itself for an address beyond ASCII, and a sender's name
    # beyond ASCII is encoded for any server (policy.SMTPUTF8 sent it raw, also to servers without SMTPUTF8).
    message = EmailMessage()
    message["From"] = auth.mail.sender
    message["To"] = f"{username} <{address}>"  # a username is letters, digits, _ and -
    message["Subject"] = "Set a new ScarabHive password"
    message["Message-ID"] = make_msgid()
    message.set_content(
        f"Someone asked to set a new password for the ScarabHive account \"{username}\".\n\n"
        f"To set one, open this link within {auth.password_reset.link_minutes} minutes:\n\n{link}\n\n"
        "The link works once. If you did not ask for it, ignore this mail: your password stays as it is.\n")
    return message


def send_reset_link(auth: AuthConfig, db: Any, address: str) -> None:
    """Everything after the answer to a reset request: whether an account has the address, the token, the mail.
    Run once the answer went out, so neither its time nor a failure here tells who has an account."""
    try:
        user = db.get_user_by_email(address)
        if user is None or not user.is_active or not may_send(user.id):
            return
        token = reset_token(user, db.token_generation(user.id), auth.password_reset.link_minutes)
        message = reset_mail(auth, user.username, user.email, reset_link(auth, token))
    except Exception as exc:  # nobody waits for this: the log is the only place to say it
        logger.error("Password reset link for %s not prepared: %s", address, exc)
        return
    deliver(auth.mail, message)


def deliver(mail: MailConfig, message: EmailMessage) -> None:
    """Sends ``message`` (blocking); a failure is logged -- the one who asked was told only that a mail may come."""
    try:
        context = ssl.create_default_context()
        port = mail.port or PORTS[mail.security]
        if mail.security == "ssl":
            smtp = smtplib.SMTP_SSL(mail.host, port, timeout=mail.timeout, context=context)
        else:
            smtp = smtplib.SMTP(mail.host, port, timeout=mail.timeout)
        with smtp:
            if mail.security == "starttls":
                smtp.starttls(context=context)
            if mail.username:
                smtp.login(mail.username, mail.password)
            smtp.send_message(message)
        logger.info("Password reset mail sent to %s", message["To"])
    except Exception as exc:  # nobody waits for this either
        logger.error("Password reset mail to %s not sent: %s", message["To"], exc)
