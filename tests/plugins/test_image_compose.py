"""Tests for the image_compose plugin.

Focuses on the pure-Python compositor — server.py is a thin async wrapper
and is exercised end-to-end via a smoke test.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from PIL import Image

from plugins.image_compose.compositor import (
    ANCHORS,
    CompositionError,
    analyze_image,
    compose,
    find_text_region,
    _direction_to_angle,
    _fit_image,
    _interpolate_gradient,
    _parse_color,
    _parse_size,
    _resolve_position,
    _resolve_size_field,
    _wrap_text,
)


HAS_SVGLIB = importlib.util.find_spec("svglib") is not None


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def fonts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "fonts"
    d.mkdir()
    return d


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def out_path(tmp_path: Path) -> Path:
    return tmp_path / "out.png"


def _render(spec, tmp_path, fonts_dir, ext="png"):
    out = tmp_path / f"out.{ext}"
    meta = compose(spec, out, fonts_dir, {}, tmp_path)
    return out, meta


# ── Per-layer PNG export ──────────────────────────────────────────────────

class TestLayerExport:
    def test_layers_dir_writes_one_png_per_layer(self, tmp_path, fonts_dir):
        layers_dir = tmp_path / "layers"
        out = tmp_path / "out.png"
        spec = {
            "size": [200, 300], "background": "#222222",
            "layers": [
                {"type": "gradient", "rect": [0, 150, 200, 150],
                 "colors": ["#00000000", "#000000cc"], "direction": "bottom"},
                {"type": "text", "text": "Titel", "font": "serif_bold",
                 "size": 32, "color": "#ffffff", "position": {"anchor": "center"}},
            ],
        }
        meta = compose(spec, out, fonts_dir, {}, tmp_path, layers_dir=layers_dir)
        assert len(meta["layer_files"]) == 2
        names = sorted(Path(p).name for p in meta["layer_files"])
        assert names == ["layer_00_gradient.png", "layer_01_text.png"]
        for p in meta["layer_files"]:
            img = Image.open(p)
            assert img.size == (200, 300)
            assert img.mode == "RGBA"

    def test_no_layers_dir_means_no_export(self, tmp_path, fonts_dir):
        _, meta = _render({
            "size": [100, 100],
            "layers": [{"type": "rect", "rect": [0, 0, 50, 50], "fill": "#ff0000"}],
        }, tmp_path, fonts_dir)
        assert meta["layer_files"] == []

    def test_recompose_clears_stale_layer_files(self, tmp_path, fonts_dir):
        """A re-composition with fewer/different layers must not leave orphan
        layer PNGs from the previous run in layers_dir."""
        layers_dir = tmp_path / "layers"
        out = tmp_path / "out.png"
        # First compose: 3 layers (image-ish rect, gradient, text)
        compose({
            "size": [200, 200], "background": "#111111",
            "layers": [
                {"type": "rect", "rect": [0, 0, 200, 200], "fill": "#222222"},
                {"type": "gradient", "rect": [0, 100, 200, 100],
                 "colors": ["#00000000", "#000000cc"], "direction": "bottom"},
                {"type": "text", "text": "Hi", "color": "#ffffff",
                 "position": [10, 10]},
            ],
        }, out, fonts_dir, {}, tmp_path, layers_dir=layers_dir)
        assert (layers_dir / "layer_01_gradient.png").exists()

        # Re-compose: gradient dropped, only 2 layers now
        meta = compose({
            "size": [200, 200], "background": "#111111",
            "layers": [
                {"type": "rect", "rect": [0, 0, 200, 200], "fill": "#222222"},
                {"type": "text", "text": "Hi", "color": "#ffffff",
                 "position": [10, 10]},
            ],
        }, out, fonts_dir, {}, tmp_path, layers_dir=layers_dir)
        # The stale gradient PNG must be gone
        assert not (layers_dir / "layer_01_gradient.png").exists()
        remaining = sorted(p.name for p in layers_dir.glob("layer_*.png"))
        assert remaining == ["layer_00_rect.png", "layer_01_text.png"]
        assert len(meta["layer_files"]) == 2

    def test_exported_layer_is_isolated(self, tmp_path, fonts_dir):
        """Each exported layer PNG holds only that layer on transparency."""
        layers_dir = tmp_path / "layers"
        out = tmp_path / "out.png"
        spec = {
            "size": [100, 100], "background": "#0000ff",
            "layers": [{"type": "rect", "rect": [10, 10, 30, 30], "fill": "#ff0000"}],
        }
        meta = compose(spec, out, fonts_dir, {}, tmp_path, layers_dir=layers_dir)
        layer = Image.open(meta["layer_files"][0]).convert("RGBA")
        # inside the rect: red, opaque
        assert layer.getpixel((20, 20)) == (255, 0, 0, 255)
        # outside: transparent (NOT the blue background)
        assert layer.getpixel((80, 80)) == (0, 0, 0, 0)


# ── Color parsing ─────────────────────────────────────────────────────────

class TestParseColor:
    def test_rgb_hex(self):
        assert _parse_color("#ff0000") == (255, 0, 0, 255)

    def test_rgba_hex(self):
        assert _parse_color("#ff000080") == (255, 0, 0, 0x80)

    def test_shorthand_3(self):
        assert _parse_color("#f00") == (255, 0, 0, 255)

    def test_shorthand_4(self):
        assert _parse_color("#f008") == (255, 0, 0, 0x88)

    def test_rgb_array(self):
        assert _parse_color([10, 20, 30]) == (10, 20, 30, 255)

    def test_rgba_array(self):
        assert _parse_color([10, 20, 30, 128]) == (10, 20, 30, 128)

    @pytest.mark.parametrize("bad", ["red", "#xxxxxx", "#12345", "", 42, None])
    def test_invalid_raises(self, bad):
        with pytest.raises(CompositionError):
            _parse_color(bad)


# ── Size parsing ──────────────────────────────────────────────────────────

class TestParseSize:
    def test_basic(self):
        assert _parse_size([100, 200]) == (100, 200)

    def test_tuple(self):
        assert _parse_size((100, 200)) == (100, 200)

    @pytest.mark.parametrize("bad", [[0, 200], [100, 0], [-1, 100], [100], [100, 200, 300], "100x200", None])
    def test_invalid(self, bad):
        with pytest.raises(CompositionError):
            _parse_size(bad)

    def test_too_large(self):
        with pytest.raises(CompositionError, match="too large"):
            _parse_size([9000, 100])


# ── Position resolution ───────────────────────────────────────────────────

class TestResolvePosition:
    def test_pixel_tuple(self):
        assert _resolve_position([50, 100], (40, 40), (200, 300)) == (50, 100)

    def test_none_is_origin(self):
        assert _resolve_position(None, (40, 40), (200, 300)) == (0, 0)

    def test_anchor_top_left(self):
        assert _resolve_position({"anchor": "top_left"}, (40, 40), (200, 300)) == (0, 0)

    def test_anchor_bottom_right(self):
        # Layer's bottom-right at canvas bottom-right (200,300) → top-left at (160,260)
        pos = _resolve_position({"anchor": "bottom_right"}, (40, 40), (200, 300))
        assert pos == (160, 260)

    def test_anchor_center(self):
        # Centered: layer center on canvas center (100,150) → top-left at (80,130)
        pos = _resolve_position({"anchor": "center"}, (40, 40), (200, 300))
        assert pos == (80, 130)

    def test_anchor_with_offset(self):
        pos = _resolve_position({"anchor": "bottom_center", "offset": [0, -20]}, (40, 40), (200, 300))
        # bottom_center on canvas = (100, 300); layer's bottom_center anchor = (20, 40)
        # → top-left = (100-20+0, 300-40+(-20)) = (80, 240)
        assert pos == (80, 240)

    def test_anchor_with_offset_pct(self):
        # 10% of 200 = 20, 10% of 300 = 30
        pos = _resolve_position({"anchor": "top_left", "offset_pct": [10, 10]}, (40, 40), (200, 300))
        assert pos == (20, 30)

    def test_unknown_anchor(self):
        with pytest.raises(CompositionError, match="unknown anchor"):
            _resolve_position({"anchor": "middle_of_nowhere"}, (40, 40), (200, 300))

    def test_invalid_position(self):
        # a non-anchor bare string is invalid
        with pytest.raises(CompositionError):
            _resolve_position("somewhere", (40, 40), (200, 300))
        # a bare int is invalid (ambiguous)
        with pytest.raises(CompositionError):
            _resolve_position(868, (40, 40), (200, 300))

    def test_bare_anchor_string_accepted(self):
        # a valid anchor name passed as a bare string works like {"anchor": ...}
        assert _resolve_position("center", (40, 40), (200, 300)) == \
            _resolve_position({"anchor": "center"}, (40, 40), (200, 300))
        assert _resolve_position("bottom_center", (40, 40), (200, 300)) == \
            _resolve_position({"anchor": "bottom_center"}, (40, 40), (200, 300))

    def test_all_anchors_resolve(self):
        for name in ANCHORS:
            pos = _resolve_position({"anchor": name}, (40, 40), (200, 300))
            assert isinstance(pos, tuple) and len(pos) == 2


# ── Size field resolution ─────────────────────────────────────────────────

class TestResolveSizeField:
    def test_explicit_list(self):
        assert _resolve_size_field([100, 200], (1024, 1536)) == (100, 200)

    def test_pct_dict(self):
        assert _resolve_size_field({"width_pct": 50, "height_pct": 25}, (1024, 1600)) == (512, 400)

    def test_none_returns_default(self):
        assert _resolve_size_field(None, (1024, 1600), default=(50, 50)) == (50, 50)

    def test_partial_preserves_aspect(self):
        # default 100x50 (2:1) — providing only height=200 → width=400
        result = _resolve_size_field({"height": 200}, (1024, 1600), default=(100, 50))
        assert result == (400, 200)


# ── Direction → angle ─────────────────────────────────────────────────────

class TestDirection:
    @pytest.mark.parametrize("name,expected", [
        ("right", 0), ("bottom", 90), ("left", 180), ("top", 270),
        ("bottom_right", 45), ("top_left", 225),
    ])
    def test_named(self, name, expected):
        assert _direction_to_angle(name) == expected

    def test_numeric_passthrough(self):
        assert _direction_to_angle(135) == 135.0

    def test_unknown(self):
        with pytest.raises(CompositionError):
            _direction_to_angle("diagonal-ish")


# ── Gradient interpolation ────────────────────────────────────────────────

class TestGradientInterp:
    def test_clamps_below(self):
        c = _interpolate_gradient([(0, 0, 0, 255), (255, 255, 255, 255)], [0.0, 1.0], -0.5)
        assert c == (0, 0, 0, 255)

    def test_clamps_above(self):
        c = _interpolate_gradient([(0, 0, 0, 255), (255, 255, 255, 255)], [0.0, 1.0], 1.5)
        assert c == (255, 255, 255, 255)

    def test_midpoint(self):
        c = _interpolate_gradient([(0, 0, 0, 0), (200, 100, 50, 200)], [0.0, 1.0], 0.5)
        assert c == (100, 50, 25, 100)

    def test_multi_stop(self):
        c = _interpolate_gradient(
            [(0, 0, 0, 0), (255, 0, 0, 255), (0, 255, 0, 255)],
            [0.0, 0.5, 1.0],
            0.75,
        )
        # halfway between red and green
        assert c == (127, 127, 0, 255)


# ── Text wrapping ─────────────────────────────────────────────────────────

class TestWrapText:
    def test_no_max_width_returns_lines(self, fonts_dir):
        from PIL import ImageFont
        font = ImageFont.load_default()
        assert _wrap_text("hello world", font, None, 0) == ["hello world"]

    def test_newlines_split(self, fonts_dir):
        from PIL import ImageFont
        font = ImageFont.load_default()
        assert _wrap_text("a\nb\nc", font, None, 0) == ["a", "b", "c"]


# ── Image fitting ─────────────────────────────────────────────────────────

class TestFitImage:
    def test_stretch(self):
        src = Image.new("RGBA", (100, 50), (255, 0, 0, 255))
        out = _fit_image(src, (200, 200), "stretch")
        assert out.size == (200, 200)

    def test_contain_preserves_aspect(self):
        # 2:1 source into 100x100 → 100x50 with transparent letterbox
        src = Image.new("RGBA", (200, 100), (255, 0, 0, 255))
        out = _fit_image(src, (100, 100), "contain")
        assert out.size == (100, 100)
        # Top row should be transparent (letterbox)
        assert out.getpixel((50, 0))[3] == 0

    def test_cover_crops(self):
        # 2:1 source into 100x100 → upscaled and center-cropped, no transparency
        src = Image.new("RGBA", (200, 100), (255, 0, 0, 255))
        out = _fit_image(src, (100, 100), "cover")
        assert out.size == (100, 100)
        assert out.getpixel((50, 50))[3] == 255


# ── End-to-end compose() ──────────────────────────────────────────────────

class TestCompose:
    def test_empty_layers_renders_background(self, tmp_path, fonts_dir):
        out, meta = _render(
            {"size": [100, 100], "background": "#ff0000", "layers": []},
            tmp_path, fonts_dir,
        )
        assert meta["size"] == [100, 100]
        assert meta["layers_rendered"] == 0
        img = Image.open(out).convert("RGBA")
        assert img.size == (100, 100)
        assert img.getpixel((50, 50)) == (255, 0, 0, 255)

    def test_default_size_is_2_3_book_cover(self, tmp_path, fonts_dir):
        out, meta = _render({"layers": []}, tmp_path, fonts_dir)
        assert meta["size"] == [1024, 1536]

    def test_transparent_background(self, tmp_path, fonts_dir):
        out, _ = _render(
            {"size": [50, 50], "background": "transparent", "layers": []},
            tmp_path, fonts_dir,
        )
        img = Image.open(out).convert("RGBA")
        assert img.getpixel((10, 10)) == (0, 0, 0, 0)

    def test_rect_layer(self, tmp_path, fonts_dir):
        out, _ = _render({
            "size": [100, 100], "background": "transparent",
            "layers": [{"type": "rect", "rect": [10, 10, 30, 30], "fill": "#00ff00"}],
        }, tmp_path, fonts_dir)
        img = Image.open(out).convert("RGBA")
        assert img.getpixel((20, 20))[:3] == (0, 255, 0)
        # Outside rect remains transparent
        assert img.getpixel((80, 80)) == (0, 0, 0, 0)

    def test_text_layer_renders(self, tmp_path, fonts_dir):
        out, meta = _render({
            "size": [200, 80], "background": "#000000",
            "layers": [{
                "type": "text", "text": "Hi",
                "font": "sans", "size": 32, "color": "#ffffff",
                "position": [10, 10],
            }],
        }, tmp_path, fonts_dir)
        assert meta["layers_rendered"] == 1
        img = Image.open(out).convert("RGBA")
        # Find at least one non-black pixel (the text)
        found_light = any(
            img.getpixel((x, y))[0] > 200
            for x in range(0, 200, 5)
            for y in range(0, 80, 5)
        )
        assert found_light, "expected white text pixels somewhere"

    def test_literal_backslash_n_treated_as_newline(self, tmp_path, fonts_dir):
        """Agents sometimes pass the two-character escape '\\n' instead of a
        real newline (double-JSON-encoding). The compositor must split on it
        anyway — otherwise the title overflows and shows a literal '\\n'."""
        # Build a tall narrow canvas so two short lines fit but a single long
        # line wouldn't. If the compositor splits "Bittere\\nReduktion" into
        # two lines, layout fits; otherwise the line would be ~9 chars wide.
        out, meta = _render({
            "size": [400, 300], "background": "#000000",
            "layers": [{
                "type": "text",
                "text": "Bittere\\nReduktion",   # literal backslash + n
                "font": "serif_bold", "size": 60, "color": "#ffffff",
                "position": {"anchor": "center"},
                "align": "center",
            }],
        }, tmp_path, fonts_dir)
        assert meta["layers_rendered"] == 1
        img = Image.open(out).convert("RGBA")
        # Top half and bottom half should each contain text pixels — proves
        # the string was split into two lines.
        def has_text(y0, y1):
            return any(
                img.getpixel((x, y))[0] > 200
                for x in range(20, 380, 10)
                for y in range(y0, y1, 5)
            )
        assert has_text(40, 140), "no text in top half — split didn't happen"
        assert has_text(160, 260), "no text in bottom half — split didn't happen"

    def test_text_without_max_width_wraps_at_canvas_margin(self, tmp_path, fonts_dir):
        """If the caller omits max_width, a too-wide single line must still be
        wrapped instead of overflowing the canvas (was: stayed on one line and
        ran past the canvas edges)."""
        long_title = "Der unbedingt viel zu lange Titel der niemals passt"
        out, _ = _render({
            "size": [600, 400], "background": "#000000",
            "layers": [{
                "type": "text", "text": long_title,
                "font": "sans_bold", "size": 50, "color": "#ffffff",
                "position": {"anchor": "center"},
                "align": "center",
                # no max_width given on purpose
            }],
        }, tmp_path, fonts_dir)
        img = Image.open(out).convert("RGBA")
        # The first/last 30 px columns (within the 80 px safe margin) must NOT
        # contain text pixels — text must have wrapped to stay inside.
        left_band_has_text = any(
            img.getpixel((x, y))[0] > 200
            for x in range(0, 30)
            for y in range(0, 400, 5)
        )
        right_band_has_text = any(
            img.getpixel((x, y))[0] > 200
            for x in range(570, 600)
            for y in range(0, 400, 5)
        )
        assert not left_band_has_text, "text leaked into left safe margin"
        assert not right_band_has_text, "text leaked into right safe margin"

    def test_overflow_warning_for_rect_outside_canvas(self, tmp_path, fonts_dir):
        """A rect whose bbox extends past the canvas must produce a warning
        naming the layer index, type, and how far it overflows on each side.
        File is still written."""
        out, meta = _render({
            "size": [200, 200], "background": "transparent",
            "layers": [
                {"type": "rect", "rect": [-30, -10, 100, 100], "fill": "#ff0000"},
                {"type": "rect", "rect": [150, 150, 100, 100], "fill": "#00ff00"},
            ],
        }, tmp_path, fonts_dir)
        # File rendered
        assert Image.open(out).size == (200, 200)
        assert meta["layers_rendered"] == 2
        # Warnings for both layers
        warns = meta["warnings"]
        joined = " | ".join(warns)
        assert "layer 0 (rect)" in joined
        assert "layer 1 (rect)" in joined
        assert "left by 30px" in joined
        assert "top by 10px" in joined
        assert "right by 50px" in joined
        assert "bottom by 50px" in joined

    def test_no_overflow_warning_for_fitted_layers(self, tmp_path, fonts_dir):
        """Layers that fit inside the canvas don't generate warnings."""
        _, meta = _render({
            "size": [200, 200], "background": "#000000",
            "layers": [
                {"type": "rect", "rect": [10, 10, 180, 180], "fill": "#ffffff"},
                {"type": "vignette", "strength": 0.3},
            ],
        }, tmp_path, fonts_dir)
        assert meta["warnings"] == []

    def test_overflow_warning_for_oversized_text(self, tmp_path, fonts_dir):
        """A text layer that can't fit on the canvas (because the agent set a
        huge font without sizing the canvas appropriately) must trigger an
        overflow warning identifying the text layer."""
        _, meta = _render({
            "size": [100, 80], "background": "#000000",
            "layers": [{
                "type": "text", "text": "ABCDEFGHIJ", "font": "sans_bold",
                "size": 80, "color": "#ffffff", "position": [0, 0],
            }],
        }, tmp_path, fonts_dir)
        assert any("layer 0 (text) extends beyond canvas" in w for w in meta["warnings"]), \
            f"expected canvas-overflow warning, got: {meta['warnings']}"

    def test_overlap_warning_text_on_svg(self, tmp_path, fonts_dir):
        """Title text dropped on top of an SVG decoration (same vertical band)
        must produce a hard-overlap warning — this is the most common
        composition bug from cover_artist runs."""
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='400' height='40' "
               "viewBox='0 0 400 40'><line x1='0' y1='20' x2='400' y2='20' "
               "stroke='#fff' stroke-width='4'/></svg>")
        _, meta = _render({
            "size": [600, 400], "background": "#000000",
            "layers": [
                {"type": "svg", "svg": svg, "size": [400, 40],
                 "position": [100, 180]},
                # Title sits right on top of the svg's vertical band
                {"type": "text", "text": "TITLE", "font": "sans_bold",
                 "size": 60, "color": "#fff", "position": [120, 170]},
            ],
        }, tmp_path, fonts_dir)
        assert any("overlap by" in w and "text" in w and "svg" in w
                   for w in meta["warnings"]), \
            f"expected hard-overlap warning, got: {meta['warnings']}"

    def test_overlap_warning_text_stacked_too_close(self, tmp_path, fonts_dir):
        """Two text layers stacked with < 30 px vertical gap should warn."""
        _, meta = _render({
            "size": [600, 400], "background": "#000000",
            "layers": [
                {"type": "text", "text": "TOP", "font": "sans_bold",
                 "size": 40, "color": "#fff", "position": [100, 100]},
                # Second text 15 px below the first — well under the 30 px rule
                {"type": "text", "text": "BOTTOM", "font": "sans_bold",
                 "size": 40, "color": "#fff", "position": [100, 165]},
            ],
        }, tmp_path, fonts_dir)
        assert any("stacked too close vertically" in w for w in meta["warnings"]), \
            f"expected stacked-too-close warning, got: {meta['warnings']}"

    def test_no_overlap_warning_for_well_separated_layers(self, tmp_path, fonts_dir):
        """Title and SVG in distinct vertical zones with > 30 px gap → no overlap warning."""
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='200' height='30' "
               "viewBox='0 0 200 30'><line x1='0' y1='15' x2='200' y2='15' "
               "stroke='#fff' stroke-width='2'/></svg>")
        _, meta = _render({
            "size": [600, 800], "background": "#000000",
            "layers": [
                {"type": "text", "text": "TITLE", "font": "sans_bold",
                 "size": 60, "color": "#fff", "position": [150, 100]},
                # SVG well below the title (gap > 30 px)
                {"type": "svg", "svg": svg, "size": [200, 30],
                 "position": [200, 400]},
            ],
        }, tmp_path, fonts_dir)
        overlap_warnings = [w for w in meta["warnings"]
                            if "overlap" in w or "too close" in w]
        assert overlap_warnings == [], \
            f"unexpected overlap warnings: {overlap_warnings}"

    def test_no_overlap_warning_for_background_text(self, tmp_path, fonts_dir):
        """Image / gradient / vignette layers are background-style and are
        intentionally rendered under text+svg. Overlap with them must NOT warn."""
        _, meta = _render({
            "size": [600, 400], "background": "#202020",
            "layers": [
                {"type": "gradient", "rect": [0, 0, 600, 400],
                 "colors": ["#00000000", "#000000ff"], "direction": "bottom"},
                # Text sits over the gradient — that's the whole point
                {"type": "text", "text": "ON GRADIENT", "font": "sans_bold",
                 "size": 50, "color": "#fff", "position": [100, 150]},
            ],
        }, tmp_path, fonts_dir)
        overlap_warnings = [w for w in meta["warnings"]
                            if "overlap" in w or "too close" in w]
        assert overlap_warnings == [], \
            f"background+text should not warn, got: {overlap_warnings}"

    def test_overlap_check_can_be_disabled(self, tmp_path, fonts_dir):
        """Plugin config can switch the overlap check off entirely — overlapping
        text+svg then produces no overlap warning (still renders fine)."""
        out_path = tmp_path / "out.png"
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='400' height='40' "
               "viewBox='0 0 400 40'><line x1='0' y1='20' x2='400' y2='20' "
               "stroke='#fff' stroke-width='4'/></svg>")
        meta = compose(
            {
                "size": [600, 400], "background": "#000000",
                "layers": [
                    {"type": "svg", "svg": svg, "size": [400, 40],
                     "position": [100, 180]},
                    {"type": "text", "text": "TITLE", "font": "sans_bold",
                     "size": 60, "color": "#fff", "position": [120, 170]},
                ],
            },
            out_path, fonts_dir, {}, Path.cwd(),
            overlap_check_enabled=False,
        )
        overlap_warnings = [w for w in meta["warnings"]
                            if "overlap" in w or "too close" in w]
        assert overlap_warnings == [], \
            f"check disabled but still got: {overlap_warnings}"

    def test_overlap_min_gap_is_configurable(self, tmp_path, fonts_dir):
        """A stricter min_gap_px catches what the default 30 would let pass."""
        out_path = tmp_path / "out.png"
        spec = {
            "size": [600, 400], "background": "#000000",
            "layers": [
                # Two text layers with a 45-pixel vertical gap → passes
                # default rule (gap >= 30) but fails a strict rule (gap < 60).
                {"type": "text", "text": "A", "font": "sans_bold",
                 "size": 40, "color": "#fff", "position": [100, 100]},
                {"type": "text", "text": "B", "font": "sans_bold",
                 "size": 40, "color": "#fff", "position": [100, 195]},
            ],
        }
        # Default (30 px) — no warning expected
        meta_loose = compose(spec, out_path, fonts_dir, {}, Path.cwd())
        loose_warnings = [w for w in meta_loose["warnings"]
                          if "too close" in w or "overlap" in w]
        assert loose_warnings == [], \
            f"default gap=30 should not warn here, got: {loose_warnings}"

        # Stricter (60 px) — warning expected
        meta_strict = compose(spec, out_path, fonts_dir, {}, Path.cwd(),
                              overlap_min_gap_px=60)
        strict_warnings = [w for w in meta_strict["warnings"]
                           if "too close" in w]
        assert any("required: 60px" in w for w in strict_warnings), \
            f"strict gap=60 should warn here, got: {meta_strict['warnings']}"

    def test_gradient_layer(self, tmp_path, fonts_dir):
        out, _ = _render({
            "size": [50, 50], "background": "transparent",
            "layers": [{
                "type": "gradient", "rect": [0, 0, 50, 50],
                "colors": ["#000000ff", "#ffffffff"], "direction": "bottom",
            }],
        }, tmp_path, fonts_dir)
        img = Image.open(out).convert("RGBA")
        top = img.getpixel((25, 2))
        bottom = img.getpixel((25, 47))
        assert top[0] < 30 and bottom[0] > 220, f"top={top} bottom={bottom}"

    def test_gradient_from_to_shortcut(self, tmp_path, fonts_dir):
        out, _ = _render({
            "size": [10, 10],
            "layers": [{
                "type": "gradient", "rect": [0, 0, 10, 10],
                "from": "#000000", "to": "#ffffff", "direction": "bottom",
            }],
        }, tmp_path, fonts_dir)
        # Just verify it renders (no exception)
        assert Image.open(out).size == (10, 10)

    def test_vignette_layer_darkens_corners(self, tmp_path, fonts_dir):
        out, _ = _render({
            "size": [100, 100], "background": "#ffffff",
            "layers": [{"type": "vignette", "strength": 1.0, "color": "#000000"}],
        }, tmp_path, fonts_dir)
        img = Image.open(out).convert("RGBA")
        center = img.getpixel((50, 50))
        corner = img.getpixel((2, 2))
        assert corner[0] < center[0], f"corner {corner} should be darker than center {center}"

    def test_image_layer_from_disk(self, tmp_path, fonts_dir):
        # Create a small red image
        src = tmp_path / "src.png"
        Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(src)
        out, _ = _render({
            "size": [50, 50], "background": "transparent",
            "layers": [{
                "type": "image", "src": "src.png",
                "position": [5, 5],
            }],
        }, tmp_path, fonts_dir)
        img = Image.open(out).convert("RGBA")
        # Red should be at (15, 15)
        assert img.getpixel((15, 15)) == (255, 0, 0, 255)
        # Outside should be transparent
        assert img.getpixel((40, 40)) == (0, 0, 0, 0)

    def test_image_layer_data_uri(self, tmp_path, fonts_dir):
        import base64
        from io import BytesIO
        buf = BytesIO()
        Image.new("RGBA", (10, 10), (0, 0, 255, 255)).save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        out, _ = _render({
            "size": [30, 30], "background": "transparent",
            "layers": [{
                "type": "image",
                "src": f"data:image/png;base64,{b64}",
                "position": [10, 10],
            }],
        }, tmp_path, fonts_dir)
        img = Image.open(out).convert("RGBA")
        assert img.getpixel((15, 15)) == (0, 0, 255, 255)

    def test_opacity_applied(self, tmp_path, fonts_dir):
        out, _ = _render({
            "size": [20, 20], "background": "transparent",
            "layers": [{
                "type": "rect", "rect": [0, 0, 20, 20],
                "fill": "#ff0000", "opacity": 0.5,
            }],
        }, tmp_path, fonts_dir)
        img = Image.open(out).convert("RGBA")
        px = img.getpixel((10, 10))
        # Should be red with ~50% alpha
        assert px[0] == 255 and 100 < px[3] < 160

    def test_multiple_layers_compose_bottom_up(self, tmp_path, fonts_dir):
        out, _ = _render({
            "size": [30, 30], "background": "#ff0000",
            "layers": [
                {"type": "rect", "rect": [0, 0, 30, 30], "fill": "#00ff00"},
                {"type": "rect", "rect": [10, 10, 10, 10], "fill": "#0000ff"},
            ],
        }, tmp_path, fonts_dir)
        img = Image.open(out).convert("RGBA")
        # Outer: green (covers red)
        assert img.getpixel((2, 2))[:3] == (0, 255, 0)
        # Inner: blue (covers green)
        assert img.getpixel((15, 15))[:3] == (0, 0, 255)

    def test_jpeg_flattens_alpha(self, tmp_path, fonts_dir):
        out, meta = _render({
            "size": [20, 20], "background": "transparent",
            "format": "jpeg",
            "layers": [{"type": "rect", "rect": [5, 5, 10, 10], "fill": "#ff0000"}],
        }, tmp_path, fonts_dir, ext="jpg")
        assert meta["format"] == "jpeg"
        img = Image.open(out)
        assert img.mode == "RGB"  # no alpha

    def test_webp_output(self, tmp_path, fonts_dir):
        out = tmp_path / "out.webp"
        meta = compose({"size": [20, 20], "background": "#00ff00", "layers": []},
                       out, fonts_dir, {}, tmp_path)
        assert meta["format"] == "webp"
        assert out.exists()

    def test_format_inferred_from_extension(self, tmp_path, fonts_dir):
        out = tmp_path / "out.webp"
        meta = compose({"size": [10, 10], "layers": []}, out, fonts_dir, {}, tmp_path)
        assert meta["format"] == "webp"

    def test_returns_layer_count(self, tmp_path, fonts_dir):
        _, meta = _render({
            "size": [50, 50],
            "layers": [
                {"type": "rect", "rect": [0, 0, 50, 50], "fill": "#ff0000"},
                {"type": "vignette", "strength": 0.3},
            ],
        }, tmp_path, fonts_dir)
        assert meta["layers_rendered"] == 2

    def test_rotation_doesnt_crash(self, tmp_path, fonts_dir):
        out, _ = _render({
            "size": [100, 100],
            "layers": [{
                "type": "rect", "rect": [25, 25, 50, 50],
                "fill": "#ff0000", "rotation": 45,
            }],
        }, tmp_path, fonts_dir)
        assert Image.open(out).size == (100, 100)

    @pytest.mark.parametrize("mode", ["multiply", "screen", "overlay"])
    def test_blend_modes_dont_crash(self, mode, tmp_path, fonts_dir):
        out, _ = _render({
            "size": [40, 40], "background": "#808080",
            "layers": [{
                "type": "rect", "rect": [10, 10, 20, 20],
                "fill": "#ff0000", "blend_mode": mode,
            }],
        }, tmp_path, fonts_dir)
        assert Image.open(out).size == (40, 40)


# ── LLM-sloppy spec tolerance ─────────────────────────────────────────────

class TestSpecTolerance:
    """The compositor must survive the malformed specs LLM agents produce
    instead of hard-failing and forcing a re-compose loop."""

    def test_type_with_trailing_junk(self, tmp_path, fonts_dir):
        # agent jammed an attribute into the type string
        out, meta = _render({
            "size": [200, 80], "background": "#000000",
            "layers": [{"type": "text,uppercase:true", "text": "Hi",
                        "position": [10, 10], "color": "#ffffff"}],
        }, tmp_path, fonts_dir)
        assert meta["layers_rendered"] == 1
        assert any("trailing junk" in w for w in meta["warnings"])

    def test_position_bare_anchor_string(self, tmp_path, fonts_dir):
        out, _ = _render({
            "size": [200, 200], "background": "transparent",
            "layers": [{"type": "rect", "position": "bottom_center",
                        "size": [50, 50], "fill": "#ff0000"}],
        }, tmp_path, fonts_dir)
        img = Image.open(out).convert("RGBA")
        # rect centered horizontally at the bottom
        assert img.getpixel((100, 190))[:3] == (255, 0, 0)

    def test_stroke_as_string_is_ignored(self, tmp_path, fonts_dir):
        out, meta = _render({
            "size": [200, 80], "background": "#000000",
            "layers": [{"type": "text", "text": "Hi", "color": "#ffffff",
                        "stroke": "2px black", "position": [10, 10]}],
        }, tmp_path, fonts_dir)
        assert meta["layers_rendered"] == 1
        assert any("stroke must be an object" in w for w in meta["warnings"])

    def test_shadow_as_number_is_ignored(self, tmp_path, fonts_dir):
        out, meta = _render({
            "size": [200, 80], "background": "#000000",
            "layers": [{"type": "text", "text": "Hi", "color": "#ffffff",
                        "shadow": 5, "position": [10, 10]}],
        }, tmp_path, fonts_dir)
        assert meta["layers_rendered"] == 1
        assert any("shadow must be an object" in w for w in meta["warnings"])

    def test_numeric_field_as_string(self, tmp_path, fonts_dir):
        # "size": "48px" instead of 48
        out, meta = _render({
            "size": [300, 120], "background": "#000000",
            "layers": [{"type": "text", "text": "Hi", "size": "48px",
                        "color": "#ffffff", "position": [10, 10]}],
        }, tmp_path, fonts_dir)
        assert meta["layers_rendered"] == 1

    def test_position_bare_int_still_errors(self, tmp_path, fonts_dir):
        # an unrecoverable case must give a clear, prescriptive error
        with pytest.raises(CompositionError, match="invalid position"):
            _render({"size": [200, 200],
                     "layers": [{"type": "rect", "position": 868,
                                 "size": [20, 20], "fill": "#fff"}]},
                    tmp_path, fonts_dir)


# ── Spec validation errors ────────────────────────────────────────────────

class TestSpecErrors:
    def test_non_object_spec(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="object"):
            compose("nope", tmp_path / "x.png", fonts_dir, {}, tmp_path)

    def test_unknown_layer_type(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="unknown layer type"):
            _render({"size": [10, 10], "layers": [{"type": "frobnicate"}]},
                    tmp_path, fonts_dir)

    def test_missing_image_src(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="src"):
            _render({"size": [10, 10], "layers": [{"type": "image"}]},
                    tmp_path, fonts_dir)

    def test_missing_image_file(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="not found"):
            _render({"size": [10, 10],
                     "layers": [{"type": "image", "src": "nope.png"}]},
                    tmp_path, fonts_dir)

    def test_missing_text_field(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="text"):
            _render({"size": [10, 10], "layers": [{"type": "text"}]},
                    tmp_path, fonts_dir)

    def test_invalid_text_align(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="align"):
            _render({
                "size": [50, 20],
                "layers": [{"type": "text", "text": "x", "align": "diagonal"}],
            }, tmp_path, fonts_dir)

    def test_gradient_needs_two_colors(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError):
            _render({
                "size": [20, 20],
                "layers": [{"type": "gradient", "rect": [0, 0, 20, 20], "colors": ["#000"]}],
            }, tmp_path, fonts_dir)

    def test_invalid_opacity(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="opacity"):
            _render({
                "size": [20, 20],
                "layers": [{"type": "rect", "rect": [0, 0, 20, 20],
                            "fill": "#fff", "opacity": 2.0}],
            }, tmp_path, fonts_dir)

    def test_invalid_blend_mode(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="blend_mode"):
            _render({
                "size": [20, 20],
                "layers": [{"type": "rect", "rect": [0, 0, 20, 20],
                            "fill": "#fff", "blend_mode": "dodge"}],
            }, tmp_path, fonts_dir)

    def test_unsupported_format(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="format"):
            compose({"size": [10, 10], "format": "bmp", "layers": []},
                    tmp_path / "x.bmp", fonts_dir, {}, tmp_path)

    def test_layer_error_includes_index(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="layer 1"):
            _render({
                "size": [20, 20],
                "layers": [
                    {"type": "rect", "rect": [0, 0, 10, 10], "fill": "#fff"},
                    {"type": "image"},  # missing src — error
                ],
            }, tmp_path, fonts_dir)


# ── SVG layer (only if svglib is installed) ───────────────────────────────

@pytest.mark.skipif(not HAS_SVGLIB, reason="svglib/reportlab not installed")
class TestSvgLayer:
    def test_renders_simple_svg(self, tmp_path, fonts_dir):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" '
            'viewBox="0 0 40 40"><circle cx="20" cy="20" r="15" fill="#ff0000"/></svg>'
        )
        out, meta = _render({
            "size": [60, 60], "background": "transparent",
            "layers": [{"type": "svg", "svg": svg, "position": [10, 10]}],
        }, tmp_path, fonts_dir)
        assert meta["layers_rendered"] == 1
        img = Image.open(out).convert("RGBA")
        # Red somewhere in the circle area (around (30, 30))
        assert img.getpixel((30, 30))[0] > 200

    def test_missing_svg_field(self, tmp_path, fonts_dir):
        with pytest.raises(CompositionError, match="svg"):
            _render({"size": [40, 40], "layers": [{"type": "svg"}]},
                    tmp_path, fonts_dir)

    def test_white_svg_content_is_visible(self, tmp_path, fonts_dir):
        """Regression: a WHITE svg stroke/fill must stay visible. The earlier
        'make white transparent' alpha hack erased white svg content entirely
        (renderPM bakes a white background, and white content collided with
        it). Two-pass alpha recovery must keep the white line opaque."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="20" '
            'viewBox="0 0 200 20"><line x1="5" y1="10" x2="195" y2="10" '
            'stroke="#ffffff" stroke-width="4"/></svg>'
        )
        out, meta = _render({
            "size": [220, 60], "background": "#102030",
            "layers": [{"type": "svg", "svg": svg, "size": [200, 20],
                        "position": {"anchor": "center"}}],
        }, tmp_path, fonts_dir)
        assert meta["layers_rendered"] == 1
        img = Image.open(out).convert("RGBA")
        # the white line runs across the vertical centre (y=30)
        white_hits = sum(
            1 for x in range(20, 200, 5)
            if img.getpixel((x, 30))[:3] == (255, 255, 255)
        )
        assert white_hits > 10, f"white svg line not visible (hits={white_hits})"

    def test_svg_keeps_non_white_color(self, tmp_path, fonts_dir):
        """A coloured svg element keeps its colour through alpha recovery."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
            'viewBox="0 0 100 100"><rect x="10" y="10" width="80" height="80" '
            'fill="#d4af37"/></svg>'
        )
        out, _ = _render({
            "size": [120, 120], "background": "#000000",
            "layers": [{"type": "svg", "svg": svg, "size": [100, 100],
                        "position": [10, 10]}],
        }, tmp_path, fonts_dir)
        img = Image.open(out).convert("RGBA")
        r, g, b, a = img.getpixel((60, 60))
        assert a > 240 and abs(r - 212) < 12 and abs(g - 175) < 12 and abs(b - 55) < 12


# ── Image analysis (analyze_image) ────────────────────────────────────────

class TestAnalyzeImage:
    def test_solid_dark_recommends_light_text(self, tmp_path):
        p = tmp_path / "dark.png"
        Image.new("RGB", (100, 100), (20, 20, 20)).save(p)
        r = analyze_image(p)
        assert r["brightness"] < 0.15
        assert r["recommendation"] == "light_text"
        assert r["region"] == [0, 0, 100, 100]

    def test_solid_light_recommends_dark_text(self, tmp_path):
        p = tmp_path / "light.png"
        Image.new("RGB", (100, 100), (240, 240, 240)).save(p)
        r = analyze_image(p)
        assert r["brightness"] > 0.85
        assert r["recommendation"] == "dark_text"

    def test_midtone_recommends_backdrop(self, tmp_path):
        p = tmp_path / "mid.png"
        Image.new("RGB", (100, 100), (128, 128, 128)).save(p)
        r = analyze_image(p)
        assert 0.40 <= r["brightness"] <= 0.60
        assert r["recommendation"] == "midtone_uncertain_use_backdrop"

    def test_region_clipping_to_bounds(self, tmp_path):
        p = tmp_path / "im.png"
        Image.new("RGB", (100, 100), (50, 50, 50)).save(p)
        # request region partially outside the image
        r = analyze_image(p, (90, 90, 200, 200))
        assert r["region"] == [90, 90, 10, 10]
        assert r["size"] == [10, 10]

    def test_region_isolates_local_area(self, tmp_path):
        """Left half dark, right half light — region must give the right answer."""
        p = tmp_path / "split.png"
        im = Image.new("RGB", (100, 100), (20, 20, 20))
        im.paste((250, 250, 250), (50, 0, 100, 100))
        im.save(p)
        left = analyze_image(p, (0, 0, 50, 100))
        right = analyze_image(p, (50, 0, 50, 100))
        assert left["recommendation"] == "light_text"
        assert right["recommendation"] == "dark_text"

    def test_palette_and_dominant_color(self, tmp_path):
        p = tmp_path / "im.png"
        # 80% red, 20% blue
        im = Image.new("RGB", (100, 100), (200, 32, 32))
        im.paste((32, 32, 200), (80, 0, 100, 100))
        im.save(p)
        r = analyze_image(p)
        # dominant should be ~red (quantised to 32-bin)
        assert r["dominant_color"].startswith("#c0")  # 192 = 200 // 32 * 32
        assert len(r["palette"]) <= 5
        # palette fractions sum close to 1 (rounded)
        assert sum(item[1] for item in r["palette"]) > 0.95

    def test_edge_density_high_on_busy_image(self, tmp_path):
        p_solid = tmp_path / "solid.png"
        Image.new("RGB", (60, 60), (128, 128, 128)).save(p_solid)
        edge_solid = analyze_image(p_solid)["edge_density"]
        # alternating black/white pixels = max edges
        p_noisy = tmp_path / "noisy.png"
        im = Image.new("RGB", (60, 60))
        for y in range(60):
            for x in range(60):
                v = 255 if (x + y) % 2 else 0
                im.putpixel((x, y), (v, v, v))
        im.save(p_noisy)
        edge_noisy = analyze_image(p_noisy)["edge_density"]
        assert edge_noisy > 0.3
        assert edge_solid < 0.01

    def test_homogeneity_solid_vs_noisy(self, tmp_path):
        p_solid = tmp_path / "solid.png"
        Image.new("RGB", (100, 100), (60, 60, 60)).save(p_solid)
        p_noisy = tmp_path / "noisy.png"
        im = Image.new("RGB", (100, 100))
        for y in range(100):
            for x in range(100):
                v = 255 if (x + y) % 2 else 0
                im.putpixel((x, y), (v, v, v))
        im.save(p_noisy)
        assert analyze_image(p_solid)["homogeneity"] > 0.95
        assert analyze_image(p_noisy)["homogeneity"] < 0.10

    def test_brightness_range_on_solid(self, tmp_path):
        """Uniform area → brightness_range ~ 0."""
        p = tmp_path / "solid.png"
        Image.new("RGB", (100, 100), (128, 128, 128)).save(p)
        r = analyze_image(p)
        assert r["brightness_range"] < 0.01
        assert abs(r["brightness_min"] - r["brightness_max"]) < 0.01

    def test_brightness_range_detects_mixed_dark_and_light(self, tmp_path):
        """Half dark + half light → range is large + recommendation 'mixed'."""
        p = tmp_path / "mixed.png"
        im = Image.new("RGB", (200, 100), (20, 20, 20))  # left dark
        im.paste((240, 240, 240), (100, 0, 200, 100))    # right bright
        im.save(p)
        r = analyze_image(p)
        # Aggregate brightness is ~midtone, but the sub-cells reveal the split
        assert r["brightness_range"] > 0.50
        assert r["brightness_min"] < 0.20
        assert r["brightness_max"] > 0.80
        assert r["recommendation"] == "mixed_use_stroke_or_pill"


class TestFindTextRegion:
    @pytest.fixture
    def mixed_image(self, tmp_path):
        """400×300 image: noisy everywhere EXCEPT a calm dark rect at [100,100,200,100]."""
        p = tmp_path / "mixed.png"
        im = Image.new("RGB", (400, 300))
        for y in range(300):
            for x in range(400):
                if 100 <= x <= 300 and 100 <= y <= 200:
                    v = 60
                else:
                    v = 255 if (x * 13 + y * 7) % 2 else 0
                im.putpixel((x, y), (v, v, v))
        im.save(p)
        return p

    def test_finds_calm_region(self, mixed_image):
        r = find_text_region(mixed_image, region_size=(150, 80))
        best = r["best"]
        bx, by = best["region"][0], best["region"][1]
        # Best window should sit inside or overlap the calm rect
        assert 50 <= bx <= 200
        assert 80 <= by <= 150
        assert best["homogeneity"] > 0.5

    def test_returns_top_n_candidates(self, mixed_image):
        r = find_text_region(mixed_image, region_size=(100, 50), max_candidates=5)
        assert len(r["candidates"]) == 5
        # Sorted descending by homogeneity
        hom = [c["homogeneity"] for c in r["candidates"]]
        assert hom == sorted(hom, reverse=True)

    def test_prefer_top_third(self, mixed_image):
        r = find_text_region(mixed_image, (100, 50), prefer="top_third")
        # 300 // 3 = 100 → y_max = 100 - 50 = 50; so y must be ≤ 50
        assert r["best"]["region"][1] <= 50

    def test_prefer_bottom_third(self, mixed_image):
        r = find_text_region(mixed_image, (100, 50), prefer="bottom_third")
        # bottom third starts at y = 200
        assert r["best"]["region"][1] >= 200

    def test_region_too_big_errors(self, mixed_image):
        with pytest.raises(CompositionError, match="must fit"):
            find_text_region(mixed_image, region_size=(500, 500))

    def test_invalid_prefer_errors(self, mixed_image):
        with pytest.raises(CompositionError, match="prefer"):
            find_text_region(mixed_image, (50, 50), prefer="middle_third")

    def test_result_includes_recommendation(self, mixed_image):
        r = find_text_region(mixed_image, (100, 50))
        for c in r["candidates"]:
            assert c["recommendation"] in (
                "light_text", "dark_text",
                "midtone_uncertain_use_backdrop", "mixed_use_stroke_or_pill",
            )
            # Every candidate carries the new brightness_range fields
            assert "brightness_min" in c
            assert "brightness_max" in c
            assert "brightness_range" in c
            assert c["brightness_range"] >= 0.0


# ── Font fallback ─────────────────────────────────────────────────────────

class TestFontFallback:
    def test_unknown_font_falls_back_with_warning(self, tmp_path, fonts_dir):
        # 'sans' alias maps to system fonts (Arial / DejaVuSans)
        _, meta = _render({
            "size": [80, 30],
            "layers": [{
                "type": "text", "text": "x", "font": "sans", "size": 14,
                "color": "#ffffff",
            }],
        }, tmp_path, fonts_dir)
        # Should render without raising; may or may not emit a warning depending on host
        assert meta["layers_rendered"] == 1

    def test_truly_missing_font_warns(self, tmp_path, fonts_dir):
        _, meta = _render({
            "size": [80, 30],
            "layers": [{
                "type": "text", "text": "x", "font": "completely-made-up-font-xyz",
                "size": 14, "color": "#ffffff",
            }],
        }, tmp_path, fonts_dir)
        assert any("completely-made-up-font-xyz" in w for w in meta["warnings"])
