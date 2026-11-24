"""The browser tests of the web interface, from its user's side (pytest-playwright): fixtures of the ScarabHive under
test (scarab_harness.py) and its test data (data/*.yaml).

    scarab        the running ScarabHive: url, data_dir, users, stand_in (session)
    base_url      its address -- page.goto("/") goes there
    test_data     data/*.yaml by file: users, model
    users         the test users by name: users["tester"].password
    stand_in      the model's stand-in, back on the script of the test data for every test
    page          a browser page nobody is signed in on (pytest-playwright's)
    page_as       page_as("admin"): a page signed in as that test user, in a context of its own
    user_page     a page signed in as the default user (tester)

    open_chat(page)   (scarab_harness) the main page, once its chat takes a message -- before typing into #task

Run (Chromium from `python -m playwright install chromium`, or the installed Chrome with --browser-channel chrome):

    python -m pytest tests/software --junitxml=build/test-results/software.xml \
        --tracing=retain-on-failure --screenshot=only-on-failure --output=build/test-results/playwright

Without pytest-playwright the folder is not collected: the rest of the suite runs without a browser.
"""
from __future__ import annotations

import importlib.util
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
import pytest

if importlib.util.find_spec("pytest_playwright") is None:
    collect_ignore_glob = ["*"]

from scarab_harness import DEFAULT_USER, Scarab, TestUser, load_test_data, scarab_under_test  # noqa: E402


@pytest.fixture(autouse=True)
def cleanup_unclosed_resources():
    """Not here: the root conftest's closes every socket it finds after a test -- here those of the ScarabHive under
    test and of Playwright's own event loop, which live for the session (a later call of the sync API hung on
    them, and Playwright's stop failed on its closed self-pipe)."""
    yield


@pytest.fixture(scope="session")
def test_data() -> dict[str, Any]:
    return load_test_data()


@pytest.fixture(scope="session")
def scarab(test_data: dict[str, Any], tmp_path_factory: pytest.TempPathFactory):
    with pytest.MonkeyPatch.context() as patch:
        with scarab_under_test(tmp_path_factory.mktemp("scarab"), patch, test_data) as running:
            yield running


@pytest.fixture(scope="session")
def base_url(scarab: Scarab) -> str:
    return scarab.url


@pytest.fixture(scope="session")
def users(scarab: Scarab) -> dict[str, TestUser]:
    return scarab.users


@pytest.fixture
def stand_in(scarab: Scarab):
    scarab.stand_in.reset()
    yield scarab.stand_in
    scarab.stand_in.reset()


@pytest.fixture(scope="session")
def _sign_ins(scarab: Scarab) -> Callable[[str], dict[str, Any]]:
    """The stored sign-in (a storage_state) of a test user, made once per session through the login API: the
    cookie the login sets, as the browser keeps it."""
    made: dict[str, dict[str, Any]] = {}

    def sign_in(name: str) -> dict[str, Any]:
        if name not in made:
            user = scarab.users[name]
            with httpx.Client(base_url=scarab.url, timeout=30) as client:
                answer = client.post("/auth/login", json={"username": user.name, "password": user.password})
                if answer.status_code != 200:
                    raise RuntimeError(f"test user {name} cannot sign in: {answer.status_code} {answer.text[:200]}")
                host = urlsplit(scarab.url).hostname
                made[name] = {"origins": [], "cookies": [
                    {"name": cookie.name, "value": cookie.value, "domain": host, "path": cookie.path or "/",
                     "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax"}
                    for cookie in client.cookies.jar]}
        return made[name]

    return sign_in


@pytest.fixture
def page_as(new_context: Callable[..., Any], _sign_ins: Callable[[str], dict[str, Any]], stand_in: Any) -> Callable[[str], Any]:
    """page_as(name): a page signed in as the test user ``name``, in a context of its own (traced like ``page``)."""

    def open_as(name: str) -> Any:
        return new_context(storage_state=_sign_ins(name)).new_page()

    return open_as


@pytest.fixture
def user_page(page_as: Callable[[str], Any]) -> Any:
    return page_as(DEFAULT_USER)
