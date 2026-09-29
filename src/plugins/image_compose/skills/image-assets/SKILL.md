---
name: image-assets
description: The loop for producing a game image asset — read the brief for size, tiling, background and style; decide between generation and pure composition; prompt SDXL for game assets; look and judge; compose onto the exact canvas; verify tiling; report the path. Use for any texture, sprite, icon or UI element request.
metadata:
  version: '1.0.0'
---

# 1. Read the brief into five facts

| Fact | If missing |
|---|---|
| What, and where it is used | Ask nothing — infer the plainest reading and say it in the report |
| Size in pixels | 1024×1024 for a texture, 128×128 for a sprite, and say so |
| Tileable? | no |
| Background | opaque for textures; transparent for sprites (a cutout, see 5) |
| Style | "clean game asset, flat lighting" |

# 2. Generate, or only compose?

**Compose only** (`images_render`, no ComfyUI) when the asset is geometric
or typographic: icons, buttons, panels, bars, frames, placeholder
sprites. Rect, gradient, svg and text layers on a `transparent` canvas
give real alpha and exact pixels. It is instant and deterministic; prefer
it whenever it can plausibly satisfy the brief.

**Generate** (`comfyui_workflow`) for anything organic or painterly:
textures, characters, props, backgrounds. Then compose the result onto the
exact canvas.

# 3. Prompting SDXL for game assets

`comfyui_workflow(operation="list", category="image_generation")` once —
`juggernaut_xl` for realistic, `sdxl_txt2img` for stylised. Sizes
divisible by 8; generate at 1024 on the long side, near the target aspect.

| Asset | Add to the prompt |
|---|---|
| Texture | `seamless tileable texture, top-down, even diffuse lighting, no shadows, no objects, fills the frame` |
| Sprite (top-down / side) | `game sprite, single object, centered, plain flat <colour> background, orthographic <view> view, no text` |
| Prop | `single object, centered, plain background, studio lighting, full object visible` |
| Pixel art | `pixel art, <n> colours, crisp pixels, no anti-aliasing` — and compose with `fit: contain` onto a small canvas |

Negative prompt, always: `text, watermark, signature, multiple objects,
cropped, blurry, frame, border`. Fixed `seed` when you regenerate for a
small change; new seed when the composition is wrong.

```
execute → prompt_id → wait_for_completion(prompt_id) → result(prompt_id, download=true)
```

# 4. Look, then judge against the brief

`result` with `include_content: true`, or `media_ops_load` the path.
Check, in this order: one object or a filled texture; the view asked for;
no text; background as asked; nothing cropped. One miss → change the part
of the prompt that caused it, keep the seed; regenerate. Three misses →
deliver the best, name the deviation.

# 5. Compose onto the exact canvas

```json
{"size": [W, H], "background": "transparent",
 "layers": [{"type": "image", "src": "data/comfyui/outputs/<file>.png",
             "position": {"anchor": "center"}, "size": [W, H], "fit": "cover"}]}
```

`images_render(spec=..., output_path="data/workspace/images/<name>.png",
layers_dir="", include_content=true)`. Paths are project-relative: a bare
name would land outside the workspace and is refused.
`fit: cover` fills and crops; `contain` letterboxes.

**Sprites**: generate the object on a plain, contrasting background (say so
in the prompt: "single object, centered, plain light grey background"),
then add `"cutout": true, "trim": true` to the image layer with
`"fit": "contain"` on the `transparent` canvas — rembg removes the
background, `trim` crops to the object, so it fills the canvas instead of
sitting small in the generator's frame. Pixel art gets
`"alpha_threshold": 128` as well, so every pixel is either opaque or
clear. Look at the result over the colour it will sit on. Thin parts
(whiskers, rope, antennae) can be lost — retry with
`"cutout": "birefnet-general-lite"`, which keeps them at roughly five
times the time. A busy background gives a ragged edge; regenerate on a
plainer one rather than accepting it.

# 6. Tileable? Prove it

```json
{"size": [2W, 2H], "layers": [
  {"type": "image", "src": "data/workspace/images/<name>.png", "position": [0, 0], "size": [W, H]},
  {"type": "image", "src": "data/workspace/images/<name>.png", "position": [W, 0], "size": [W, H]},
  {"type": "image", "src": "data/workspace/images/<name>.png", "position": [0, H], "size": [W, H]},
  {"type": "image", "src": "data/workspace/images/<name>.png", "position": [W, H], "size": [W, H]}]}
```

Render it with `layers_dir=""` — a throwaway needs no layer export — and
look at the cross in the middle. A visible seam → regenerate with
`seamless tileable` moved to the front of the prompt and a new seed; two
seams later, deliver and say "seam visible at the horizontal edge".

A brief that said "tileable" and a report that says `Tileable: yes` mean
this render happened. Without it the line reads `no (not checked)`.

# 7. Report

The five-line block from your prompt. The path is the deliverable; the
`Verified:` line is what makes it trustworthy.

**The path is the one the tool returned.** A successful render replies
with `output_path`; copy that. An error reply has none — then there is no
path to report, only a fix to make and a render to repeat.
