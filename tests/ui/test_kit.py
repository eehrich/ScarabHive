"""The UI kit's server side and the contracts every kit user relies on."""
from __future__ import annotations

import re
import xml.dom.minidom
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_system.ui.resources import STATIC_DIR, TEMPLATES_DIR, sprite_icons, ui_templates
from agent_system.ui.routes import router

REPO = Path(__file__).resolve().parents[2]
KIT = STATIC_DIR / "kit"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("cookie, expected", [
    ("dark", "dark"), ("light", "light"), ("system", "system"),
    ("neon", "system"), (None, "system"),
])
def test_the_page_paints_in_the_viewers_theme(client, cookie, expected):
    """The theme is set by the server, so a frame never flashes the wrong colours."""
    if cookie:
        client.cookies.set("ui_theme", cookie)

    response = client.get("/ui/kit")

    assert response.status_code == 200
    assert f'data-theme="{expected}"' in response.text


def test_the_kit_page_shows_every_icon_in_the_sprite(client):
    icons = sprite_icons()
    assert len(icons) > 50, "fixture: the sprite is nearly empty"

    page = client.get("/ui/kit").text

    missing = [name for name in icons if f"icons.svg#{name}" not in page]
    assert missing == []


def test_a_plugin_template_reaches_the_kit(tmp_path):
    """A plugin's own template directory is searched first, the kit second."""
    (tmp_path / "panel.html").write_text(
        '{% extends "kit/panel_base.html" %}{% from "kit/macros.html" import icon %}'
        '{% block title %}Probe{% endblock %}{% block content %}{{ icon("bug") }}{% endblock %}',
        encoding="utf-8")
    app = FastAPI()
    templates = ui_templates(tmp_path)

    @app.get("/panel")
    async def panel(request: Request):
        return templates.TemplateResponse(request, "panel.html")

    viewer = TestClient(app)
    viewer.cookies.set("ui_theme", "light")
    page = viewer.get("/panel").text

    assert "<title>Probe · ScarabHive</title>" in page
    assert 'data-theme="light"' in page
    assert "/static/kit/kit.css" in page and "icons.svg#bug" in page


@pytest.mark.parametrize("svg", sorted(p.name for p in KIT.glob("*.svg")))
def test_every_kit_svg_is_well_formed_xml(svg):
    """A '--' inside an XML comment made logo.svg invalid: every <use> of it
    rendered nothing, without an error anywhere."""
    xml.dom.minidom.parse(str(KIT / svg))


def _kit_users():
    """Templates and scripts built on the kit: the shell's own pages and every
    script in static/ (vendored libraries aside), plugin templates extending the
    kit base, and plugin scripts importing panel-kit.js."""
    files = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        files.append((path, path.read_text(encoding="utf-8")))
    for path in (REPO / "src").rglob("templates/**/*.html"):
        text = path.read_text(encoding="utf-8")
        if "kit/panel_base.html" in text:
            files.append((path, text))
    for path in STATIC_DIR.rglob("*.js"):
        if not path.is_relative_to(STATIC_DIR / "vendor"):
            files.append((path, path.read_text(encoding="utf-8")))
    for path in (REPO / "src").rglob("static/**/*.js"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"panel-kit\.js['\"]", text):
            files.append((path, text))
    return files


_ICON_CALL = re.compile(r"\b(?:icon|kitIcon)\(([^()]*)\)")
_ICON_NAME = re.compile(r"""(?<!size: )(?<!label: )(?<!size=)(?<!label=)['"]([a-z][\w-]*)['"]""")
# the empty state names its icon first (emptyState() in JS, the empty() macro); the rest is text
_EMPTY_CALL = re.compile(r"""\b(?:emptyState|empty)\(\s*['"]([a-z][\w-]*)['"]""")


def _icon_names(text: str) -> list[str]:
    """Every icon a file names: sprite references, icon() calls and the first argument of an empty state."""
    found = re.findall(r"icons\.svg#([\w-]+)", text)
    for call in _ICON_CALL.finditer(text):
        found += _ICON_NAME.findall(call.group(1))
    return found + _EMPTY_CALL.findall(text)


def test_every_icon_named_in_code_exists_in_the_sprite():
    names = set(sprite_icons())
    referenced = {}
    for path, text in _kit_users():
        for name in _icon_names(text):
            referenced.setdefault(name, path.name)
    assert {"x", "play", "shield"} <= set(referenced), "fixture: literal, ternary or sprite references went unseen"
    probe = "emptyState('only-in-an-empty-state', 'Titel', 'text')"
    assert _icon_names(probe) == ["only-in-an-empty-state"], "fixture: an empty state's icon went unseen"

    unknown = {name: where for name, where in referenced.items() if name not in names}

    assert unknown == {}


_NATIVE = re.compile(
    r"(?<![\w.$])(alert|confirm|prompt)\("
    r"|\b(?:window|globalThis|self|top|parent)\.(alert|confirm|prompt)\(")
_IMPORTED = re.compile(r"import\s*\{([^}]*)\}\s*from\s*['\"]/static/kit/panel-kit\.js['\"]")


def test_kit_users_make_no_native_dialog_calls():
    """alert/confirm/prompt block the page and silently returned false in the
    panel sandbox. A kit user calls the kit's versions -- imported by name --
    and never window.alert & co."""
    offenders = []
    for path, text in _kit_users():
        if path.name == "panel-kit.js":
            continue  # defines the replacements and the override
        imported = {name.strip().split(" as ")[-1] for m in _IMPORTED.finditer(text)
                    for name in m.group(1).split(",")}
        for match in _NATIVE.finditer(text):
            if match.group(2) or match.group(1) not in imported:
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.name}:{line}")
    assert offenders == []


def _panel_name(path: Path) -> str:
    """What a template and the script beside it have in common: the plugin, or the core panel's own name."""
    parts = path.relative_to(REPO).parts
    return parts[2] if parts[0] == "src" else path.stem


def test_a_panel_scoped_to_a_session_says_which_one():
    """`session.shown` decides which session a panel asks about -- and a link
    (?session_id=) can make that another one than the chat's, for as long as the
    panel lives. <pk-session> in the toolbar is what says so and offers the way
    back; a panel that takes the one without showing the other is the bug the
    element was built for.

    A tripwire, not a proof: it reads the two names off the source, so a panel
    that reaches the session another way (`const {shown} = session`) is invisible
    to it, and a plugin with two panels is satisfied by the element in either
    template. No plugin under src/plugins has two panels today. A panel that
    shows its session some other way -- message_debugger puts the id in a filter
    field the viewer can clear -- never reads `session.shown` and is not asked."""
    scoped, saying = {}, set()
    for path, text in _kit_users():
        if path.name == "panel-kit.js":
            continue  # where it is defined, not a panel that uses it
        if path.suffix == ".js" and ("session.shown" in text or "session.pinned" in text):
            scoped[_panel_name(path)] = path
        if path.suffix == ".html" and "<pk-session" in text:
            saying.add(_panel_name(path))
    assert len(scoped) > 3, "fixture: hardly a panel scopes to a session any more"

    silent = {name: str(path) for name, path in scoped.items() if name not in saying}

    assert silent == {}
