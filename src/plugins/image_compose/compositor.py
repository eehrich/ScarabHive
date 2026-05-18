"""Layered image composition built on Pillow.

Layer types: image, text, rect, gradient, svg, vignette.
SVG support is optional (lazy import of svglib + reportlab).
"""
from __future__ import annotations

import base64
import io
import logging
import math
import re
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

DEFAULT_SIZE = (1024, 1536)  # 2:3 portrait book cover
LAYER_TYPES = {"image", "text", "rect", "gradient", "svg", "vignette"}
BLEND_MODES = {"normal", "multiply", "screen", "overlay"}

ANCHORS = {
    "top_left": (0.0, 0.0), "top_center": (0.5, 0.0), "top_right": (1.0, 0.0),
    "center_left": (0.0, 0.5), "center": (0.5, 0.5), "center_right": (1.0, 0.5),
    "bottom_left": (0.0, 1.0), "bottom_center": (0.5, 1.0), "bottom_right": (1.0, 1.0),
}

# Common platform font fallbacks tried when no alias matches.
SYSTEM_FONT_FALLBACKS = {
    "serif":      ["georgia.ttf", "Georgia.ttf", "DejaVuSerif.ttf", "Times New Roman.ttf", "times.ttf"],
    "serif_bold": ["georgiab.ttf", "Georgia Bold.ttf", "DejaVuSerif-Bold.ttf", "timesbd.ttf"],
    "sans":       ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "Helvetica.ttf"],
    "sans_bold":  ["arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "Helvetica-Bold.ttf"],
    "mono":       ["consola.ttf", "Consolas.ttf", "DejaVuSansMono.ttf", "Courier New.ttf", "cour.ttf"],
    "display":    ["impact.ttf", "Impact.ttf", "DejaVuSans-Bold.ttf"],
}


class CompositionError(Exception):
    """Raised on any user-facing composition error (bad spec, missing asset, ...)."""


# ── Top-level entry point ─────────────────────────────────────────────────

def compose(spec: dict, output_path: Path, fonts_dir: Path,
            font_aliases: dict[str, str], project_root: Path) -> dict:
    """Render `spec` to `output_path`. Returns a small metadata dict."""
    if not isinstance(spec, dict):
        raise CompositionError("spec must be an object")

    size = _parse_size(spec.get("size") or DEFAULT_SIZE)
    bg = spec.get("background", "transparent")
    canvas = _new_canvas(size, bg)

    warnings: list[str] = []
    layers = spec.get("layers") or []
    if not isinstance(layers, list):
        raise CompositionError("layers must be an array")

    rendered = 0
    for idx, layer in enumerate(layers):
        ltype = (layer or {}).get("type", "?") if isinstance(layer, dict) else "?"
        try:
            canvas = _render_layer(
                canvas, layer, size, fonts_dir, font_aliases, project_root,
                warnings, layer_index=idx,
            )
            rendered += 1
        except CompositionError as e:
            raise CompositionError(f"layer {idx} ({ltype}): {e}") from e
        except Exception as e:
            raise CompositionError(f"layer {idx} ({ltype}): {e}") from e

    fmt = (spec.get("format") or _guess_format(output_path) or "png").lower()
    if fmt not in {"png", "webp", "jpeg", "jpg"}:
        raise CompositionError(f"unsupported format: {fmt}")
    if fmt == "jpg":
        fmt = "jpeg"

    if fmt == "jpeg" and canvas.mode == "RGBA":
        flat = Image.new("RGB", canvas.size, (255, 255, 255))
        flat.paste(canvas, mask=canvas.split()[3])
        canvas = flat

    save_kwargs: dict[str, Any] = {}
    quality = int(spec.get("quality", 95))
    if fmt == "webp":
        save_kwargs.update(quality=quality, method=6)
    elif fmt == "jpeg":
        save_kwargs.update(quality=quality, optimize=True)
    elif fmt == "png":
        save_kwargs["optimize"] = True

    canvas.save(output_path, format=fmt.upper(), **save_kwargs)
    return {
        "size": list(size),
        "format": fmt,
        "bytes": output_path.stat().st_size,
        "layers_rendered": rendered,
        "warnings": warnings,
    }


# ── Canvas / size / color helpers ─────────────────────────────────────────

def _parse_size(size: Any) -> tuple[int, int]:
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise CompositionError(f"size must be [width, height], got {size!r}")
    w, h = size
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0):
        raise CompositionError(f"size must be two positive ints, got {size!r}")
    if w > 8192 or h > 8192:
        raise CompositionError(f"size too large (max 8192): {size!r}")
    return (w, h)


def _new_canvas(size: tuple[int, int], background: Any) -> Image.Image:
    if background in (None, "transparent"):
        return Image.new("RGBA", size, (0, 0, 0, 0))
    return Image.new("RGBA", size, _parse_color(background))


_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _parse_color(c: Any) -> tuple[int, int, int, int]:
    """Accept '#rgb', '#rgba', '#rrggbb', '#rrggbbaa', or [r,g,b(,a)]."""
    if isinstance(c, (list, tuple)):
        if len(c) == 3:
            r, g, b = c
            return (int(r), int(g), int(b), 255)
        if len(c) == 4:
            r, g, b, a = c
            return (int(r), int(g), int(b), int(a))
        raise CompositionError(f"color array must have 3 or 4 components: {c!r}")
    if not isinstance(c, str):
        raise CompositionError(f"color must be hex string or rgb(a) array: {c!r}")
    m = _HEX_RE.match(c.strip())
    if not m:
        raise CompositionError(f"invalid hex color: {c!r}")
    h = m.group(1)
    if len(h) in (3, 4):
        # Expand shorthand: #rgb → #rrggbb
        h = "".join(ch * 2 for ch in h)
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    a = int(h[6:8], 16) if len(h) == 8 else 255
    return (r, g, b, a)


def _guess_format(path: Path) -> str | None:
    suf = path.suffix.lower().lstrip(".")
    return suf if suf in {"png", "webp", "jpeg", "jpg"} else None


# ── Position / size resolvers ─────────────────────────────────────────────

def _resolve_position(pos: Any, layer_size: tuple[int, int],
                      canvas_size: tuple[int, int]) -> tuple[int, int]:
    """Return top-left (x, y) pixel coordinate for placing layer_size into canvas."""
    cw, ch = canvas_size
    lw, lh = layer_size
    if pos is None:
        return (0, 0)
    if isinstance(pos, (list, tuple)) and len(pos) == 2:
        return (int(pos[0]), int(pos[1]))
    if isinstance(pos, dict):
        anchor = pos.get("anchor", "top_left")
        if anchor not in ANCHORS:
            raise CompositionError(f"unknown anchor: {anchor}")
        ax, ay = ANCHORS[anchor]
        if "offset_pct" in pos:
            dx_pct, dy_pct = pos["offset_pct"]
            dx, dy = int(cw * float(dx_pct) / 100.0), int(ch * float(dy_pct) / 100.0)
        else:
            ox, oy = pos.get("offset", (0, 0))
            dx, dy = int(ox), int(oy)
        # Anchor on canvas
        cx, cy = int(cw * ax), int(ch * ay)
        # Place layer so its corresponding anchor lands on (cx+dx, cy+dy)
        x = cx + dx - int(lw * ax)
        y = cy + dy - int(lh * ay)
        return (x, y)
    raise CompositionError(f"invalid position: {pos!r}")


def _resolve_size_field(size: Any, canvas_size: tuple[int, int],
                        default: tuple[int, int] | None = None) -> tuple[int, int] | None:
    """Resolve a 'size' field that may be [w,h], {width_pct/height_pct}, or None."""
    if size is None:
        return default
    cw, ch = canvas_size
    if isinstance(size, (list, tuple)) and len(size) == 2:
        return (int(size[0]), int(size[1]))
    if isinstance(size, dict):
        w = size.get("width")
        h = size.get("height")
        if "width_pct" in size:
            w = int(cw * float(size["width_pct"]) / 100.0)
        if "height_pct" in size:
            h = int(ch * float(size["height_pct"]) / 100.0)
        if w is None and h is None:
            return default
        # If only one dimension given and default provided, preserve aspect
        if default and (w is None or h is None):
            dw, dh = default
            aspect = dw / dh if dh else 1.0
            if w is None and h is not None:
                w = int(h * aspect)
            elif h is None and w is not None:
                h = int(w / aspect) if aspect else w
        return (int(w or 0), int(h or 0))
    raise CompositionError(f"invalid size: {size!r}")


def _resolve_rect(rect: Any, position: Any, size: Any,
                  canvas_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Return (x, y, w, h). 'rect' wins; else combine position+size; else full canvas."""
    if rect is not None:
        if not (isinstance(rect, (list, tuple)) and len(rect) == 4):
            raise CompositionError(f"rect must be [x, y, w, h], got {rect!r}")
        return tuple(int(v) for v in rect)  # type: ignore[return-value]
    sz = _resolve_size_field(size, canvas_size, default=canvas_size)
    pos = _resolve_position(position, sz or canvas_size, canvas_size)
    w, h = sz or canvas_size
    return (pos[0], pos[1], int(w), int(h))


# ── Layer dispatcher ──────────────────────────────────────────────────────

# Layer types that legitimately fill or exceed the canvas — we don't warn for
# these even if they extend beyond the canvas (it's their job).
_FULL_CANVAS_TYPES = frozenset({"vignette"})


def _check_canvas_overflow(layer_index: int, layer_type: str,
                            pos: tuple[int, int], layer_size: tuple[int, int],
                            canvas_size: tuple[int, int],
                            warnings: list[str]) -> None:
    """Append a warning if the layer's bounding box leaves the canvas.

    Skipped for full-canvas layers (vignette) and for `image` layers using
    `fit=cover/contain/stretch` to canvas size (those are intended full-bleed).
    """
    if layer_type in _FULL_CANVAS_TYPES:
        return
    x, y = pos
    w, h = layer_size
    cw, ch = canvas_size
    over: list[str] = []
    if x < 0:
        over.append(f"left by {-x}px")
    if y < 0:
        over.append(f"top by {-y}px")
    if x + w > cw:
        over.append(f"right by {x + w - cw}px")
    if y + h > ch:
        over.append(f"bottom by {y + h - ch}px")
    if over:
        warnings.append(
            f"layer {layer_index} ({layer_type}) extends beyond canvas: "
            + ", ".join(over)
            + f". Layer size {w}x{h} at top-left ({x},{y}); canvas {cw}x{ch}."
        )


def _render_layer(base: Image.Image, layer: Any, canvas_size: tuple[int, int],
                  fonts_dir: Path, font_aliases: dict[str, str],
                  project_root: Path, warnings: list[str],
                  layer_index: int = 0) -> Image.Image:
    if not isinstance(layer, dict):
        raise CompositionError("layer must be an object")
    t = layer.get("type")
    if t not in LAYER_TYPES:
        raise CompositionError(f"unknown layer type: {t!r} (allowed: {sorted(LAYER_TYPES)})")

    if t == "image":
        layer_img, pos = _render_image_layer(layer, canvas_size, project_root)
    elif t == "text":
        layer_img, pos = _render_text_layer(layer, canvas_size, fonts_dir, font_aliases, warnings)
    elif t == "rect":
        layer_img, pos = _render_rect_layer(layer, canvas_size)
    elif t == "gradient":
        layer_img, pos = _render_gradient_layer(layer, canvas_size)
    elif t == "svg":
        layer_img, pos = _render_svg_layer(layer, canvas_size)
    elif t == "vignette":
        layer_img, pos = _render_vignette_layer(layer, canvas_size)
    else:  # pragma: no cover - guarded above
        raise CompositionError(f"unknown layer type: {t}")

    # Rotation (around the layer's own center)
    rotation = float(layer.get("rotation", 0))
    if rotation:
        before_w, before_h = layer_img.size
        layer_img = layer_img.rotate(-rotation, resample=Image.BICUBIC, expand=True)
        after_w, after_h = layer_img.size
        pos = (pos[0] - (after_w - before_w) // 2, pos[1] - (after_h - before_h) // 2)

    # Canvas-overflow check — file is still rendered, but we surface the issue
    # so the calling agent can re-compose with adjusted size/position.
    _check_canvas_overflow(layer_index, t, pos, layer_img.size, canvas_size, warnings)

    # Opacity
    opacity = float(layer.get("opacity", 1.0))
    if not 0.0 <= opacity <= 1.0:
        raise CompositionError(f"opacity out of [0,1]: {opacity}")
    if opacity < 1.0:
        alpha = layer_img.split()[3].point(lambda v: int(v * opacity))
        layer_img.putalpha(alpha)

    # Blend
    blend = layer.get("blend_mode", "normal")
    if blend not in BLEND_MODES:
        raise CompositionError(f"unknown blend_mode: {blend}")
    if blend == "normal":
        base.alpha_composite(layer_img, dest=pos)
    else:
        base = _apply_blend(base, layer_img, pos, blend)
    return base


# ── Layer implementations ─────────────────────────────────────────────────

def _render_image_layer(layer: dict, canvas_size: tuple[int, int],
                        project_root: Path) -> tuple[Image.Image, tuple[int, int]]:
    src = layer.get("src")
    if not src:
        raise CompositionError("image layer: 'src' is required")
    img = _load_image_src(src, project_root)
    target = _resolve_size_field(layer.get("size"), canvas_size, default=None)
    if target:
        fit = layer.get("fit", "cover")
        img = _fit_image(img, target, fit)
    pos = _resolve_position(layer.get("position"), img.size, canvas_size)
    return img.convert("RGBA"), pos


def _render_text_layer(layer: dict, canvas_size: tuple[int, int],
                       fonts_dir: Path, font_aliases: dict[str, str],
                       warnings: list[str]) -> tuple[Image.Image, tuple[int, int]]:
    text = layer.get("text")
    if text is None:
        raise CompositionError("text layer: 'text' is required")
    text = str(text)
    # Tolerate literal "\n" / "\r\n" / "\r" written as two-character escapes by
    # callers that double-encoded their JSON. Real newlines pass through unchanged.
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    if layer.get("uppercase"):
        text = text.upper()

    size_pt = int(layer.get("size", 48))
    font = _load_font(layer.get("font", "sans"), size_pt, fonts_dir, font_aliases, warnings)
    color = _parse_color(layer.get("color", "#ffffff"))
    align = layer.get("align", "left")
    if align not in ("left", "center", "right"):
        raise CompositionError(f"text align must be left|center|right, got {align!r}")
    line_height = float(layer.get("line_height", 1.1))
    tracking = int(layer.get("tracking", 0))

    # Wrapping. If the caller didn't set max_width we fall back to "canvas width
    # minus an 80 px safe margin on each side" so a too-wide title is wrapped
    # instead of overflowing the canvas.
    max_w_field = layer.get("max_width")
    cw, ch = canvas_size
    _DEFAULT_SAFE_MARGIN = 80
    if isinstance(max_w_field, dict) and "width_pct" in max_w_field:
        max_w: int | None = int(cw * float(max_w_field["width_pct"]) / 100.0)
    elif max_w_field is not None:
        max_w = int(max_w_field)
    else:
        max_w = max(1, cw - 2 * _DEFAULT_SAFE_MARGIN)

    lines = _wrap_text(text, font, max_w, tracking)

    # Measure layout
    line_metrics: list[tuple[str, int, int]] = []  # (line, width, ascent)
    for line in lines:
        w = _text_width(line, font, tracking)
        ascent, descent = font.getmetrics()
        line_metrics.append((line, w, ascent + descent))
    if not line_metrics:
        line_metrics = [("", 0, font.getmetrics()[0] + font.getmetrics()[1])]
    block_w = max(w for _, w, _ in line_metrics)
    line_h = int(line_metrics[0][2] * line_height)
    block_h = line_h * len(line_metrics)

    # Padding for stroke / shadow bleed
    stroke = layer.get("stroke") or {}
    stroke_w = int(stroke.get("width", 0)) if stroke else 0
    shadow = layer.get("shadow") or {}
    shadow_offset = shadow.get("offset", (0, 0)) if shadow else (0, 0)
    shadow_blur = int(shadow.get("blur", 0)) if shadow else 0
    pad = max(stroke_w, shadow_blur + max(abs(int(shadow_offset[0])), abs(int(shadow_offset[1])))) + 4

    layer_w = block_w + 2 * pad
    layer_h = block_h + 2 * pad
    img = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))

    # Shadow first (separate layer for blur)
    if shadow and (shadow_blur or any(shadow_offset)):
        shadow_color = _parse_color(shadow.get("color", "#00000088"))
        shadow_img = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow_img)
        _draw_text_block(sd, line_metrics, font, shadow_color, align, line_h, pad,
                         stroke_w=0, stroke_color=(0, 0, 0, 0), tracking=tracking)
        if shadow_blur:
            shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(shadow_blur))
        # Offset shadow
        ox, oy = int(shadow_offset[0]), int(shadow_offset[1])
        offset_img = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
        offset_img.paste(shadow_img, (ox, oy), shadow_img)
        img.alpha_composite(offset_img)

    # Main text + optional stroke
    draw = ImageDraw.Draw(img)
    stroke_color = _parse_color(stroke.get("color", "#000000")) if stroke_w else (0, 0, 0, 0)
    _draw_text_block(draw, line_metrics, font, color, align, line_h, pad,
                     stroke_w=stroke_w, stroke_color=stroke_color, tracking=tracking)

    pos = _resolve_position(layer.get("position"), (layer_w, layer_h), canvas_size)
    # Compensate for our internal padding so caller-specified anchor stays accurate
    pos = (pos[0] - pad + pad, pos[1] - pad + pad)  # no-op kept for clarity; pad equal both sides
    return img, pos


def _draw_text_block(draw: ImageDraw.ImageDraw,
                     line_metrics: list[tuple[str, int, int]],
                     font: ImageFont.FreeTypeFont,
                     color: tuple[int, int, int, int],
                     align: str, line_h: int, pad: int,
                     stroke_w: int, stroke_color: tuple[int, int, int, int],
                     tracking: int) -> None:
    block_w = max(w for _, w, _ in line_metrics)
    y = pad
    for line, w, _h in line_metrics:
        if align == "left":
            x = pad
        elif align == "right":
            x = pad + (block_w - w)
        else:  # center
            x = pad + (block_w - w) // 2
        _draw_line_tracking(draw, line, font, (x, y), color, stroke_w, stroke_color, tracking)
        y += line_h


def _draw_line_tracking(draw: ImageDraw.ImageDraw, line: str,
                        font: ImageFont.FreeTypeFont,
                        xy: tuple[int, int],
                        color: tuple[int, int, int, int],
                        stroke_w: int, stroke_color: tuple[int, int, int, int],
                        tracking: int) -> None:
    if tracking == 0:
        if stroke_w > 0:
            draw.text(xy, line, font=font, fill=color,
                      stroke_width=stroke_w, stroke_fill=stroke_color)
        else:
            draw.text(xy, line, font=font, fill=color)
        return
    x, y = xy
    for ch in line:
        if stroke_w > 0:
            draw.text((x, y), ch, font=font, fill=color,
                      stroke_width=stroke_w, stroke_fill=stroke_color)
        else:
            draw.text((x, y), ch, font=font, fill=color)
        x += int(font.getlength(ch)) + tracking


def _text_width(line: str, font: ImageFont.FreeTypeFont, tracking: int) -> int:
    if tracking == 0:
        return int(font.getlength(line))
    return sum(int(font.getlength(ch)) + tracking for ch in line) - tracking if line else 0


def _wrap_text(text: str, font: ImageFont.FreeTypeFont,
               max_width: int | None, tracking: int) -> list[str]:
    out: list[str] = []
    for paragraph in text.split("\n"):
        if max_width is None or not paragraph:
            out.append(paragraph)
            continue
        words = paragraph.split(" ")
        line = ""
        for word in words:
            trial = (line + " " + word).strip() if line else word
            if _text_width(trial, font, tracking) <= max_width:
                line = trial
            else:
                if line:
                    out.append(line)
                # Single word too wide → hard-break it
                if _text_width(word, font, tracking) > max_width:
                    chunk = ""
                    for ch in word:
                        if _text_width(chunk + ch, font, tracking) <= max_width:
                            chunk += ch
                        else:
                            if chunk:
                                out.append(chunk)
                            chunk = ch
                    line = chunk
                else:
                    line = word
        if line:
            out.append(line)
    return out


def _render_rect_layer(layer: dict, canvas_size: tuple[int, int]) -> tuple[Image.Image, tuple[int, int]]:
    x, y, w, h = _resolve_rect(layer.get("rect"), layer.get("position"), layer.get("size"), canvas_size)
    if w <= 0 or h <= 0:
        raise CompositionError(f"rect must have positive size: ({w}, {h})")
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = _parse_color(layer.get("fill", "#00000000")) if layer.get("fill") else None
    radius = int(layer.get("radius", 0))
    border = layer.get("border") or {}
    border_w = int(border.get("width", 0)) if border else 0
    border_color = _parse_color(border.get("color", "#000000")) if border_w else None
    if radius > 0:
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius,
                               fill=fill, outline=border_color, width=border_w)
    else:
        draw.rectangle((0, 0, w - 1, h - 1),
                       fill=fill, outline=border_color, width=border_w)
    return img, (x, y)


def _render_gradient_layer(layer: dict, canvas_size: tuple[int, int]) -> tuple[Image.Image, tuple[int, int]]:
    x, y, w, h = _resolve_rect(layer.get("rect"), layer.get("position"), layer.get("size"), canvas_size)
    if w <= 0 or h <= 0:
        raise CompositionError(f"gradient must have positive size: ({w}, {h})")

    # Backward-friendly: accept "from"/"to" too
    colors = layer.get("colors")
    if colors is None:
        if "from" in layer and "to" in layer:
            colors = [layer["from"], layer["to"]]
        else:
            raise CompositionError("gradient: provide 'colors' (list) or 'from'+'to'")
    if not isinstance(colors, list) or len(colors) < 2:
        raise CompositionError("gradient.colors needs at least 2 colors")
    parsed = [_parse_color(c) for c in colors]
    n = len(parsed)
    stops = layer.get("stops")
    if stops is None:
        stops = [i / (n - 1) for i in range(n)]
    if len(stops) != n:
        raise CompositionError("gradient.stops length must match colors length")

    direction = layer.get("direction", "bottom")
    angle_deg = _direction_to_angle(direction)
    rad = math.radians(angle_deg)
    dx, dy = math.cos(rad), math.sin(rad)

    # Project each pixel along (dx, dy); normalize to [0, 1]
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pixels = img.load()

    # Compute projection range over the rect corners
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    projections = [px * dx + py * dy for px, py in corners]
    p_min, p_max = min(projections), max(projections)
    span = (p_max - p_min) or 1.0

    for py in range(h):
        for px in range(w):
            t = (px * dx + py * dy - p_min) / span
            pixels[px, py] = _interpolate_gradient(parsed, stops, t)
    return img, (x, y)


def _direction_to_angle(direction: Any) -> float:
    """Direction → angle in degrees (0° = +x / right, 90° = +y / down)."""
    if isinstance(direction, (int, float)):
        return float(direction)
    mapping = {
        "right": 0, "bottom_right": 45, "bottom": 90, "bottom_left": 135,
        "left": 180, "top_left": 225, "top": 270, "top_right": 315,
    }
    if direction in mapping:
        return float(mapping[direction])
    raise CompositionError(f"unknown gradient direction: {direction!r}")


def _interpolate_gradient(colors: list[tuple[int, int, int, int]],
                          stops: list[float], t: float) -> tuple[int, int, int, int]:
    if t <= stops[0]:
        return colors[0]
    if t >= stops[-1]:
        return colors[-1]
    for i in range(len(stops) - 1):
        s0, s1 = stops[i], stops[i + 1]
        if s0 <= t <= s1:
            local = (t - s0) / (s1 - s0) if s1 > s0 else 0.0
            c0, c1 = colors[i], colors[i + 1]
            return (
                int(c0[0] + (c1[0] - c0[0]) * local),
                int(c0[1] + (c1[1] - c0[1]) * local),
                int(c0[2] + (c1[2] - c0[2]) * local),
                int(c0[3] + (c1[3] - c0[3]) * local),
            )
    return colors[-1]


def _render_vignette_layer(layer: dict, canvas_size: tuple[int, int]) -> tuple[Image.Image, tuple[int, int]]:
    strength = float(layer.get("strength", 0.3))
    color = _parse_color(layer.get("color", "#000000"))
    cw, ch = canvas_size
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    pixels = img.load()
    cx, cy = cw / 2, ch / 2
    max_d = math.hypot(cx, cy)
    for y in range(ch):
        for x in range(cw):
            d = math.hypot(x - cx, y - cy) / max_d  # 0 center → 1 corner
            a = max(0.0, min(1.0, (d - 0.4) / 0.6)) * strength
            pixels[x, y] = (color[0], color[1], color[2], int(255 * a))
    return img, (0, 0)


def _render_svg_layer(layer: dict, canvas_size: tuple[int, int]) -> tuple[Image.Image, tuple[int, int]]:
    svg_str = layer.get("svg")
    if not svg_str or not isinstance(svg_str, str):
        raise CompositionError("svg layer: 'svg' (raw SVG string) is required")
    target = _resolve_size_field(layer.get("size"), canvas_size, default=None)
    img = _render_svg_to_image(svg_str, target)
    pos = _resolve_position(layer.get("position"), img.size, canvas_size)
    return img, pos


def _render_svg_to_image(svg_str: str, target_size: tuple[int, int] | None) -> Image.Image:
    """Render SVG via svglib + reportlab (pure-Python, no native deps)."""
    try:
        from svglib.svglib import svg2rlg  # type: ignore
        from reportlab.graphics import renderPM  # type: ignore
    except ImportError as e:
        raise CompositionError(
            "SVG rendering requires 'svglib' and 'reportlab' (pip install svglib reportlab). "
            f"Import failed: {e}"
        )
    drawing = svg2rlg(io.StringIO(svg_str))
    if drawing is None:
        raise CompositionError("SVG could not be parsed (invalid SVG)")
    if target_size:
        tw, th = target_size
        sx = tw / drawing.width if drawing.width else 1.0
        sy = th / drawing.height if drawing.height else 1.0
        # Fit (preserve aspect) — pick smaller scale, then center later via position
        scale = min(sx, sy) if (sx > 0 and sy > 0) else max(sx, sy, 1.0)
        drawing.width = int(drawing.width * scale)
        drawing.height = int(drawing.height * scale)
        drawing.scale(scale, scale)
    png_bytes = renderPM.drawToString(drawing, fmt="PNG", bg=0xFFFFFF)
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    # renderPM forces a white background — derive an alpha mask so white becomes
    # transparent (and anti-aliased edges fade). Pure ImageChops, no deprecated APIs.
    r, g, b, _a = img.split()
    inv_r = ImageChops.invert(r)
    inv_g = ImageChops.invert(g)
    inv_b = ImageChops.invert(b)
    alpha = ImageChops.lighter(ImageChops.lighter(inv_r, inv_g), inv_b)
    img.putalpha(alpha)
    return img


# ── Image loading + fitting ───────────────────────────────────────────────

def _load_image_src(src: str, project_root: Path) -> Image.Image:
    if src.startswith("data:"):
        # data URI: data:image/<fmt>;base64,<payload>
        try:
            _, payload = src.split(",", 1)
            raw = base64.b64decode(payload)
            return Image.open(io.BytesIO(raw)).convert("RGBA")
        except Exception as e:
            raise CompositionError(f"failed to decode data URI: {e}")
    p = Path(src)
    if not p.is_absolute():
        p = (project_root / src).resolve()
    if not p.exists():
        raise CompositionError(f"image src not found: {p}")
    try:
        return Image.open(p).convert("RGBA")
    except Exception as e:
        raise CompositionError(f"failed to load image {p}: {e}")


def _fit_image(img: Image.Image, target: tuple[int, int], fit: str) -> Image.Image:
    tw, th = target
    if fit == "stretch":
        return img.resize((tw, th), Image.LANCZOS)
    sw, sh = img.size
    src_aspect = sw / sh if sh else 1.0
    dst_aspect = tw / th if th else 1.0
    if fit == "contain":
        if src_aspect > dst_aspect:
            new_w, new_h = tw, max(1, int(tw / src_aspect))
        else:
            new_w, new_h = max(1, int(th * src_aspect)), th
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        canvas = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        canvas.paste(resized, ((tw - new_w) // 2, (th - new_h) // 2), resized)
        return canvas
    # cover (default): scale to fill, then center-crop
    if src_aspect > dst_aspect:
        new_h = th
        new_w = max(1, int(th * src_aspect))
    else:
        new_w = tw
        new_h = max(1, int(tw / src_aspect))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - tw) // 2
    top = (new_h - th) // 2
    return resized.crop((left, top, left + tw, top + th))


# ── Font loading ──────────────────────────────────────────────────────────

def _load_font(font_spec: str, size: int, fonts_dir: Path,
               aliases: dict[str, str], warnings: list[str]) -> ImageFont.FreeTypeFont:
    # 1) Absolute / existing path
    if font_spec:
        p = Path(font_spec)
        if p.is_absolute() and p.exists():
            return ImageFont.truetype(str(p), size=size)
        # 2) Relative within fonts_dir
        if not p.is_absolute():
            candidate = fonts_dir / font_spec
            if candidate.exists():
                return ImageFont.truetype(str(candidate), size=size)
        # 3) Alias
        if font_spec in aliases:
            mapped = aliases[font_spec]
            candidate = fonts_dir / mapped if not Path(mapped).is_absolute() else Path(mapped)
            if candidate.exists():
                return ImageFont.truetype(str(candidate), size=size)
            warnings.append(f"font alias '{font_spec}' → '{mapped}' not found at {candidate}")
        # 4) System fallback by alias category
        for fname in SYSTEM_FONT_FALLBACKS.get(font_spec, []):
            try:
                return ImageFont.truetype(fname, size=size)
            except OSError:
                continue
        # 5) Try the spec itself as a system font name
        try:
            return ImageFont.truetype(font_spec, size=size)
        except OSError:
            pass
    warnings.append(f"font '{font_spec}' not found — falling back to PIL default")
    # PIL default is a bitmap font; scale poorly but keeps render running
    return ImageFont.load_default()


# ── Blending ──────────────────────────────────────────────────────────────

def _apply_blend(base: Image.Image, layer: Image.Image, pos: tuple[int, int],
                 mode: str) -> Image.Image:
    """Apply blend modes (multiply/screen/overlay) for the layer onto base.

    Falls back to simple alpha composite for the non-overlapping region.
    """
    # Build a full-canvas layer with the small layer pasted at pos
    full = Image.new("RGBA", base.size, (0, 0, 0, 0))
    full.alpha_composite(layer, dest=pos)
    base_rgb = base.convert("RGB")
    layer_rgb = full.convert("RGB")
    if mode == "multiply":
        blended = ImageChops.multiply(base_rgb, layer_rgb)
    elif mode == "screen":
        blended = ImageChops.screen(base_rgb, layer_rgb)
    elif mode == "overlay":
        # Pillow doesn't ship overlay → implement: a<0.5 -> 2ab, else 1-2(1-a)(1-b)
        blended = _overlay(base_rgb, layer_rgb)
    else:
        return base
    # Use layer alpha as the blend mask so non-covered base stays as-is
    mask = full.split()[3]
    out_rgb = Image.composite(blended, base_rgb, mask)
    out = out_rgb.convert("RGBA")
    # Preserve base alpha
    out.putalpha(ImageChops.lighter(base.split()[3], mask))
    return out


def _overlay(a: Image.Image, b: Image.Image) -> Image.Image:
    """Per-channel overlay: multiply where base<128, screen otherwise."""
    mult = ImageChops.multiply(a, b)
    scr = ImageChops.screen(a, b)
    bands = []
    for base_band, mult_band, scr_band in zip(a.split(), mult.split(), scr.split()):
        mask = base_band.point(lambda v: 255 if v >= 128 else 0)
        bands.append(Image.composite(scr_band, mult_band, mask))
    return Image.merge("RGB", bands)
