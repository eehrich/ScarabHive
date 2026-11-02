"""The UI kit's server side and the contracts every kit user relies on."""
from __future__ import annotations

import re
import xml.dom.minidom
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_system.ui.resources import STATIC_DIR, STYLES, TEMPLATES_DIR, sprite_icons, ui_templates
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


@pytest.mark.parametrize("cookie, expected", [
    ("classic", "classic"), ("apple", "apple"), ("petrol", "petrol"), ("neon", "classic"), (None, "classic"),
])
def test_the_page_paints_in_the_viewers_style(client, cookie, expected):
    """The style is set by the server as the theme is: a frame never flashes the other look."""
    if cookie:
        client.cookies.set("ui_style", cookie)

    response = client.get("/ui/kit")

    assert response.status_code == 200
    assert f'data-style="{expected}"' in response.text


def _style_tokens(style: str) -> dict[str, tuple[str, str]]:
    """The colour tokens a style paints with, as (light, dark): classic's from :root, a style's own over them."""
    css = (KIT / "kit.css").read_text(encoding="utf-8")

    def block(selector: str) -> str:
        match = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.S)
        assert match, f"kit.css has no block {selector}"
        return match.group(1)

    def pairs(text: str) -> dict[str, tuple[str, str]]:
        return {name: (light, dark) for name, light, dark in
                re.findall(r"(--[\w-]+):\s*light-dark\((#[0-9a-fA-F]{6}),\s*(#[0-9a-fA-F]{6})\)", text)}

    tokens = pairs(block(":root"))
    if style != "classic":
        tokens.update(pairs(block(f':root[data-style="{style}"]')))
    return tokens


def _contrast(a: str, b: str) -> float:
    def luminance(colour: str) -> float:
        channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("style", STYLES)
def test_every_style_reads_at_wcag_aa_in_light_and_dark(style):
    """Text at 4.5:1 or more (WCAG 2.1 AA) on the surfaces it sits on, in both themes: muted text on the grounds,
    white on the accent, accent text on cards, the strong badge's text on danger."""
    tokens = _style_tokens(style)
    white = ("#ffffff", "#ffffff")
    pairs = [("--text-primary", "--surface-0"), ("--text-secondary", "--surface-2"),
             *(("--text-muted", f"--surface-{n}") for n in (0, 1, 2)),
             ("--accent-text", "--surface-2"), (white, "--accent"), ("--danger-strong-fg", "--danger")]
    for fore, back in pairs:
        for theme in (0, 1):
            fg = fore[theme] if isinstance(fore, tuple) else tokens[fore][theme]
            ratio = _contrast(fg, tokens[back][theme])
            assert ratio >= 4.5, f"{style} {'dark' if theme else 'light'}: {fore} on {back} is {ratio:.2f}:1"


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
# an operand of a comparison (`mode === 'person' ? 'user' : 'brain'`) is a value, not an icon
_ICON_NAME = re.compile(r"""(?<!size: )(?<!label: )(?<!size=)(?<!label=)(?<!== )(?<!==)['"]([a-z][\w-]*)['"]""")
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
    compared = "icon(mode === 'person' ? 'user' : 'brain')"
    assert _icon_names(compared) == ["user", "brain"], "fixture: a compared value was taken for an icon"

    unknown = {name: where for name, where in referenced.items() if name not in names}

    assert unknown == {}


def test_the_sprite_holds_exactly_the_icons_its_generator_lists():
    """icons.svg is built from build_icon_sprite.ICONS: an icon put into the sprite by hand is gone after the next
    build, and the buttons that show it are empty."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_icon_sprite", REPO / "src" / "scripts" / "build_icon_sprite.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)

    assert sorted(sprite_icons()) == sorted(generator.ICONS)


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


def _js_object(name: str, text: str) -> dict[str, str]:
    """A flat object literal of string values from panel-kit.js (``name = { key: 'value', ... }``), the first one."""
    body = re.search(rf"\b{name}\s*[=:]\s*\{{([^}}]*)\}}", text).group(1)
    return dict(re.findall(r"(\w+):\s*'([^']*)'", body))


def _js_words(script: str, lang: str) -> dict[str, str]:
    """The status words of one language in panel-kit.js's WORDS table (its ``<lang>: { ... },`` block)."""
    block = re.search(rf"\n  {lang}: \{{\n(.*?)\n  \}},", script, re.DOTALL).group(1)
    return _js_object("statuses", block)


def test_the_status_macro_and_its_script_twin_draw_the_same_set():
    """status() in panel-kit.js and the status() macro both draw .pk-status: the same names, the same icon for each,
    the same word in each language the kit speaks, every icon in the sprite -- a list drawn by the script and a page
    drawn by the template show one state alike, on an English page and on a German one."""
    script = (KIT / "panel-kit.js").read_text(encoding="utf-8")
    icons = _js_object("STATUS_ICONS", script)
    words = {lang: _js_words(script, lang) for lang in ("en", "de")}
    assert len(icons) == 9 and all(set(w) == set(icons) for w in words.values()), "fixture: the status set was not read"
    assert words["en"] != words["de"], "fixture: the two languages read the same table"
    macros = ui_templates().env.get_template("kit/macros.html").module

    drawn = {lang: {name: str(macros.status(name, label=True, lang=lang)) for name in icons} for lang in words}

    assert {name: re.search(r"icons\.svg#([\w-]+)", markup).group(1) for name, markup in drawn["en"].items()} == icons
    for lang, markups in drawn.items():
        assert {name: re.search(r'pk-status-label">([^<]*)<', markup).group(1)
                for name, markup in markups.items()} == words[lang], lang
    assert set(icons.values()) <= set(sprite_icons())
    assert f"icons.svg#{icons['pending']}" in str(macros.status("no-such-state")), "an unknown name draws as pending"


def test_the_steps_macro_says_each_state_in_words_not_only_in_its_drawing():
    """done, warn and ahead are a tick, a "!" and a ring of CSS; a screen reader hears them from hidden text."""
    macros = ui_templates().env.get_template("kit/macros.html").module
    items = [{"label": "Plan", "state": "done"}, {"label": "Build", "state": "current"},
             {"label": "Test", "state": "warn"}, {"label": "Ship"}]
    drawn = str(macros.steps(items, lang="de-DE"))
    for said in ("Plan<span class=\"pk-sr-only\"> (erledigt)", "Test<span class=\"pk-sr-only\"> (braucht Aufmerksamkeit)",
                 "Ship<span class=\"pk-sr-only\"> (noch offen)"):
        assert said in drawn, drawn
    assert 'aria-current="step"><span class="pk-step-label">Build</span>' in drawn, "the current one has aria-current"
    assert "(done)" in str(macros.steps(items)) and "(done)" in str(macros.steps(items, lang="fr"))
