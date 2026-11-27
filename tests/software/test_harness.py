"""The harness of the browser tests itself: the test data is well formed, a test user is signed in, the model's
stand-in answers in the chat and a slow answer can be stopped. The software tests of the TESTs stand beside it,
each named after its TEST (test_SHV_TEST_4_...)."""
from __future__ import annotations

import re

from playwright.sync_api import expect

from scarab_harness import DEFAULT_USER, open_chat, users_of


def test_harness_the_test_data_is_well_formed(test_data):
    users = users_of(test_data)
    assert DEFAULT_USER in users and {user.role for user in users.values()} <= {"admin", "user", "guest"}
    assert all(len(user.password) >= 8 for user in users.values())
    assert set(test_data["model"]) <= {"default", "answers"} and test_data["model"]["default"]["text"]


def test_harness_a_test_user_is_signed_in(user_page):
    user_page.goto("/")
    expect(user_page).not_to_have_url(re.compile(r"/login"))
    expect(user_page.locator("#task")).to_be_visible()


def test_harness_nobody_is_signed_in_on_a_plain_page(page):
    page.goto("/")
    expect(page).to_have_url(re.compile(r"/login"))


def test_harness_the_stand_in_answers_in_the_chat(user_page, stand_in):
    open_chat(user_page)
    user_page.locator("#task").fill("Hallo Agent")
    user_page.locator("#runBtn").click()
    expect(user_page.locator("#chat")).to_contain_text("Hallo Agent")
    expect(user_page.locator("#chat")).to_contain_text(stand_in.answers["greeting"].text, timeout=30_000)
    assert stand_in.calls and stand_in.calls[-1]["user"].endswith("Hallo Agent")


def test_harness_a_slow_answer_can_be_stopped(user_page, stand_in):
    stand_in.use("slow")
    open_chat(user_page)
    user_page.locator("#task").fill("Take your time")
    user_page.locator("#runBtn").click()
    expect(user_page.locator("#stopBtn"), f"the run with the slow answer: {stand_in.calls}").to_be_visible(timeout=30_000)
    user_page.locator("#stopBtn").click()
    expect(user_page.locator("#runBtn")).to_be_visible(timeout=30_000)
    expect(user_page.locator("#chat")).not_to_contain_text(stand_in.answers["slow"].text)
