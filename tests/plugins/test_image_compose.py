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
    compose,
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
        with pytest.raises(CompositionError):
            _resolve_position("center", (40, 40), (200, 300))

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
