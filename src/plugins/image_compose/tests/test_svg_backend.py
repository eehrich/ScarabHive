"""SVG layers on a host without reportlab's cairo backend.

The backend (``reportlab[pycairo]``) is an optional dependency since 2026-10:
pycairo builds from source outside Windows, and as a hard dependency it
stopped ``pip install -e .`` on a fresh Mac. Without it, reportlab fails only
when it draws, with "cannot import desired renderPM backend rlPyCairo" and a
mailing list to ask. The plugin has to say what to do instead -- in the tool
result and, before any model calls it, in the log at start.

The backend is made unavailable the way Python sees a missing package:
``sys.modules["rlPyCairo"] = None`` makes ``import rlPyCairo`` raise
ImportError, in the plugin's check and in reportlab's own loader alike.
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sys
import sysconfig
from pathlib import Path

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.tools.status import StatusPhase, get_status_bus
from plugins.image_compose import compositor
from plugins.image_compose.server import PLUGIN_FACTORY

REPO = Path(__file__).resolve().parents[4]
HAS_SVGLIB = importlib.util.find_spec("svglib") is not None
#: Found independently of the plugin's own check: a check that wrongly said
#: "missing" must not be able to skip the test that would catch it.
HAS_CAIRO_BACKEND = importlib.util.find_spec("rlPyCairo") is not None
#: The status row's budget (tests/plugins/test_status_end_lines.py: MAX_LINE).
MAX_LINE = 140

CIRCLE = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
          '<circle cx="20" cy="20" r="10" fill="#f00"/></svg>')


@pytest.fixture
def no_cairo_backend(monkeypatch):
    monkeypatch.setitem(sys.modules, "rlPyCairo", None)


def make_server(tmp_path):
    config = ToolServerConfig(type="image_compose", enabled=True, fonts_dir=str(tmp_path / "fonts"))
    server = PLUGIN_FACTORY(name="images", system_config=AgentSystemConfig(), server_config=config)
    server.project_root = tmp_path
    return server


async def render_with_status(server, params):
    """One real tool call through call_with_status; returns the result and
    the event that closed its status scope."""
    bus = get_status_bus()
    queue = await bus.subscribe(server=f"{server.name}.render()")
    try:
        result = await server.call_with_status(f"{server.name}_render", params)
    finally:
        bus.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    closing = [e for e in events if e.phase in (StatusPhase.END, StatusPhase.ERROR)]
    assert len(closing) == 1, [(e.phase, e.message) for e in events]
    return result, closing[0]


def install_sh_commands(headers: bool) -> dict[str, str]:
    """The system package commands install.sh names, by the tool they run, as
    install.sh fills them in for this Python (the server's) with or without
    its headers."""
    text = (REPO / "install.sh").read_text(encoding="utf-8")
    [base] = re.findall(r'^APT_PACKAGES="([^"$]+)"$', text, re.M)
    [extra] = re.findall(r'^has_headers \|\| APT_PACKAGES="\$APT_PACKAGES ([^"]+)"$', text, re.M)
    packages = base if headers else f"{base} {extra.replace('$PYVER', '%d.%d' % sys.version_info[:2])}"
    commands = [c.replace("$APT_PACKAGES", packages) for c in re.findall(r'CAIRO_CMD="([^"]+)"', text)]
    assert commands, "install.sh names no CAIRO_CMD -- this test would be vacuous"
    return {c.split()[1] if c.startswith("sudo ") else c.split()[0]: c for c in commands}


async def render_svg(tmp_path):
    return await render_with_status(make_server(tmp_path), {
        "spec": {"size": [64, 64], "layers": [{"type": "svg", "svg": CIRCLE}]},
        "output_path": str(tmp_path / "o.png"), "layers_dir": "", "include_content": False})


@pytest.mark.skipif(not HAS_SVGLIB, reason="svglib/reportlab not installed")
async def test_an_svg_layer_without_the_cairo_backend_answers_with_the_fix(tmp_path, no_cairo_backend):
    server = make_server(tmp_path)
    result, closing = await render_with_status(server, {
        "spec": {"size": [64, 64], "layers": [{"type": "svg", "svg": CIRCLE}]},
        "output_path": str(tmp_path / "o.png"), "layers_dir": "", "include_content": False})

    assert result["status"] == "error" and result["error_type"] == "CompositionError", result
    error = result["error"]
    assert error.startswith("layer 0 (svg): "), error
    assert "cannot import desired renderPM backend" not in error, "reportlab's own message got through"
    # The file it names exists and carries the backend.
    named = re.findall(r"requirements/[\w.-]+\.txt", error)
    assert named, error
    for name in named:
        assert "pycairo" in (REPO / name).read_text(encoding="utf-8"), f"{name} does not install the backend"
    # The system packages it names are the ones install.sh installs here.
    headers = (Path(sysconfig.get_paths()["include"]) / "Python.h").exists()
    commands = install_sh_commands(headers)
    for tool in ("brew", "apt-get"):
        # Up to the ";" that ends it: a package more is a different command.
        assert f"{commands[tool]};" in error, f"the error does not give install.sh's {tool} command: {error}"
    assert not (tmp_path / "o.png").exists(), "an error result with a picture on disk"
    assert closing.phase is StatusPhase.ERROR and len(closing.message) <= MAX_LINE, closing.message


@pytest.mark.skipif(not HAS_SVGLIB, reason="svglib/reportlab not installed")
async def test_the_size_checks_answer_before_the_backend_is_asked(tmp_path, no_cairo_backend):
    """Parsing and the pixel cap need no backend: on a host without it, an
    oversized SVG is still refused for its size, not for the missing backend."""
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100000" height="100000"><rect width="10" height="10"/></svg>'
    with pytest.raises(compositor.CompositionError) as raised:
        compositor.compose({"size": [8, 8], "layers": [{"type": "svg", "svg": svg}]},
                           tmp_path / "o.png", tmp_path, {}, tmp_path)
    assert "cairo" not in str(raised.value), raised.value


def test_the_server_warns_at_start_when_svg_layers_cannot_be_drawn(tmp_path, no_cairo_backend, caplog):
    problem = compositor.svg_backend_problem()
    assert problem, "fixture: the backend is still importable"
    with caplog.at_level(logging.WARNING, logger="plugins.image_compose.server"):
        make_server(tmp_path)
    warnings = [r.getMessage() for r in caplog.records
                if r.name == "plugins.image_compose.server" and r.levelno == logging.WARNING]
    assert len(warnings) == 1 and problem in warnings[0], warnings


@pytest.mark.skipif(not (HAS_SVGLIB and HAS_CAIRO_BACKEND), reason="reportlab's cairo backend not installed")
def test_a_host_with_the_backend_hears_nothing_and_draws(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="plugins.image_compose.server"):
        make_server(tmp_path)
    assert not [r for r in caplog.records if r.name == "plugins.image_compose.server"
                and r.levelno >= logging.WARNING], caplog.text
    meta = compositor.compose({"size": [64, 64], "layers": [{"type": "svg", "svg": CIRCLE}]},
                              tmp_path / "o.png", tmp_path, {}, tmp_path)
    assert meta["layers_rendered"] == 1


@pytest.mark.skipif(not HAS_SVGLIB, reason="svglib/reportlab not installed")
async def test_without_python_h_the_hint_names_this_pythons_headers(tmp_path, no_cairo_backend, monkeypatch):
    """A Python without Python.h needs pythonX.Y-dev, as install.sh asks for it;
    one with its headers must not get a package name apt may not know."""
    monkeypatch.setattr(compositor.sysconfig, "get_paths", lambda: {"include": str(tmp_path)})
    result, _ = await render_svg(tmp_path)
    assert f'{install_sh_commands(headers=False)["apt-get"]};' in result["error"], result["error"]
    assert "python%d.%d-dev" % sys.version_info[:2] in result["error"]


@pytest.mark.skipif(not HAS_SVGLIB, reason="svglib/reportlab not installed")
async def test_an_svglib_that_cannot_import_without_the_backend_answers_with_the_fix(tmp_path, no_cairo_backend, monkeypatch):
    """reportlab 4.0.0 fails already in svglib's import without a backend
    (measured: "Could not create text2PathDescription ..."); that must not
    read as "install svglib and reportlab", which are installed."""
    import svglib
    monkeypatch.setitem(sys.modules, "svglib.svglib", None)
    monkeypatch.delattr(svglib, "svglib", raising=False)
    result, _ = await render_svg(tmp_path)
    assert result["status"] == "error" and "requirements/optional.txt" in result["error"], result
    assert "pip install svglib reportlab" not in result["error"], result["error"]


@pytest.mark.skipif(not HAS_SVGLIB, reason="svglib/reportlab not installed")
@pytest.mark.parametrize("version, drawable", [("4.4.4", True), ("5.0.1", False)])
def test_reportlab_4_draws_through_its_old_backend_and_5_does_not(monkeypatch, no_cairo_backend, version, drawable):
    """reportlab 4 falls back to _rl_renderPM without rlPyCairo
    (renderPM._getPMBackend); 5 has no fallback."""
    import reportlab
    monkeypatch.setattr(reportlab, "Version", version)
    monkeypatch.setitem(sys.modules, "_rl_renderPM", type(sys)("_rl_renderPM"))
    assert (compositor.svg_backend_problem() is None) is drawable
