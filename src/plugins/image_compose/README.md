# Image Compose

Deterministic image composition from a JSON spec: background, text, shapes,
gradients, SVG, vignette — layered bottom-up and rendered with Pillow. Built
for covers, where a diffusion model gives you the artwork but the typography
has to land on an exact pixel.

## What it provides

`type = ["mcp-server"]`, entrypoint `server:PLUGIN_FACTORY` (this plugin has no
`plugin.py`; the factory sits next to the server).

| Tool | Purpose |
|---|---|
| `image_compose_render` | render a layer spec to PNG / WEBP / JPEG |
| `image_compose_analyze` | brightness, colour and edge statistics of a region |
| `image_compose_find_region` | find the calmest rectangle for a text block |

`analyze` and `find_region` exist so an agent can pick a text colour and a
position **from measurements** instead of looking at the background with a
vision model and guessing. `analyze` returns Rec. 709 luminance mean and
standard deviation, edge density, a 5-colour palette and a
`light_text` / `dark_text` / `midtone_uncertain_use_backdrop` recommendation;
`find_region` slides a window of the requested size and ranks candidates by
homogeneity, optionally restricted to a third or half of the image.

## Layer types

`image`, `text`, `rect`, `gradient`, `svg`, `vignette`. Every layer takes
`opacity`, `rotation`, `blend_mode` (`normal|multiply|screen|overlay`) and a
position that is either absolute `[x, y]` or an anchor with a pixel or
percentage offset. The full per-field reference lives in `schema.yaml` — it is
the prompt the model reads, so that file is the specification.

## Two warnings the renderer emits

Both are warnings, never errors: the image still renders, and a caller that
knows what it is doing can ignore them.

* **Canvas overflow** — a layer's bounding box leaves the canvas, reported with
  the exact overhang per edge. Skipped for full-canvas layers (`vignette`) and
  for `image` layers fitted to the canvas, where full-bleed is the point.
* **Layer overlap** — text and SVG layers that intersect, or sit closer than
  `overlap_min_gap_px`. Only text/SVG pairs are checked: a text block over a
  gradient is the normal case, two headlines on top of each other is not.

## Fonts

`fonts/` holds the TTFs the aliases map to. The compositor resolves
`font_aliases` first, then platform fonts (`SYSTEM_FONT_FALLBACKS`), then
PIL's bitmap default — so a missing font degrades the look but never fails the
render. The alias inventory and download instructions are in `fonts/README.md`.

## Configuration

```yaml
image_compose:
  type: image_compose
  enabled: true
  fonts_dir: "src/plugins/image_compose/fonts"
  output_root: "."
  overlap_check_enabled: true
  overlap_min_gap_px: 30
  # font_aliases:
  #   display_grotesk: "Oswald-Bold.ttf"
```

## Dependencies, and why there are four of them

`svglib>=1.5`, `reportlab[pycairo]>=4.0`, `numpy>=1.26`. The SVG path is an
onion, and each layer only failed once the one below it worked:

1. `svglib`/`reportlab` were never declared and only ever arrived transitively
   — a fresh server venv rendered every SVG layer as an error.
2. reportlab 4 rasterises through `rlPyCairo`; without it `renderPM` fails
   *after* those imports succeed. On hosts without a wheel its pycairo build
   needs system headers (Python 3.13: `apt install libcairo2-dev pkg-config`).
3. Text-to-path in svglib additionally needs `freetype-py` — a circle renders
   without it, an SVG containing **text** does not.

The `[pycairo]` extra carries both backends with reportlab's own pins. Pinning
them separately collided immediately (`freetype-py>=2.4` against reportlab's
`<2.4`), so the extra is the source of truth.

`numpy` is imported lazily at three call sites — `analyze`, `find_region`, and
the SVG layer. The third is not obvious: `renderPM` cannot draw onto a
transparent background, so an SVG is rendered **twice**, on black and on white,
and true alpha plus un-premultiplied colour is reconstructed from the
difference. A naive white-to-transparent mask would erase white strokes.

## Tests

`tests/test_image_compose.py`.

## License

Apache-2.0 — see `LICENSE`.
