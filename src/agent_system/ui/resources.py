"""Where templates and static files live, and how a panel template reaches the kit."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from starlette.requests import Request
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Message, Receive, Scope, Send

THEMES = ("dark", "light", "system")
THEME_COOKIE = "ui_theme"


def find_resource_dir(name: str) -> Path:
    """Find a directory that lies outside the package (templates, static, docs) in the supported install layouts."""
    try:
        import agent_system
        resource_path = Path(agent_system.__file__).parent / name
        if resource_path.exists():
            return resource_path
    except Exception:
        pass

    dev_path = Path(__file__).parents[3] / name
    if dev_path.exists():
        return dev_path

    cwd_path = Path.cwd() / name
    if cwd_path.exists():
        return cwd_path

    return dev_path


TEMPLATES_DIR = find_resource_dir("templates")
STATIC_DIR = find_resource_dir("static")


def revalidated(files: ASGIApp) -> ASGIApp:
    """Static files the browser asks for again before each use -- a short 304 while unchanged.

    Given to /static and to every plugin's static folder alike: without a rule the browser keeps a panel
    script for a while on its own, and a page mixes a fresh kit with a stale panel. A wrapping ASGI app,
    not middleware: BaseHTTPMiddleware costs 100 ms and more per request.
    """
    async def serve(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await files(scope, receive, send)
            return
        if any(name == b"if-none-match" for name, _ in scope["headers"]):
            # the ETag decides alone (RFC 9110 13.1.3): Starlette would also answer 304 for an unchanged second of
            # modification, and a file rewritten within it, or replaced by one with an older time, would stay stale
            headers = [(name, value) for name, value in scope["headers"] if name != b"if-modified-since"]
            scope = {**scope, "headers": headers}

        async def send_revalidated(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"cache-control", b"no-cache")]}
            await send(message)

        await files(scope, receive, send_revalidated)

    return serve


@lru_cache(maxsize=1)
def sprite_icons() -> tuple[str, ...]:
    """Icon names in the kit sprite, in sprite order."""
    sprite = (STATIC_DIR / "kit" / "icons.svg").read_text(encoding="utf-8")
    return tuple(re.findall(r'<symbol id="([^"]+)"', sprite))


def ui_theme(request: Request) -> str:
    """The theme the viewer chose (cookie set by the shell), or ``system``."""
    theme = request.cookies.get(THEME_COOKIE)
    return theme if theme in THEMES else "system"


def ui_templates(*directories: str | Path) -> Jinja2Templates:
    """Jinja2Templates that reach the kit.

    ``directories`` (a plugin's own templates) are searched first, the shared
    templates last, so ``{% extends "kit/panel_base.html" %}`` and
    ``{% from "kit/macros.html" import icon %}`` resolve from any plugin.
    """
    search = [str(d) for d in directories] + [str(TEMPLATES_DIR)]
    templates = Jinja2Templates(directory=search)
    templates.env.globals["ui_theme"] = ui_theme
    return templates
