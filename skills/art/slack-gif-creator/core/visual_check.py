#!/usr/bin/env python3
"""
Visual Inspection Utilities - fallback to "see" images/GIFs without vision.

Only needed for models WITHOUT image input. If your model can see images, just
render a frame and look at it directly - no ASCII map required. This module
turns pixels into something readable:

  * render_ascii()    -> brightness map of a frame (composition check)
  * color_classes()   -> coarse grid of dominant color classes (palette check)
  * count_in_region() -> count pixels matching a color rule inside a box
  * check_motion()    -> frame-to-frame diff (does it actually animate?)
  * inspect()         -> combined report for a quick look

Run these helpers only when you cannot see images - they catch broken layouts and frozen GIFs.
"""

from pathlib import Path

import numpy as np
from PIL import Image

_BRIGHTNESS_RAMP = " .:-=+*#%@"


# ---------------------------------------------------------------- loading
def load_frame(source, frame: int = 0) -> np.ndarray:
    """Load one frame as an (H, W, 3) uint8 RGB array.

    source: path to an image/GIF or a PIL Image.
    """
    if isinstance(source, Image.Image):
        img = source
    else:
        img = Image.open(Path(source))
    if getattr(img, "is_animated", False):
        img.seek(frame)
    return np.asarray(img.convert("RGB"))


# ---------------------------------------------------------------- ascii
def render_ascii(
    source,
    frame: int = 0,
    cols: int = 80,
    rows: int = 40,
    ramp: str = _BRIGHTNESS_RAMP,
) -> str:
    """Render a frame as ASCII brightness art.

    Dark pixels become early chars (' '), bright pixels late chars ('@').
    Good for checking composition: sky up, water down, subject in the middle.
    """
    a = load_frame(source, frame).astype(np.float64)
    H, W = a.shape[:2]
    cell_h, cell_w = max(1, H // rows), max(1, W // cols)
    out = []
    for r in range(rows):
        line = []
        for c in range(cols):
            block = a[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w]
            avg = block.reshape(-1, 3).mean(axis=0)
            lum = 0.299 * avg[0] + 0.587 * avg[1] + 0.114 * avg[2]
            idx = min(len(ramp) - 1, int(lum / 256.0 * len(ramp)))
            line.append(ramp[idx])
        out.append("".join(line))
    return "\n".join(out)


# ---------------------------------------------------------------- colors
_COLOR_RULES = {
    "red":    lambda r, g, b: r > 170 and g < 110 and b < 110,
    "yellow": lambda r, g, b: r > 190 and g > 150 and b < 140,
    "brown":  lambda r, g, b: 90 < r < 190 and 55 < g < 135 and b < 95 and r > g > b,
    "green":  lambda r, g, b: g > 95 and g > r and g > b,
    "blue":   lambda r, g, b: b > 100 and b > r,
    "white":  lambda r, g, b: r > 200 and g > 200 and b > 200,
    "dark":   lambda r, g, b: 0.299 * r + 0.587 * g + 0.114 * b < 70,
}
_CLASS_CHARS = {
    "red": "R", "yellow": "Y", "brown": "B", "green": "G",
    "blue": "W", "white": "L", "dark": "D",
}


def _classify(rgb) -> str:
    for name in ("red", "yellow", "brown", "green", "blue", "white", "dark"):
        if _COLOR_RULES[name](*rgb):
            return _CLASS_CHARS[name]
    return "."


def color_classes(
    source, frame: int = 0, cols: int = 16, rows: int = 16, legend: bool = True
) -> str:
    """Coarse grid where each cell shows its dominant color class.

    R=red  Y=yellow  B=brown  G=green  W=water/blue  L=light  D=dark  .=mixed
    """
    a = load_frame(source, frame).astype(np.float64)
    H, W = a.shape[:2]
    cell_h, cell_w = max(1, H // rows), max(1, W // cols)
    grid = []
    for r in range(rows):
        line = []
        for c in range(cols):
            block = a[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w]
            avg = block.reshape(-1, 3).mean(axis=0)
            line.append(_classify(avg))
        grid.append("".join(line))
    s = "\n".join(grid)
    if legend:
        s += "\nR=red  Y=yellow  B=brown  G=green  W=water  L=light  D=dark  .=mixed"
    return s


# ---------------------------------------------------------------- regions
def count_in_region(source, box, color="brown", frame: int = 0) -> int:
    """Count pixels matching a color rule inside (x0, y0, x1, y1).

    color: a name from _COLOR_RULES, or a callable(r, g, b) -> bool.
    Example: count_in_region('out.gif', (120, 220, 360, 400), 'red')
             -> number of red pixels in the boat/beaver area.
    """
    a = load_frame(source, frame)
    x0, y0, x1, y1 = box
    region = a[y0:y1, x0:x1].reshape(-1, 3)
    rule = color if callable(color) else _COLOR_RULES[color]
    mask = np.array([rule(r, g, b) for r, g, b in region])
    return int(mask.sum())


# ---------------------------------------------------------------- motion
def check_motion(
    gif_path, frame_a: int = 0, frame_b: int | None = None, verbose: bool = True
) -> tuple[float, int]:
    """Mean and max pixel diff between two frames of an animated GIF.

    A mean diff near 0 with max 0 means the "GIF" is a static image.
    """
    a = load_frame(gif_path, frame_a).astype(np.float64)
    fb = frame_b if frame_b is not None else frame_a + 1
    b = load_frame(gif_path, fb).astype(np.float64)
    mean = float(np.abs(a - b).mean())
    mx = int(np.abs(a - b).max())
    if verbose:
        status = "ANIMATED" if mean > 0.5 else ("static?" if mean > 0 else "IDENTICAL")
        print(f"motion diff frame {frame_a}->{fb}: mean={mean:.2f} max={mx} -> {status}")
    return mean, mx


# ---------------------------------------------------------------- report
def inspect(source, cols: int = 80, rows: int = 40, grid: int = 16, frame: int = 0) -> None:
    """Print one report: ASCII map, color classes, and (for GIFs) motion."""
    print("== ASCII brightness map ==")
    print(render_ascii(source, frame=frame, cols=cols, rows=rows))
    print("\n== dominant color classes ==")
    print(color_classes(source, frame=frame, cols=grid, rows=grid))
    if not isinstance(source, Image.Image):
        try:
            with Image.open(Path(source)) as im:
                if getattr(im, "is_animated", False):
                    print("\n== motion ==")
                    check_motion(source, frame_a=0, frame_b=1)
        except Exception:
            pass
