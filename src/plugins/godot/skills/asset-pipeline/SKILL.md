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

For pixel art, the import filter matters: after the first import, set
`texture filter` to Nearest on the node (`texture_filter = 1` in the
`.tscn`), or the sprite blurs.

# Sprites need transparency?

They get it in the compose step, not from the generator and not from a
shader: the image agent renders the object on a plain background and sets
`cutout: true` on the image layer. Brief it that way and check the PNG has
alpha where you expect it — thin parts (antennae, rope, whiskers) can be
lost, and the agent's report names what it lost.

# Check before you report

- The file is in `res://assets/...` and `godot_import_assets` ran clean.
- The scene references it and `godot_check` + `godot_run` are clean.
- You looked at it in a screenshot, and the size is the size you asked for.
