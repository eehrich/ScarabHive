"""Where templates and static files live, and how a panel template reaches the kit."""
from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.templating import Jinja2Templates

THEMES = ("dark", "light", "system")
THEME_COOKIE = "ui_theme"


def find_resource_dir(name: str) -> Path:
    """Find the templates or static directory in the supported install layouts."""
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
