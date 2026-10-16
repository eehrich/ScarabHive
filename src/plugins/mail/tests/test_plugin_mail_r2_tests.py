"""Tests review, second round: what a reader of the poller state can see between two writes of poll_once."""
from __future__ import annotations

from plugins.mail.tests.test_plugin_mail_imap import FakeIMAP, imap, make_mail, server  # noqa: F401


async def test_a_fresh_last_poll_never_shows_the_error_of_an_earlier_failed_poll(imap):
    FakeIMAP.mails = {1: make_mail()}
    s = server()
    s.store.set("last_error", "error: [AUTHENTICATIONFAILED] wrong: ***")
    seen = []
    real_set = type(s.store).set

    def spying_set(store, key, value):
        real_set(store, key, value)
        if key == "last_poll":             # what status reads the instant the poll is stamped
            seen.append(store.get("last_error"))

    s.store.set = spying_set.__get__(s.store)
    await s.poll_once()
    assert seen == [None], seen
    assert s.store.get("last_poll") and s.store.get("last_error") is None
