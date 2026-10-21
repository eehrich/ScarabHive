"""The cross-site guard in a real browser (auth/cross_site.py): the page's own POST passes, a page on another port
of the same host -- same site, so it gets the SameSite=Lax cookie -- is refused.

What the browser sends is the point: the unit tests take ``Sec-Fetch-Site`` as given, this measures it.
"""
import http.server
import threading

import pytest
from fastapi import FastAPI

from agent_system.auth.cross_site import CrossSiteGuard
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed")

OWN_PAGE = """<!doctype html><meta charset="utf-8"><body><script type="module">
document.cookie = 'access_token=signed-in; SameSite=Lax; path=/';
const own = await fetch('/act', { method: 'POST' });
const frame = document.createElement('iframe');
const done = new Promise((resolve) => addEventListener('message', resolve, { once: true }));
frame.src = `http://127.0.0.1:${new URLSearchParams(location.search).get('other')}/other.html?target=${location.port}`;
document.body.append(frame);
await done;
const seen = await (await fetch('/seen')).json();
await fetch('/__results', { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ own: own.status, seen }) });
</script>"""

OTHER_PAGE = b"""<!doctype html><meta charset="utf-8"><script type="module">
const target = `http://127.0.0.1:${new URLSearchParams(location.search).get('target')}`;
await fetch(`${target}/act`, { method: 'POST', mode: 'no-cors', credentials: 'include' });
parent.postMessage('done', '*');
</script>"""


class _Other(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(OTHER_PAGE)

    def log_message(self, *args):
        pass


def test_the_own_page_passes_and_a_page_on_another_port_gets_the_cookie_sent_and_is_refused():
    """One test: the root conftest closes the sockets a test leaves open, a module fixture's server among them."""
    from starlette.responses import HTMLResponse

    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    app = FastAPI()
    seen, acted = [], []

    @app.get("/")
    async def page():
        return HTMLResponse(OWN_PAGE)

    @app.post("/act")
    async def act():
        acted.append(1)
        return {"ok": True}

    @app.get("/seen")
    async def report():
        return {"requests": seen, "acted": len(acted)}

    app.add_middleware(CrossSiteGuard)

    @app.middleware("http")  # outside the guard: what arrived, whether it got through or not
    async def record(request, call_next):
        if request.url.path == "/act":
            seen.append({"site": request.headers.get("sec-fetch-site"),
                         "cookie": "access_token" in request.cookies})
        return await call_next(request)

    try:
        results = run_app_test_page(BROWSER, app, f"?other={other.server_address[1]}")
    finally:
        other.shutdown()
        other.server_close()
    assert results["own"] == 200
    assert results["seen"]["requests"] == [{"site": "same-origin", "cookie": True},
                                           {"site": "same-site", "cookie": True}]  # the hole SameSite=Lax leaves
    assert results["seen"]["acted"] == 1  # only the own one reached the route
