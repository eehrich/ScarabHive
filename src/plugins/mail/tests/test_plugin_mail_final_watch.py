"""The watcher's wakes: the plugin's own mail is no news, a wake belongs to no run, a wake lost with its process is
rung again, and a moved watch takes its news along. Fake IMAP/SMTP of test_plugin_mail_imap."""
from __future__ import annotations

import asyncio

import pytest

from agent_system.tools.status import current_request_id
from plugins.mail import server as mail
from plugins.mail.tests.test_plugin_mail import FakeSMTP
from plugins.mail.tests.test_plugin_mail_imap import call, imap, make_mail, server  # noqa: F401 - imap: autouse


@pytest.fixture
def rings(monkeypatch):
    """wake_session recorded with started_by; `hold` keeps a wake ringing until set."""
    calls, hold = [], asyncio.Event()
    hold.set()

    async def wake_session(system_config, session_id, user_id, what="", still_needed=None, started_by=None):
        calls.append({"session_id": session_id, "what": what, "still_needed": still_needed,
                      "started_by": started_by if started_by is not None else current_request_id.get(),
                      "run": current_request_id.get()})
        await hold.wait()
        return "woke_session"

    monkeypatch.setattr(mail, "wake_session", wake_session)
    monkeypatch.setattr(mail, "wake_blocked", lambda config, session_id, user_id: "")
    monkeypatch.setattr(mail, "presence_for", lambda config: None)
    return calls, hold


async def test_the_plugins_own_report_landing_in_the_mailbox_wakes_nobody(imap, rings):
    calls, _ = rings
    imap.mails = {1: make_mail()}
    s = server(recipients=["bot@example.org"])           # the report goes to the watched mailbox itself
    await call(s, "watch", {"action": "on", "_session_id": "s-1", "_user_id": "u-1"})
    await s.poll_once()
    sent, _ = await call(s, "send", {"subject": "2 new mails", "body": "Report."})
    assert sent["status"] == "success", sent
    imap.mails[2] = FakeSMTP.sessions[-1].message.as_bytes()
    assert await s.poll_once() == [] and not calls, "its own report is no news"
    assert s.store.news(7) == set() and s.store.get("cursor") == [7, 2]
    imap.mails[3] = make_mail(message_id="<other@example.org>")
    assert await s.poll_once() == [3]
    await s._waking
    assert len(calls) == 1


async def test_a_wake_belongs_to_no_run_even_when_a_run_started_the_poller(imap, rings):
    calls, _ = rings
    imap.mails = {1: make_mail()}
    s = server(poll_seconds=0.01)
    s.store.set("watch", {"session_id": "s-1", "user_id": "u-1"})
    token = current_request_id.set("first-run-id")
    try:
        await s.start_plugin()
    finally:
        current_request_id.reset(token)
    for _ in range(100):
        if s.store.get("cursor"):
            break
        await asyncio.sleep(0.02)
    imap.mails[2] = make_mail()
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0.02)
    await s.stop_plugin()
    assert calls and calls[0]["started_by"] == "" and calls[0]["run"] is None, calls


async def test_a_wake_lost_with_its_process_is_rung_by_the_next_lease_holder(imap, rings):
    calls, hold = rings
    hold.clear()                                          # the session is busy: the wake rings on
    imap.mails = {1: make_mail()}
    a = server()
    a.store.set("watch", {"session_id": "s-1", "user_id": "u-1"})
    await a.poll_once()
    imap.mails[2] = make_mail()
    assert await a.poll_once() == [2]
    await asyncio.sleep(0)
    await a.stop_plugin()                                 # restart: the ringing wake dies with it
    hold.set()
    b = server()
    assert await b.poll_once() == []
    await b._waking
    assert len(calls) == 2 and await calls[1]["still_needed"]() is True, calls
    assert await b.poll_once() == []
    await asyncio.sleep(0)
    assert len(calls) == 2, "rung once, not on every look"


async def test_a_wake_ringing_for_one_session_does_not_take_the_mails_of_the_next(imap, rings):
    calls, hold = rings
    hold.clear()
    imap.mails = {1: make_mail()}
    s = server()
    await call(s, "watch", {"action": "on", "_session_id": "s-1", "_user_id": "u-1"})
    await s.poll_once()
    imap.mails[2] = make_mail()
    await s.poll_once()
    await asyncio.sleep(0)
    await call(s, "watch", {"action": "on", "_session_id": "s-2", "_user_id": "u-1"})
    imap.mails[3] = make_mail()
    await s.poll_once()
    await asyncio.sleep(0)
    assert [c["session_id"] for c in calls] == ["s-1", "s-2"], calls
    assert calls[1]["what"] == "2 new mail(s) in INBOX", "the moved watch takes the waiting news along"
    hold.set()
    await s.stop_plugin()
