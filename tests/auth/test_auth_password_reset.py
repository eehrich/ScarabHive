"""Forgot password: a link by mail to the account's address, on the configured public address, good once.

The mail is caught where it would go out (password_reset.deliver); everything before it -- the account lookup,
the token, the link, the mail's text -- and the password change after it run for real.
"""
from __future__ import annotations

import re
import smtplib
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI
from jose import jwt
from fastapi.testclient import TestClient

from agent_system.api.auth_endpoints import router
from agent_system.api.auth_endpoints import request_password_reset
from agent_system.auth import database, password_reset, security
from agent_system.auth.models import PasswordResetRequest, UserCreate, UserRole, UserUpdate
from agent_system.auth.security import create_access_token
from agent_system.config.models import AuthConfig, MailConfig, PasswordResetConfig

real_deliver = password_reset.deliver
OLD, NEW = "password123", "newpassword456"
PUBLIC = "https://hive.example.com"


def configured(**changes) -> AuthConfig:
    return AuthConfig(enabled=True, mail=MailConfig(host="smtp.example.com", sender="ScarabHive <hive@example.com>"),
                      password_reset=PasswordResetConfig(public_url=PUBLIC)).model_copy(update=changes)


@pytest.fixture
def db(tmp_path):
    db = database.UserDatabase(tmp_path / "users.db")
    db.create_user(UserCreate(username="alice", email="alice@example.com", password=OLD, role=UserRole.USER))
    carol = db.create_user(UserCreate(username="carol", email="carol@example.com", password=OLD, role=UserRole.USER))
    db.update_user(carol.id, UserUpdate(is_active=False))
    return db


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(password_reset, "deliver", lambda mail, message: sent.append((mail, message)))
    monkeypatch.setattr(password_reset, "_sent", {})
    return sent


def client_for(db, auth: AuthConfig) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[database.get_db] = lambda: db
    app.state.config = SimpleNamespace(auth=auth)
    return TestClient(app)


@pytest.fixture
def client(db):
    return client_for(db, configured())


def ask(client, email="alice@example.com", **kwargs):
    response = client.post("/auth/password-reset-request", json={"email": email}, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def link_in(message) -> str:
    (link,) = re.findall(r"\S+/login#reset=\S+", message.get_content())
    return link


def token_in(message) -> str:
    return link_in(message).split("reset=", 1)[1]


def set_password(client, token, password=NEW):
    return client.post("/auth/password-reset", json={"token": token, "new_password": password})


def signs_in(client, password) -> bool:
    return client.post("/auth/login", json={"username": "alice", "password": password}).status_code == 200


def test_a_known_address_gets_a_link_that_sets_the_password_once(client, mails):
    ask(client)

    assert len(mails) == 1, "no mail went out"
    mail, message = mails[0]
    assert mail.host == "smtp.example.com" and "alice@example.com" in message["To"]
    assert link_in(message).startswith(f"{PUBLIC}/login#reset=")

    assert set_password(client, token_in(message)).status_code == 200
    assert signs_in(client, NEW) and not signs_in(client, OLD)
    assert set_password(client, token_in(message), "another-one-789").status_code == 400, "the link worked twice"
    assert signs_in(client, NEW)


def test_the_answer_tells_nothing_of_who_has_an_account(client, mails):
    known, unknown, inactive = ask(client), ask(client, "nobody@example.com"), ask(client, "carol@example.com")

    assert known == unknown == inactive
    assert [m["To"] for _, m in mails] == ["alice <alice@example.com>"], "an inactive or unknown address got a mail"


def test_the_link_is_built_on_the_public_address_whatever_host_the_request_names(client, mails):
    ask(client, headers={"Host": "evil.example"})

    assert link_in(mails[0][1]).startswith(f"{PUBLIC}/login#reset=")


def test_a_password_change_in_between_spends_the_link(client, db, mails):
    ask(client)
    alice = db.get_user_by_username("alice")
    db.update_user(alice.id, UserUpdate(password="changed-by-admin-1"))

    assert set_password(client, token_in(mails[0][1])).status_code == 400
    assert not signs_in(client, NEW)


def test_a_link_for_an_account_made_inactive_since_sets_nothing(client, db, mails):
    ask(client)
    db.update_user(db.get_user_by_username("alice").id, UserUpdate(is_active=False))

    assert set_password(client, token_in(mails[0][1])).status_code == 400


def test_an_expired_link_or_a_login_token_sets_nothing(client, db):
    alice = db.get_user_by_username("alice")
    expired = password_reset.reset_token(alice, db.token_generation(alice.id), minutes=-1)
    login = create_access_token({"sub": "alice", "user_id": alice.id, "role": "user", "gen": 0, "uid": alice.id})

    assert set_password(client, expired).status_code == 400
    assert set_password(client, login).status_code == 400
    assert signs_in(client, OLD)


def test_a_reset_link_signs_nobody_in(client, mails):
    ask(client)

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token_in(mails[0][1])}"})

    assert me.status_code == 401, me.text


def test_an_account_gets_one_mail_per_cooldown(client, mails):
    ask(client)
    ask(client)

    assert len(mails) == 1


@pytest.mark.parametrize("auth, problem", [
    (configured(mail=MailConfig()), "auth.mail.host"),
    (configured(mail=MailConfig(host="smtp.example.com")), "auth.mail.sender"),
    (configured(password_reset=PasswordResetConfig()), "public_url"),
    (configured(enabled=False), "sign-in is off"),
])
def test_without_a_mail_server_a_sender_or_a_public_address_it_says_what_is_missing(db, mails, auth, problem):
    client = client_for(db, auth)

    asked = client.post("/auth/password-reset-request", json={"email": "alice@example.com"})
    reset = set_password(client, "whatever")

    assert asked.status_code == reset.status_code == 503 and problem in asked.json()["detail"], asked.text
    assert mails == []


@pytest.mark.anyio
@pytest.mark.parametrize("auth, offered", [(configured(), True), (configured(mail=MailConfig()), False)])
async def test_the_login_page_offers_forgot_password_only_where_a_mail_can_go_out(auth, offered):
    from agent_system.app import build_app

    app = build_app()
    app.state.config = app.state.config.model_copy(update={"auth": auth})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/login")

    assert page.status_code == 200
    assert ('id="forgotLink"' in page.text) is offered


def test_a_link_from_a_database_made_anew_sets_nothing_for_whoever_has_the_id_now(client, db, mails, tmp_path):
    ask(client)
    anew = database.UserDatabase(tmp_path / "anew.db")
    dave = anew.create_user(UserCreate(username="dave", email="dave@example.com", password=OLD, role=UserRole.USER))
    assert dave.id == db.get_user_by_username("alice").id, "fixture: the id is not taken again"

    assert set_password(client_for(anew, configured()), token_in(mails[0][1])).status_code == 400


def test_a_link_sent_before_the_address_changed_sets_nothing(client, db, mails):
    ask(client)
    db.update_user(db.get_user_by_username("alice").id, UserUpdate(email="alice.new@example.com"))

    assert set_password(client, token_in(mails[0][1])).status_code == 400


def test_neither_the_signing_key_nor_another_purpose_makes_a_reset_token(client, db):
    alice = db.get_user_by_username("alice")
    claims = password_reset.read_reset_token(password_reset.reset_token(alice, db.token_generation(alice.id), 30))
    assert claims is not None, "fixture: a real token does not read"
    _, generation, print_ = claims
    good = {"purpose": password_reset.PURPOSE, "uid": alice.id, "gen": generation, "fp": print_}
    with_login_key = jwt.encode(good, security.SECRET_KEY, "HS256")
    other_purpose = jwt.encode({**good, "purpose": "something_else"}, password_reset._key(), "HS256")

    assert set_password(client, with_login_key).status_code == 400
    assert set_password(client, other_purpose).status_code == 400
    assert signs_in(client, OLD)


def test_the_answer_comes_before_the_address_is_looked_up():
    class Untouched:
        def __getattr__(self, name):
            raise AssertionError(f"the account was looked up before the answer ({name})")

    background = BackgroundTasks()
    answer = asyncio.run(request_password_reset(PasswordResetRequest(email="alice@example.com"), background,
                                                auth=configured(), db=Untouched()))

    assert "on its way" in answer.message
    assert [task.func for task in background.tasks] == [password_reset.send_reset_link]


class RecordingSMTP(smtplib.SMTP):
    """smtplib's own send_message, up to the wire: what it would send, and the options it would ask for."""
    sent: list = []

    def __init__(self, host="", port=0, timeout=None, **kwargs):
        super().__init__()  # no host: no connection

    def ehlo_or_helo_if_needed(self):
        self.does_esmtp, self.esmtp_features = True, {"smtputf8": "", "8bitmime": "", "starttls": ""}

    def starttls(self, *args, **kwargs):
        return 220, b"ready"

    def login(self, user, password, **kwargs):
        return 235, b"ok"

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        RecordingSMTP.sent.append((to_addrs, msg, list(mail_options)))
        return {}


@pytest.mark.parametrize("address, smtputf8", [("alice@example.com", False), ("jörg@example.com", True)])
def test_the_mail_goes_out_for_any_server_and_beyond_ascii_where_it_must(db, mails, monkeypatch, address, smtputf8):
    """A sender's name beyond ASCII is encoded for any server; only an address beyond ASCII asks for SMTPUTF8."""
    if address != "alice@example.com":
        db.create_user(UserCreate(username="joerg", email=address, password=OLD, role=UserRole.USER))
    auth = configured(mail=MailConfig(host="smtp.example.com", sender="Schäfer Hive <hive@example.com>",
                                      username="hive", password="secret"))
    ask(client_for(db, auth), address)
    assert len(mails) == 1, "fixture: no mail was made"
    monkeypatch.setattr(smtplib, "SMTP", RecordingSMTP)
    monkeypatch.setattr(RecordingSMTP, "sent", [])

    real_deliver(auth.mail, mails[0][1])

    [(to, wire, options)] = RecordingSMTP.sent
    assert to == [address] and address.encode() in wire
    assert ("SMTPUTF8" in options) is smtputf8, options
    # raw UTF-8 only where SMTPUTF8 was asked for; to any other server the name goes encoded
    assert (b"From: =?utf-8?" in wire) is not smtputf8, "the sender's name went out raw to any server"


def test_an_account_gets_a_few_mails_a_day_at_most(client, mails, monkeypatch):
    monkeypatch.setattr(password_reset, "COOLDOWN_SECONDS", 0)

    for _ in range(password_reset.PER_DAY + 2):
        ask(client)

    assert len(mails) == password_reset.PER_DAY
