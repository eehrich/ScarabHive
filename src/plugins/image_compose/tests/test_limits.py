"""Bounds on what a model-written call can make the plugin touch or hold.

Host paths are refused on their text before any file system call (resolving
\\\\host\\share signs in to that host); every decoded source and every sized
layer is capped at MAX_PIXELS; find_region's max_candidates and the render's
warning list are bounded.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from PIL import Image

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.image_compose import compositor
from plugins.image_compose.server import MAX_WARNINGS, PLUGIN_FACTORY

HOSTS = ["\\\\host\\share\\x.png", "//host/share/x.png"]


@pytest.fixture
def server(tmp_path):
    config = ToolServerConfig(type="image_compose", enabled=True, fonts_dir=str(tmp_path / "fonts"))
    srv = PLUGIN_FACTORY(name="images", system_config=AgentSystemConfig(), server_config=config)
    srv.project_root = tmp_path
    return srv


@pytest.fixture
def no_host_access(monkeypatch):
    """Any Path call on the host fails loudly, so a missing guard shows up
    as a different error than the refusal."""
    for name in ("resolve", "exists", "is_file"):
        original = getattr(Path, name)

        def spy(self, *a, _original=original, **kw):
            if str(self).replace("/", "\\").startswith("\\\\host\\"):
                raise AssertionError("touched the host")
            return _original(self, *a, **kw)
        monkeypatch.setattr(Path, name, spy)


def small_spec(**layer):
    return {"size": [8, 8], "layers": [layer or {"type": "rect", "rect": [0, 0, 4, 4], "fill": "#f00"}]}


def refused(result):
    return result["status"] == "error" and "network or device path" in result["error"]


# ── host paths ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("field", ["output_path", "layers_dir", "spec_path"])
async def test_a_host_path_to_write_is_refused_on_its_text(server, no_host_access, tmp_path, host, field):
    params = {"spec": small_spec(), "output_path": str(tmp_path / "ok.png"), "include_content": False}
    params[field] = host
    assert refused(await server.render(params))


@pytest.mark.parametrize("host", HOSTS)
async def test_an_image_layer_from_a_host_is_refused(server, no_host_access, tmp_path, host):
    result = await server.render({"spec": small_spec(type="image", src=host),
                                  "output_path": str(tmp_path / "ok.png"), "layers_dir": ""})
    assert refused(result)


async def test_a_font_from_a_host_is_refused(server, no_host_access, tmp_path):
    result = await server.render({"spec": small_spec(type="text", text="A", font=HOSTS[0]),
                                  "output_path": str(tmp_path / "ok.png"), "layers_dir": ""})
    assert refused(result)


@pytest.mark.parametrize("host", HOSTS)
async def test_analyze_and_find_region_refuse_a_host(server, no_host_access, host):
    assert refused(await server.analyze({"path": host}))
    assert refused(await server.find_region({"path": host, "region_size": [4, 4]}))


# ── pixel caps ────────────────────────────────────────────────────────────

@pytest.fixture
def tiny_cap(monkeypatch):
    monkeypatch.setattr(compositor, "MAX_PIXELS", 32 * 32)


def test_the_cap_is_the_canvas_cap():
    with pytest.raises(compositor.CompositionError):
        compositor._parse_size([8193, 8192])
    assert compositor.MAX_PIXELS == 8192 * 8192


def too_large(result):
    return result["status"] == "error" and "too large" in result["error"]


async def test_an_oversized_source_is_refused_before_decoding(server, tiny_cap, tmp_path, monkeypatch):
    big = tmp_path / "big.png"
    Image.new("RGB", (33, 32)).save(big)
    loads = []
    original_load = Image.Image.load

    def spy_load(self):
        if getattr(self, "filename", "") == str(big):
            loads.append(self)
        return original_load(self)
    monkeypatch.setattr(Image.Image, "load", spy_load)
    assert too_large(await server.analyze({"path": str(big)}))
    assert too_large(await server.find_region({"path": str(big), "region_size": [4, 4]}))
    render = await server.render({"spec": small_spec(type="image", src=str(big)),
                                  "output_path": str(tmp_path / "o.png"), "layers_dir": ""})
    assert too_large(render)
    assert loads == [], "the pixels were decoded"


@pytest.mark.parametrize("layer", [
    {"type": "rect", "rect": [0, 0, 33, 32], "fill": "#f00"},
    {"type": "gradient", "size": [33, 32], "colors": ["#000", "#fff"]},
    {"type": "text", "text": "W", "size": 60},
])
async def test_an_oversized_layer_is_refused(server, tiny_cap, tmp_path, layer):
    result = await server.render({"spec": small_spec(**layer),
                                  "output_path": str(tmp_path / "o.png"), "layers_dir": ""})
    assert too_large(result), result


async def test_a_sliver_scaled_to_cover_is_refused(server, tiny_cap, tmp_path):
    """The target fits; the intermediate the cover fit scales to does not."""
    sliver = tmp_path / "sliver.png"
    Image.new("RGB", (1, 30)).save(sliver)
    result = await server.render({"spec": small_spec(type="image", src=str(sliver), size=[30, 30]),
                                  "output_path": str(tmp_path / "o.png"), "layers_dir": ""})
    assert too_large(result), result


@pytest.mark.skipif(importlib.util.find_spec("svglib") is None, reason="svglib missing")
async def test_an_svg_declaring_a_huge_size_is_refused(server, tmp_path):
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100000" height="100000"><rect width="10" height="10"/></svg>'
    result = await server.render({"spec": small_spec(type="svg", svg=svg),
                                  "output_path": str(tmp_path / "o.png"), "layers_dir": ""})
    assert too_large(result), result


# ── bounded answers ───────────────────────────────────────────────────────

@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "p.png"
    Image.new("RGB", (64, 64), (40, 40, 40)).save(path)
    return str(path)


@pytest.mark.parametrize("given, expected", [(0, 1), (-3, 1), (50, 10), ("2", 2)])
async def test_max_candidates_is_held_to_one_to_ten(server, picture, given, expected):
    result = await server.find_region({"path": picture, "region_size": [8, 8], "max_candidates": given})
    assert result["status"] == "success", result
    assert len(result["candidates"]) == expected


async def test_a_non_number_max_candidates_is_an_error_answer(server, picture):
    result = await server.find_region({"path": picture, "region_size": [8, 8], "max_candidates": "many"})
    assert result["status"] == "error" and result["error_type"] == "ValidationError"


class StatusSpy:
    def __init__(self):
        self.lines = []

    async def progress(self, message, meta=None):
        self.lines.append(message)

    async def end(self, message="completed", meta=None):
        self.lines.append(message)

    async def error(self, message, meta=None):
        self.lines.append(message)


async def test_the_warning_list_is_capped(server, tmp_path):
    layers = [{"type": "text", "text": "A", "size": 10, "position": [5, 5]} for _ in range(10)]
    status = StatusSpy()
    result = await server.render({"spec": {"size": [64, 64], "layers": layers},
                                  "output_path": str(tmp_path / "o.png"), "layers_dir": "",
                                  "include_content": False, "_status": status})
    assert status.lines[-1].endswith("45 warning(s)"), status.lines
    assert result["status"] == "warning"
    assert len(result["warnings"]) == MAX_WARNINGS + 1  # 45 pairs overlap
    assert result["warnings"][-1] == f"... and {45 - MAX_WARNINGS} more warnings"


@pytest.mark.parametrize("given", [float("inf"), 1e999])
async def test_an_infinite_max_candidates_is_an_error_answer(server, picture, given):
    result = await server.find_region({"path": picture, "region_size": [8, 8], "max_candidates": given})
    assert result["status"] == "error" and result["error_type"] == "ValidationError"


# ── svg ───────────────────────────────────────────────────────────────────

needs_svglib = pytest.mark.skipif(importlib.util.find_spec("svglib") is None, reason="svglib missing")
#: Drawing also needs reportlab's cairo backend (requirements/optional.txt).
needs_svg_backend = pytest.mark.skipif(
    importlib.util.find_spec("svglib") is None or importlib.util.find_spec("rlPyCairo") is None,
    reason="svglib or reportlab's cairo backend missing")


def svg_text(family):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
            f'<text x="1" y="10" font-family="{family}">A</text></svg>')


@needs_svg_backend
async def test_an_svg_font_family_that_is_a_path_opens_no_file(server, tmp_path, monkeypatch):
    """svglib turns an unknown family into "<family>.ttf" and opens it."""
    import svglib.fonts
    from reportlab.pdfbase.ttfonts import TTFError

    opened = []

    def spy(name, path, *a, **kw):
        opened.append(str(path))
        raise TTFError("spy")
    monkeypatch.setattr(svglib.fonts, "TTFont", spy)

    for family in ("//evilhost/share/f", r"\\evilhost\share\f", "nosuchfamily"):
        result = await server.render({"spec": small_spec(type="svg", svg=svg_text(family)),
                                      "output_path": str(tmp_path / "o.png"), "layers_dir": ""})
        assert result["status"] in ("success", "warning"), result
    assert not [p for p in opened if "evilhost" in p], opened
    assert any("nosuchfamily" in p for p in opened), "the spy saw no font lookup at all"


@needs_svglib
async def test_an_oversized_raster_inside_an_svg_is_refused(server, tiny_cap, tmp_path):
    import base64
    import io

    buf = io.BytesIO()
    Image.new("RGB", (33, 32)).save(buf, format="PNG")
    payload = base64.b64encode(buf.getvalue()).decode()
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           'width="20" height="20">'
           f'<image width="20" height="20" xlink:href="data:image/png;base64,{payload}"/></svg>')
    result = await server.render({"spec": small_spec(type="svg", svg=svg),
                                  "output_path": str(tmp_path / "o.png"), "layers_dir": ""})
    assert too_large(result), result
