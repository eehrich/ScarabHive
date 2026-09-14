"""The two pages the app serves itself, rendered by the real app -- also where no browser is installed."""
from __future__ import annotations

import httpx
import pytest

from agent_system.app import build_app

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("path, entry", [
    ("/", '<script type="module" src="/static/js/shell/shell.js">'),
    ("/login", 'id="loginForm"'),
])
async def test_the_page_renders_with_its_entry_point(path, entry):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app()), base_url="http://test") as client:
        page = await client.get(path)

    assert page.status_code == 200
    assert entry in page.text
    assert '<html lang="en" class="pk-root" data-theme="system">' in page.text
