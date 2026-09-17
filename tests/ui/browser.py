"""Run a test page in a real headless browser and collect what it reports.

The page POSTs its results as JSON to /__results; the harness waits for that
request instead of guessing when an async module has finished. Tests skip
when no Chromium-based browser is installed, like the node-based tests skip
without node.
"""
from __future__ import annotations

import ctypes
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response

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


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9
_job = None


def _browsers_die_with_this_process() -> None:
    """Windows: put this process in a job that kills every member when the process ends.

    The browsers started afterwards inherit the membership. A test that times out (pytest-timeout ends the process
    with os._exit) or a runner that kills pytest never reaches the ``finally`` that stops its browser; those orphans
    piled up to 159 headless browsers on 16.09.2026 and froze the machine. The job handle stays open on purpose:
    closing it is what kills the members.
    """
    global _job
    if os.name != "nt" or _job is not None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
    if not (job and kernel32.SetInformationJobObject(job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                                                     ctypes.sizeof(limits))
            and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())):
        raise ctypes.WinError(ctypes.get_last_error())
    _job = job


def _open_headless(browser: str, url: str, reported: threading.Event, timeout: float) -> None:
    _browsers_die_with_this_process()
    # With __COMPAT_LAYER set, Edge starts itself anew and the process started here exits at once: kill() below
    # would reach nothing and the browser would run on (measured: all 18 processes stayed).
    env = {key: value for key, value in os.environ.items() if key.upper() != "__COMPAT_LAYER"}
    profile = tempfile.mkdtemp(prefix="pk-browser-")
    try:
        process = subprocess.Popen(
            # nothing but the test server resolves: a panel's CDN script fails at once
            # instead of tying the result to the network
            [browser, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}",
             "--no-first-run", "--disable-extensions", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            if not reported.wait(timeout):
                raise AssertionError(f"{url} reported no results within {timeout}s")
        finally:
            process.kill()
            process.wait(timeout=30)
    finally:
        # Chromium's helpers let go of the profile a moment after the browser is killed: try again briefly, and
        # never let a failed cleanup hide the results the page already reported.
        for _ in range(25):
            shutil.rmtree(profile, ignore_errors=True)
            if not os.path.exists(profile):
                break
            time.sleep(0.2)


def run_test_page(browser: str, page: str, timeout: float = 90) -> dict:
    """Open /<page> headless and return the JSON the page POSTed to /__results."""
    handler = functools.partial(_Handler, directory=str(REPO))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.reported = threading.Event()
    server.results = None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _open_headless(browser, f"http://127.0.0.1:{server.server_address[1]}/{page}", server.reported, timeout)
    finally:
        server.shutdown()
    return server.results


def run_app_test_page(browser: str, app: FastAPI, page: str, timeout: float = 120) -> dict:
    """Serve an ASGI app with uvicorn, open /<page> headless, return what it POSTed to /__results.

    For pages the server renders (the shell's index.html is a template); the
    app gets the /__results route added.
    """
    reported = threading.Event()
    box: dict = {}

    @app.post("/__results")
    async def results(request: Request):
        box["results"] = await request.json()
        reported.set()
        return Response(status_code=204)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    # ws="none": the websockets import warns about its legacy module, and the
    # suite turns warnings into errors -- inside the server thread, silently.
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off", ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise AssertionError("the test server did not start")
            time.sleep(0.05)
        _open_headless(browser, f"http://127.0.0.1:{sock.getsockname()[1]}/{page}", reported, timeout)
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()
    return box["results"]
