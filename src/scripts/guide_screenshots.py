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
import contextlib
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src")]

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from tests.ui.browser import _browsers_die_with_this_process, find_browser  # noqa: E402

#: plugin -> shots: (file name, module with the panel test's app factory, its name, page, window size[, extra]);
#: extra is a script, or {"script": ..., "login": user}: the script runs in the page once it has loaded -- for a panel
#: that shows the thing worth a picture only after a click -- and login signs every request in as that seeded user
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
    "log_viewer": [("panel.png", "plugins.log_viewer.tests.test_plugin_log_viewer_panel", "panel_app",
                    "/plugins/log_viewer/", (1000, 680), """
        // the seed's markup test entry is no sight; the folded traceback is shown open
        await new Promise((done) => setTimeout(done, 1500));
        for (const line of document.querySelectorAll('.lv-message')) {
          if (line.textContent.includes('<img')) line.textContent = 'agent_system.api Loaded the tool servers';
        }
        document.querySelector('details.lv-message')?.setAttribute('open', '');
        const log = document.getElementById('log');
        log.scrollTop = log.scrollHeight;""")],
    "batch_monitor": [("panel.png", "plugins.batch_monitor.tests.test_plugin_batch_monitor_panel", "panel_app",
                       "/plugins/batch_monitor/", (1000, 540))],
    "session_archive": [("panel.png", "plugins.session_archive.tests.test_plugin_session_archive_panel", "panel_app",
                         "/plugins/session_archive/", (900, 380))],
    "agent_editor": [("panel.png", "plugins.agent_editor.tests.test_plugin_agent_editor_panel", "panel_app",
                      "/plugins/agent_editor/", (1240, 820), {"login": "root", "script": """
        const until = async (find) => { for (;;) { const found = find(); if (found) return found;
                                                   await new Promise((done) => setTimeout(done, 50)); } };
        (await until(() => document.querySelector('[data-name="worker"]'))).click();
        await new Promise((done) => setTimeout(done, 800));
        for (const cell of document.querySelectorAll('.ae-item-desc')) {
          if (cell.textContent.includes('<img')) cell.textContent = 'Reviews the changes';
        }"""})],
    "user_management": [("panel.png", "plugins.user_management.tests.test_plugin_user_management_panel", "panel_app",
                         "/plugins/user_management/", (960, 470), {"login": "root", "script": """
        const until = async (find) => { for (;;) { const found = find(); if (found) return found;
                                                   await new Promise((done) => setTimeout(done, 50)); } };
        await until(() => document.querySelector('#users tbody tr'));
        for (const cell of document.querySelectorAll('#users td, #users td *')) {
          if (!cell.children.length && cell.textContent.startsWith('<img')) cell.textContent = 'Guest reviewer';
        }"""})],
    "ssh_control": [("panel.png", "plugins.ssh_control.tests.test_plugin_ssh_control_panel", "panel_app",
                     "/plugins/ssh_control/", (900, 640), """
        const until = async (find) => { for (;;) { const found = find(); if (found) return found;
                                                   await new Promise((done) => setTimeout(done, 50)); } };
        for (const command of ['hostname', 'false']) {  // the stub machine answers; nothing runs anywhere
          await fetch('/plugins/ssh_control/api/execute', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                           body: JSON.stringify({machine: 'alpha', command})});
        }
        document.querySelector('pk-refresh [data-act="now"]').click();
        await until(() => document.querySelectorAll('#output .ssh-entry').length >= 2);""")],
    "comfyui": [("panel.png", "plugins.comfyui.tests.test_plugin_comfyui_panel", "panel_app",
                 "/plugins/comfyui/", (820, 900), """
        // the seed's markup test strings are no sight: neutral words in their place, in the picture only -- on
        // every redraw, as the panel draws its tables anew on each refresh
        const neutral = () => {
          const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          for (let node = walk.nextNode(); node; node = walk.nextNode()) {
            if (node.data.includes('<img')) node.data = 'upscale';
            if (node.data.includes('<b>CUDA</b>')) node.data = 'CUDA out of memory';
          }
        };
        neutral();
        new MutationObserver(neutral).observe(document.body, {childList: true, subtree: true, characterData: true});""")],
    "stategraph": [("panel.png", "plugins.stategraph.tests.stategraph_panel_app", "panel_app",
                    "/plugins/stategraph/?machine=build_review&run=demo_run", (1320, 880), """
        // the stub serves the machines from a temporary folder: its path names this computer's user
        const relative = () => {
          const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          for (let node = walk.nextNode(); node; node = walk.nextNode()) {
            if (/[A-Za-z]:\\\\.*machines\\\\/.test(node.data)) node.data = node.data.replace(/[A-Za-z]:\\\\.*machines\\\\/, 'machines/');
          }
        };
        relative();
        new MutationObserver(relative).observe(document.body, {childList: true, subtree: true, characterData: true});"""),
                   # the README's picture: the graph alone, fitted, the run's path on it
                   ("readme.png", "plugins.stategraph.tests.stategraph_panel_app", "panel_app",
                    "/plugins/stategraph/?machine=build_review&run=demo_run", (1400, 640), """
        const relative = () => {
          const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          for (let node = walk.nextNode(); node; node = walk.nextNode()) {
            if (/[A-Za-z]:\\\\.*machines\\\\/.test(node.data)) node.data = node.data.replace(/[A-Za-z]:\\\\.*machines\\\\/, 'machines/');
          }
        };
        relative();
        new MutationObserver(relative).observe(document.body, {childList: true, subtree: true, characterData: true});
        await new Promise((done) => setTimeout(done, 1500));
        for (const pane of ['machinesPane', 'inspectorPane']) {
          const toggle = document.querySelector(`[data-pk-sidebar-toggle][aria-controls="${pane}"]`);
          if (toggle?.getAttribute('aria-expanded') === 'true') toggle.click();
        }
        await new Promise((done) => setTimeout(done, 500));
        document.getElementById('fit')?.click();
        await new Promise((done) => setTimeout(done, 800));""")],
    "setup": [("panel.png", "plugins.setup.tests.test_plugin_setup_panel", "panel_app",
               "/plugins/setup/", (900, 900), """
        // a first start, seeded: the stub's state is read from this machine's configuration and environment
        const until = async (find) => { for (;;) { const found = find(); if (found) return found;
                                                   await new Promise((done) => setTimeout(done, 50)); } };
        const key = (name, state, named_in, from_environment = false) => ({name, state, named_in, from_environment});
        await fetch('/__stub/set', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
          keys: [key('ANTHROPIC_API_KEY', 'placeholder', ['llm_system.models.claude-opus-thinking']),
                 key('DEEPSEEK_API_KEY', 'missing', ['llm_system.models.deepseek-pro']),
                 key('FORGE_GITLAB_TOKEN', 'set', ['plugins.servers.forge'], true),
                 key('OPENROUTER_API_KEY', 'set', ['llm_system.models.openrouter-base',
                                                   'llm_system.tts_models.openrouter-qwen-tts']),
                 key('TAVILY_API_KEY', 'missing', ['plugins.servers.tavily_search'])],
          auth: {admin: 'admin', default_admin_password: true, shared_signing_key: true,
                 signing_key_needs_restart: false, configured_signing_key_known: true},
          me: 'admin', probe: {ok: true, model: '~deepseek/deepseek-v4-flash-latest'}})});
        document.querySelector('pk-refresh [data-act="now"]').click();
        await until(() => document.querySelectorAll('#keys tbody tr').length === 5 && document.getElementById('password'));
        document.getElementById('probe').click();
        await until(() => document.querySelector('#chat .pk-badge--ok'));""")],
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
    "sub_agent_manager": [
        ("panel.png", "plugins.sub_agent_manager.tests.sam_panel_shot", "panel_app",
         "/plugins/sub_agent_manager/?session_id=s-1", (1000, 820), """
        const until = async (find) => { for (;;) { const found = find(); if (found) return found;
                                                   await new Promise((done) => setTimeout(done, 50)); } };
        await until(() => document.querySelector('#agents .sa-agent'));
        document.querySelector('[data-tab="list"]').click();"""),
        ("map.png", "plugins.sub_agent_manager.tests.sam_panel_shot", "panel_app",
         "/plugins/sub_agent_manager/?session_id=s-1", (1000, 520))],
    # not a plugin: the README's picture of the whole shell, written to docs/images/
    "readme": [("shell.png", "scripts.readme_showcase", "showcase_app",
                "/?panel=/plugins/context_usage_tracker/?session_id=5f2c8a41", (1440, 1000))],
}


def signed_in(app, user: str):
    """The app, every request carrying the login cookie of ``user`` from the factory's user database."""
    from agent_system.auth import database
    from agent_system.auth.security import create_access_token

    @app.middleware("http")
    async def login(request, call_next):
        account = database.get_db().get_user_by_username(user)
        token = create_access_token({"sub": user, "user_id": account.id, "role": account.role.value})
        request.scope["headers"] = [*request.scope["headers"], (b"cookie", f"access_token={token}".encode())]
        return await call_next(request)
    return app


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
                        "--disable-extensions", "--hide-scrollbars", "--force-device-scale-factor=1", "--lang=en-US",
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
        for name, module, factory, page, size, *extra in SHOTS[plugin]:
            extra = {"script": extra[0]} if extra and isinstance(extra[0], str) else (extra[0] if extra else {})
            # a plugin may keep its database open until the process ends: its folder is left then
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data:
                build = getattr(importlib.import_module(module), factory)
                # a factory that keeps data on disk takes the folder for it; one that holds it in memory takes none;
                # one that patches the plugin takes pytest's monkeypatch as well
                params = inspect.signature(build).parameters
                patch = pytest.MonkeyPatch()
                app = build(*[Path(data)][:len(params)], *([patch] if "monkeypatch" in params else []))
                if extra.get("script"):
                    app = with_script(app, page, extra["script"])
                if extra.get("login"):  # added last, so it runs first: the route security sees the cookie
                    app = signed_in(app, extra["login"])
                # the folder is also the working directory, as in the panel tests: seeded names may be relative
                with serve(app) as port, contextlib.chdir(data):
                    url = f"http://127.0.0.1:{port}{page}"
                    # the browser takes a picture of an error page as well: a page that fails keeps the old one
                    with urllib.request.urlopen(url, timeout=30) as answer:
                        if answer.status != 200:
                            raise SystemExit(f"{url} answered {answer.status}")
                    docs = REPO / "docs" / "images" if plugin == "readme" else REPO / "src" / "plugins" / plugin / "docs"
                    out = docs / name
                    out.parent.mkdir(exist_ok=True)
                    shoot(browser, url, out, size)
                    print(f"{out.relative_to(REPO)}")
                patch.undo()


if __name__ == "__main__":
    main(sys.argv[1:])
