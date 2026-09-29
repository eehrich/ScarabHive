"""Screenshots of plugin panels for their guides -- from the panel test's stub app, never from real data.

A plugin's panel test builds an app with the real plugin router and seeded data; this script serves that app with
uvicorn, points a headless Chromium at the panel and writes the picture to ``src/plugins/<plugin>/docs/``, where a
guide shows it with ``@{image docs/<name>.png}``.

    .venv/Scripts/python.exe src/scripts/guide_screenshots.py todo
"""
from __future__ import annotations

import importlib
import inspect
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from tests.ui.browser import _browsers_die_with_this_process, find_browser  # noqa: E402

#: plugin -> shots: (file name, module with the panel test's app factory, its name, page, window size[, script]);
#: the script runs in the page once it has loaded -- for a panel that shows the thing worth a picture only after a click
SHOTS = {
    "todo": [("panel.png", "plugins.todo.tests.test_plugin_todo_panel", "panel_app",
              "/plugins/todo/?session_id=s-1", (1000, 700))],
    "memory": [("panel.png", "plugins.memory.tests.test_plugin_memory_panel", "panel_app",
                "/plugins/memory/?session_id=s-1", (1000, 480))],
    "message_debugger": [("panel.png", "plugins.message_debugger.tests.test_plugin_message_debugger_panel", "panel_app",
                          "/plugins/message_debugger/?request_id=r-1", (1000, 560))],
    "context_summarizer": [("panel.png", "plugins.context_summarizer.tests.test_plugin_context_summarizer_panel",
                            "panel_app", "/plugins/context_summarizer/?session_id=s-1", (980, 380))],
    "lessons_learned": [("panel.png", "plugins.lessons_learned.tests.test_plugin_lessons_learned_panel", "panel_app",
                         "/plugins/lessons_learned/", (1100, 820))],
    "context_usage_tracker": [("panel.png", "plugins.context_usage_tracker.tests.test_plugin_context_usage_tracker_panel",
                               "panel_app", "/plugins/context_usage_tracker/?session_id=s-1", (1000, 760))],
    "context_engineer": [("panel.png", "plugins.context_engineer.tests.test_plugin_context_engineer_panel", "panel_app",
                          "/plugins/context_engineer/?session_id=s-1", (980, 700), """
        // the seed's markup test strings are no sight: neutral words in their place, in the picture only
        await new Promise((done) => setTimeout(done, 1500));
        const cells = [...document.querySelectorAll('td, td *')].filter((el) => !el.children.length);
        for (const cell of cells.filter((el) => el.textContent.trim().startsWith('<img'))) {
          cell.textContent = cell.closest('#facts, [data-table="facts"], table')?.querySelector('th')?.textContent
            .includes('Category') ? 'Keep the public API stable' : 'coder';
        }""")],
    "debate_forum": [("panel.png", "plugins.debate_forum.tests.test_plugin_debate_forum_panel", "panel_app",
                      "/plugins/debate_forum/", (1000, 680), """
        const until = async (find) => { for (;;) { const found = find(); if (found) return found;
                                                   await new Promise((done) => setTimeout(done, 50)); } };
        await fetch('/__stub/delete?channel=3', {method: 'POST'});  // the markup test channel is no sight
        document.querySelector('pk-refresh [data-act="now"]').click();
        const group = () => [...document.querySelectorAll('.df-group > summary')];
        await until(() => group().length && !group().some((one) => one.textContent.includes('Markup')));
        group().find((one) => one.textContent.includes('Schema review')).click();
        (await until(() => document.querySelector('[data-channel="1"]'))).click();
        await until(() => document.querySelectorAll('.df-post').length === 4);
        setTimeout(() => { document.querySelector('.df-messages').scrollTop = 0; }, 300);""")],
}


def with_script(app, page: str, script: str):
    """The app, its page carrying ``script`` as a module at the end of its body."""
    from starlette.responses import Response

    path = page.split("?")[0]

    @app.middleware("http")
    async def add_script(request, call_next):
        response = await call_next(request)
        if request.url.path != path:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator]).decode("utf-8")
        body = body.replace("</body>", f'<script type="module">{script}</script></body>', 1)
        return Response(body, status_code=response.status_code, media_type="text/html")
    return app


def shoot(browser: str, url: str, out: Path, size: tuple[int, int]) -> None:
    _browsers_die_with_this_process()  # Windows: a browser left behind dies with this process
    env = {key: value for key, value in os.environ.items() if key.upper() != "__COMPAT_LAYER"}
    profile = tempfile.mkdtemp(prefix="pk-shot-")
    try:
        # the virtual time budget lets the page load its data and draw before the picture is taken
        subprocess.run([browser, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}", "--no-first-run",
                        "--disable-extensions", "--hide-scrollbars", "--force-device-scale-factor=1",
                        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                        f"--window-size={size[0]},{size[1]}", "--virtual-time-budget=5000",
                        f"--screenshot={out}", url],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=120, check=True)
    finally:
        for _ in range(25):
            shutil.rmtree(profile, ignore_errors=True)
            if not os.path.exists(profile):
                break
            time.sleep(0.2)


@contextmanager
def serve(app):
    """The app on a free local port; stopped and its socket closed on the way out."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off", ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("the stub server did not start")
            time.sleep(0.05)
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def main(plugins: list[str]) -> None:
    browser = find_browser()
    if browser is None:
        raise SystemExit("no Chromium-based browser installed")
    for plugin in plugins or SHOTS:
        for name, module, factory, page, size, *script in SHOTS[plugin]:
            # a plugin may keep its database open until the process ends: its folder is left then
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data:
                build = getattr(importlib.import_module(module), factory)
                # a factory that keeps data on disk takes the folder for it; one that holds it in memory takes none;
                # one that patches the plugin takes pytest's monkeypatch as well
                params = inspect.signature(build).parameters
                patch = pytest.MonkeyPatch()
                app = build(*[Path(data)][:len(params)], *([patch] if "monkeypatch" in params else []))
                if script:
                    app = with_script(app, page, script[0])
                with serve(app) as port:
                    url = f"http://127.0.0.1:{port}{page}"
                    # the browser takes a picture of an error page as well: a page that fails keeps the old one
                    with urllib.request.urlopen(url, timeout=30) as answer:
                        if answer.status != 200:
                            raise SystemExit(f"{url} answered {answer.status}")
                    out = REPO / "src" / "plugins" / plugin / "docs" / name
                    out.parent.mkdir(exist_ok=True)
                    shoot(browser, url, out, size)
                    print(f"{out.relative_to(REPO)}")
                patch.undo()


if __name__ == "__main__":
    main(sys.argv[1:])
