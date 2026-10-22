"""The sign-in page's "Forgot password?" and reset link in a real browser.

The page is the real login.html; the server behind it is a stub. It records what the page sent (GET
/__stub/sent). /auth/password-reset takes the token "good" and refuses any other, as a spent link is refused;
the cookie ``stub_me=in`` makes /auth/me answer as for someone signed in. ``LOGIN_MUTANT_DIR`` names a folder
whose login.html is served instead (the mutation check).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from agent_system.ui.resources import STATIC_DIR, ui_templates
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

UI_TESTS = Path(__file__).resolve().parent
ASKED = {"message": "If an account has this address, a link to set a new password is on its way",
         "detail": "The link holds for 30 minutes and works once"}


def stub_app() -> FastAPI:
    app = FastAPI()
    templates = ui_templates(*filter(None, [os.environ.get("LOGIN_MUTANT_DIR")]))
    sent: list[dict] = []

    @app.get("/login")
    async def login(request: Request):
        return templates.TemplateResponse(request, "login.html", {"password_reset": True})

    @app.get("/auth/me")
    async def me(request: Request):
        if request.cookies.get("stub_me") == "in":
            return {"username": "alice"}
        raise HTTPException(status_code=401, detail="Not authenticated")

    @app.post("/auth/password-reset-request")
    async def reset_request(request: Request):
        sent.append({"path": "request", **await request.json()})
        return ASKED

    @app.post("/auth/password-reset")
    async def reset(request: Request):
        body = await request.json()
        sent.append({"path": "reset", **body})
        if body.get("token") != "good":
            raise HTTPException(status_code=400, detail="This link is not valid any more; ask for a new one")
        return {"message": "Your new password is set; sign in with it"}

    @app.get("/__stub/sent")
    async def what_was_sent():
        return sent

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ui", StaticFiles(directory=UI_TESTS), name="ui-tests")
    return app


@pytest.fixture(scope="module")
def results():
    return run_app_test_page(BROWSER, stub_app(), "tests/ui/login_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'forgot password asks for the address and brings the answer back to the sign-in form',
    'a reset link opens the new password form and takes the token out of the address bar',
    'two different passwords are refused before anything is sent',
    'the reset link sets the password and sends the user to sign in',
    'a spent link says so and sets nothing',
    'someone signed in who opens a reset link stays to set the password',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_login_page(results, name):
    assert results.get(name) == "ok", results
