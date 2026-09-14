---
name: asset-pipeline
description: How the gamedev agent gets a 3D model from the Blender sub-agent or a texture/sprite from the image sub-agent, lands the file in the Godot project, imports it and references it from a scene. Use whenever a task needs an asset that does not exist yet.
metadata:
  version: '1.0.0'
---

# The shape of it

Brief → sub-agent works in its own context → returns a **path** under
`data/workspace/` → you move it into `res://assets/...` →
`godot_import_assets` → reference it → look at it in the game.

Their output directories are drop boxes, not archives: `move`, do not
copy (`coder_fs_manage` has no copy anyway).

You place; they produce. Never ask a sub-agent to touch the project.

# Briefing `blender_agent`

Say all of these; the model cannot ask you back:

| | Example |
|---|---|
| What, in one sentence | "a low-poly boulder for a 2D top-down shooter's background" |
| Size in metres | "about 1.5 m across" — Godot: 1 unit = 1 m |
| Poly budget | "under 500 triangles" / "detailed, up to 10k" |
| Origin | "at the base" (things that stand) / "centred" (things that float) |
| Style | "flat-shaded, three grey tones" / "PBR, rough granite" |
| Export | "export glb, selected_only, filename boulder.glb" |

The exporter converts Blender's Z-up to Godot's Y-up; do not ask for a
rotation. Ask for **applied transforms** (scale 1, rotation 0) so the size
you asked for is the size that arrives.

Reply comes with `data/workspace/blender/boulder.glb`. Then:

```
coder_fs_manage(operation="move", path="data/workspace/blender/boulder.glb",
                destination="data/workspace/<project>/assets/models/boulder.glb")
godot_import_assets(project="<project>")
```

A `.glb` imports as a **scene**. Instance it:

```
[ext_resource type="PackedScene" path="res://assets/models/boulder.glb" id="4_rock"]
[node name="Boulder" parent="." instance=ExtResource("4_rock")]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 3, 0, -2)
```

or in `godot_script`: `load("res://assets/models/boulder.glb").instantiate()`,
`add_child`, set `owner`, pack, save. Then `godot_scene reload`, play a
frame, screenshot, look: scale and orientation are the two things that go
wrong, and both are visible.

# Briefing `image_agent`

| | Example |
|---|---|
| What and where it is used | "ground texture for a top-down desert level" / "the player's ship sprite, seen from above" / "a pause-menu button" |
| Exact size in pixels | "1024×1024" / "64×64" — powers of two for textures |
| Tileable? | "seamless, tiles in both axes" — say it or it will not be |
| Background | "opaque" / "transparent" — a sprite gets a real cutout (rembg in the compose step); ask for pixel-art hard edges if the sprite is pixel art |
| Style | "pixel art, 4 colours" / "painterly, muted" / "clean flat UI, dark theme" |
| Text on it? | Exact string, or "no text" |

Reply comes with `data/workspace/images/<name>.png` and what was verified.
Then move into `assets/textures/` or `assets/sprites/`, import, reference:

```
[ext_resource type="Texture2D" path="res://assets/sprites/ship.png" id="2_ship"]
[node name="Sprite2D" type="Sprite2D" parent="."]
texture = ExtResource("2_ship")
```

For pixel art the filter matters, and it belongs in `project.godot` once
(`[rendering] textures/canvas_textures/default_texture_filter=0`), not on
each node — details and the shifted enums in `godot-conventions`. Per node
it has to be repeated for every sprite ever added.

# Sprites need transparency?

They get it in the compose step, not from the generator and not from a
shader: the image agent renders the object on a plain background and sets
`cutout: true` on the image layer. Brief it that way and check the PNG has
alpha where you expect it — thin parts (antennae, rope, whiskers) can be
lost, and the agent's report names what it lost.

# Briefing `image_agent` for a spritesheet

The agent still delivers **one PNG**; it is a sheet, not a folder of
frames. Brief the layout, not just the subject — it decides internally
whether to generate each frame and compose them onto one canvas, or
generate the sheet directly:

| | Example |
|---|---|
| Subject and the animation | \"a knight walking, side view, 6 frames\" |
| Frame size in pixels | \"64×64 per frame\" |
| Grid | \"6 columns × 1 row\" — say rows too; do not make it infer a square |
| Consistency | \"same character, same palette, same lighting across all frames\" — a fixed seed with a per-frame prompt change is how it holds |
| Background | almost always transparent for a character sheet |

Reply is one PNG, e.g. `data/workspace/images/knight_walk.png`, 384×64 for
the example above. Move it, import it, then wire frames as
`AtlasTexture` regions into a `SpriteFrames` resource — recipe in
`godot-conventions`. The frame size you asked for is the region size you
cut; if they do not match, the sheet was not built to the brief and goes
back for another pass, not for you to reverse-engineer the actual grid.

# Briefing `image_agent` for a tileset

A tileset is one sheet of same-size tiles with **gaps between them** —
without the gap, Godot's atlas importer bleeds one tile's edge into its
neighbour's texel when filtering is on.

| | Example |
|---|---|
| Tiles needed | \"grass, dirt, water, grass-to-dirt edge — 4 tiles\" |
| Tile size in pixels | \"32×32\", power of two |
| Separation | \"2 px transparent gap between tiles\" — always ask for this, it is not the default anyone generates unprompted |
| Style | consistent across tiles: \"same style and lighting as the grass tile\" once one is approved |

Reply is one sheet, e.g. 4 tiles at 32×32 with 2 px gaps  a 134×32 PNG (4
× 32 + 3 × 2 gaps = 128 + 6). State the exact expected size in the brief
so a wrong layout is visible immediately, do not compute it after the
fact. Wire it as a `TileSetAtlasSource` with `texture_region_size` and
`separation` — recipe in `godot-conventions`.

# Briefing `image_agent` for a 9-patch UI panel

A `NinePatchRect` stretches the middle and keeps the corners fixed size —
brief the corner size explicitly, because that number becomes
`patch_margin_*` in the scene and has to match what was actually drawn:

| | Example |
|---|---|
| What | \"a stone dialogue-box panel with a carved border\" |
| Exact size in pixels | \"96×96\" — generous enough that the corner detail reads |
| Corner / border thickness | \"16 px uniform border on all four sides\" — this becomes the patch margin, pick it before asking |
| Center | \"flat, tileable when stretched\" — the middle gets stretched, not tiled, so it must not have a visible pattern the stretch would smear |
| Background | opaque unless the panel floats over game content, then transparent outside the border shape |

Reply is one PNG at the exact size. `patch_margin_left/top/right/bottom`
in the scene must equal the border thickness you briefed — recipe in
`godot-conventions`.

# Check before you report

- The file is in `res://assets/...` and `godot_import_assets` ran clean.
- The scene references it and `godot_check` + `godot_run` are clean.
- You looked at it in a screenshot, and the size is the size you asked for.
