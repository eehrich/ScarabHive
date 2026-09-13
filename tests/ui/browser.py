"""Run a test page in a real headless browser and collect what it reports.

The page POSTs its results as JSON to /__results; the harness waits for that
request instead of guessing when an async module has finished. Tests skip
when no Chromium-based browser is installed, like the node-based tests skip
without node.
"""
from __future__ import annotations

import functools
import http.server
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
]


def find_browser() -> str | None:
    for candidate in _CANDIDATES:
        if Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the repository root (/static/..., /tests/ui/...) and takes the results POST."""

    # Module scripts need a JavaScript MIME type; the Windows registry often
    # maps .js to text/plain, and the browser then refuses the import.
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
        ".woff2": "font/woff2", ".html": "text/html", ".json": "application/json",
    }

    def do_POST(self):
        if self.path != "/__results":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.results = json.loads(body)
        self.server.reported.set()
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_test_page(browser: str, page: str, timeout: float = 90) -> dict:
    """Open /<page> headless and return the JSON the page POSTed to /__results."""
    handler = functools.partial(_Handler, directory=str(REPO))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.reported = threading.Event()
    server.results = None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/{page}"
    with tempfile.TemporaryDirectory() as profile:
        process = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}",
             "--no-first-run", "--disable-extensions", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if not server.reported.wait(timeout):
                raise AssertionError(f"{page} reported no results within {timeout}s")
        finally:
            process.kill()
            process.wait(timeout=30)
            server.shutdown()
    return server.results
