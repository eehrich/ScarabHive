---
name: image-assets
description: The loop for producing an image asset — read the brief for size, tiling, background and style; decide between drawing it with a canvas script and generating it; draw icons, UI, logos, patterns, procedural textures and pixel art; prompt SDXL for the rest; work from a picture the person sent; keep a named style the same across many pictures; game backgrounds as one scene, a horizontal tile or parallax layers; look and judge; compose onto the exact canvas; verify tiling; hand the file to the person. Use for any texture, sprite, icon, illustration, background or UI element request.
metadata:
  version: '1.2.0'
---

# 1. Read the brief into five facts

| Fact | If missing |
|---|---|
| What, and where it is used | Ask nothing — infer the plainest reading and say it in the report |
| Size in pixels | 1024×1024 for a texture, 128×128 for a sprite, and say so |
| Tileable? | no |
| Background | opaque for textures; transparent for sprites (a cutout, see 5) |
| Style | a named style if the brief gives one (section 2c), else "clean game asset, flat lighting" |

# 2. Draw it, or generate it?

**Draw** (`images_render` with a `canvas` layer, no ComfyUI) whatever a
drawing can be: icons, buttons, panels, bars, frames, logos, badges, flat
or vector-style illustrations, patterns, procedural textures (stone,
wood grain, fabric, metal, clouds, noise), pixel art, placeholder
sprites. It takes a second, has real alpha and exact pixels, is the same
picture every time, and you change one line to change one thing. Prefer
it whenever it can plausibly satisfy the brief — section 2a.

**Generate** (`comfyui_workflow`) what a drawing cannot be: anything
photographic or painterly — characters, creatures, props with material
detail, painted backgrounds. Then compose the result onto the exact
canvas (section 5).

Both in one asset is normal: a generated picture as an `image` layer
under a drawn frame, or a drawing loaded into another through `images`.

# 2a. Drawing with a canvas script

```json
{"size": [W, H], "background": "transparent",
 "layers": [{"type": "canvas", "seed": 1, "script": "<JavaScript>"}]}
```

The script is a function body. `ctx` is a browser canvas 2D context of
`w`×`h` pixels — the whole API you know: paths, `Path2D` (also from an
SVG path string), gradients, `shadowBlur`, `ctx.filter`, patterns, text,
`getImageData`. What you do not paint stays transparent. An error comes
back with the line of your script; fix that line and render again.

**Build it like a drawing, not like a diagram.** Big readable shapes
first, then the details that make it this thing. A palette of five to
seven colours, chosen before the first shape. Volume from a gradient and
one soft shadow under big shapes, not from many small ones. One outline
weight (`lineJoin = 'round'`, `lineCap = 'round'`), or none at all.
Derive every coordinate from `w` and `h`, so the same script draws any
size. An icon reads at its real size: look at it there, not enlarged.

| You need | Do |
|---|---|
| Randomness | `Math.random()` — it is seeded: same `seed`, same picture; another `seed` is a variation |
| A seamless noise texture | `pixels(ctx, (x, y) => { const v = fbm(x / w * 8, y / h * 8, 4, 8) * 255; return [v, v, v]; })` — the layer must span a whole number of periods (here 8) |
| Seamless shapes | fill the background first, then wrap each shape: `tile(ctx, () => { /* one stone */ })` — what crosses an edge re-enters on the other side. Pick its position before the call and keep overlaps in mind across the edges too |
| An existing picture | `"images": {"wood": "data/workspace/images/wood.png"}` → `ctx.drawImage(img.wood, …)`, `ctx.createPattern(img.wood, 'repeat')`; SVG markup works as a source too |
| A particular font | `"fonts": ["display"]` → `ctx.font = '64px "display"'` — a text layer's font name (`display`, `serif_bold`, `script`, …) or a file of the fonts folder, named in quotes exactly as listed; system fonts need no entry |
| Pixel art | draw at the native size on `new OffscreenCanvas(16, 16)` with whole-number `fillRect`s, then `ctx.imageSmoothingEnabled = false; ctx.drawImage(small, 0, 0, w, h)` |
| A filter over everything | draw on an `OffscreenCanvas`, then `ctx.filter = 'blur(2px)'; ctx.drawImage(off, 0, 0)` |

`noise(x, y, period)` and `fbm(x, y, octaves, period)` return 0..1 on a
unit grid; without a period they do not repeat. **The one rule for a
seamless result: each axis runs through a whole number of periods.** With
period `P`, sample `fbm(x / w * P * a, y / h * P * b, 4, P)` for whole
numbers `a` and `b` — `a = b = 1` is even noise, `a = 1, b = 8` is
stretched along x (wood grain, brushed metal), and a fraction in `a` or
`b` shows as a seam. Layer several for material: a low frequency for
colour patches, a high one for grain. Warping stays seamless when the
offset is itself periodic noise: `fbm(u + 2 * noise(u, v, P), v, 4, P)`.

A drawing is checked like a generated picture (section 4): does it read
at once as what the brief asks, at the size it is used? If a second look
says "a blob" or "a diagram", redraw the shapes; do not add detail to a
shape that is wrong.

# 2b. A picture the person sent

It is in the conversation, not on disk: `image_media_list_context` gives its
id, `image_media_save` writes it to `data/workspace/images/uploads/<name>.png`.
From there it is a reference for ComfyUI (img2img keeps its layout,
IPAdapter its style — section 3) or an `image` layer. Say in the report what
you took from it.

# 2c. A style: the same look across many pictures

A style is a folder, `data/workspace/images/styles/<name>/`: one to three
reference pictures and `style.json`, the recipe every picture in that style
is made with:

```json
{"name": "<name>", "references": ["data/workspace/images/styles/<name>/ref1.png"],
 "prompt": "<style words appended to every prompt: medium, rendering, light, mood>",
 "negative": "<what this style never has>",
 "palette": ["#1d2b3a", "#e8a33d", "…"],
 "checkpoint": "Juggernaut-XL_v9.safetensors", "weight": 0.75,
 "notes": "<what made a picture fit or not>"}
```

**Making one** (the person sends a reference, or asks for a look): save the
reference into the folder, describe its style in `prompt` — medium,
brushwork, light, colour, level of detail, not its subject — and pick
`palette` from it. Make one test picture of another subject with
`sdxl_ipadapter` (`reference_image` the saved picture, `checkpoint` and
`weight` from the recipe), look at it beside the reference, adjust `weight`
(higher: closer to the reference, also in content) or `prompt`, write
`style.json`, and show the person the test picture.

**Using one**: `image_files_list_directory("data/workspace/images/styles")`
when the brief names a style or says "the same as before"; read its
`style.json`; every generation in it is `sdxl_ipadapter` with its reference,
checkpoint and weight, the recipe's `prompt` after the subject and its
`negative` added. A drawn element takes the palette. When a result fits
especially well, it may join the references; note why in `notes`.

# 2d. Game backgrounds

| Kind | Make it |
|---|---|
| One scene | Generate near the target aspect (16:9: 1344×768), compose `fit: cover` onto the exact size. Keep the area where the game draws its characters calm: no strong detail where they stand |
| Scrolling, endless horizontally | Generate wider than tall; make the left and right edge meet: compose the picture, then a copy shifted by half its width, and repaint the seam with `sdxl_img2img` at a high `start_at_step` (the rest changes only slightly). Check with a 2×1 tiling (section 6, two copies side by side) |
| Parallax layers | One picture per layer: far (sky, horizon: opaque), middle and near (transparent: generated on a plain contrasting background, then cutout — section 5). Same style, same light direction, the same horizon height in every layer; nearer layers have stronger contrast and larger shapes. Check them stacked in one render: far, middle, near |

Size: the target screen (1920×1080 unless the brief says otherwise); a
scrolling layer is a whole number of screen widths. Hand over each layer
on its own and the stacked preview.

# 3. Generating with ComfyUI

**Is it there?** `comfyui_workflow(operation="server_status")` once,
before the first generation. Offline is not an error to wait out or
retry: draw the asset if a drawing can carry the brief and say so in the
report (`Verified: drawn — ComfyUI offline`); if only a generated picture
can, report that and stop.

`comfyui_workflow(operation="list", category="image_generation")` once —
`juggernaut_xl` for realistic, `sdxl_txt2img` for stylised. Sizes
divisible by 8; generate at 1024 on the long side, near the target aspect.

**Your drawing as the composition.** When the layout matters — where the
object sits, its silhouette, the colour areas — draw it first as flat
shapes into the workspace, at the generation size and on an opaque
background (the picture comes out at the drawing's size), then let
`sdxl_img2img` paint over it: `reference_image` is the path the render
returned, `start_at_step` sets how much it may change (steps 30: 12
keeps the layout, 7 keeps only the masses; higher stays closer).
`sdxl_ipadapter` takes a reference the same way for style instead of
layout.

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

A render with `include_content: true` shows a drawing; for a generated
picture `result` with `include_content: true`, or `image_media_load` the path.
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
look at the cross in the middle. A visible seam in a drawn texture is a
bug in the script — noise sampled without its period, or a shape outside
`tile` — fix it. In a generated one → regenerate with
`seamless tileable` moved to the front of the prompt and a new seed; two
seams later, deliver and say "seam visible at the horizontal edge".

A brief that said "tileable" and a report that says `Tileable: yes` mean
this render happened. Without it the line reads `no (not checked)`.

# 7. Hand it over, then report

`image_media_share(path=<output_path>)` for each finished file and put the
Markdown it returns into the answer — that is how the person sees and
downloads it. Then the five-line block from your prompt. The `Verified:`
line is what makes it trustworthy.

**The path is the one the tool returned.** A successful render replies
with `output_path`; copy that. An error reply has none — then there is no
path to report, only a fix to make and a render to repeat.
