---
name: godot-conventions
description: Godot 4 facts a model gets wrong from memory — the .tscn text format, res:// and uid paths, imports, autoloads, the input map, signals, the node classes that matter, and the errors that mean a wrong node path. Use when writing or reading scenes, scripts or project.godot.
metadata:
  version: '1.0.0'
---

# Files

| | |
|---|---|
| `project.godot` | INI-like. `[application]` main scene, `[autoload]`, `[input]`, `[display]`, `[editor_plugins]` |
| `.tscn` / `.tres` | Text scenes / resources. Edit them; do not invent a binary |
| `.gd.uid` | Godot ≥ 4.4 writes one per script. Keep them with the script; never hand-write |
| `.import` | Per imported asset, written by the import step. Never hand-write |
| `.godot/` | Import cache. Never edit, never commit |

# .tscn essentials

```
[gd_scene load_steps=3 format=3 uid="uid://c4x..."]

[ext_resource type="Script" path="res://player.gd" id="1_abc"]
[ext_resource type="Texture2D" path="res://assets/ship.png" id="2_def"]
[ext_resource type="PackedScene" path="res://bullet.tscn" id="3_ghi"]

[sub_resource type="RectangleShape2D" id="RectangleShape2D_1"]
size = Vector2(32, 48)

[node name="Player" type="CharacterBody2D"]
script = ExtResource("1_abc")
speed = 300.0

[node name="Sprite2D" type="Sprite2D" parent="."]
texture = ExtResource("2_def")

[node name="Shape" type="CollisionShape2D" parent="."]
shape = SubResource("RectangleShape2D_1")

[node name="Gun" parent="." instance=ExtResource("3_ghi")]
position = Vector2(0, -20)

[connection signal="body_entered" from="." to="." method="_on_body_entered"]
```

- `load_steps` = ext + sub resources + 1; wrong is harmless, missing ids are not.
- `uid="..."` on the header is optional; leave the existing one, omit on a new file (the editor adds it).
- A `parent="."` node is a child of the root; deeper: `parent="Enemies/Left"`.
- Instancing another scene: `instance=ExtResource(...)`, no `type`.
- Exported script vars appear as plain `name = value` on the node.
- Values: `Vector2(1, 2)`, `Color(1, 0, 0, 1)`, `true`, `"text"`, `NodePath("../Player")`.

# Paths

`res://` is the project root. `user://` is per-user save data. Absolute OS
paths do not exist inside a running game. `uid://` references survive
renames; `res://` do not — prefer editing in the editor for moves.

# Adding an asset

Copy into `res://assets/...` → `godot_import_assets` → reference it. A file
that was never imported loads as null and the error says
`Cannot open file`, not "not imported".

**Pixel art: set the filter once, for the project.** Nearest belongs in
`project.godot`, not on every node:

```
[rendering]
textures/canvas_textures/default_texture_filter=0
```

The per-node key is `texture_filter` and **its numbers are different**:
node `0` = Inherit, `1` = Nearest, `2` = Linear; project `0` = Nearest,
`1` = Linear (the default). Setting it per node also has to be repeated for
every sprite ever added — a blur that comes back with the next asset. The
project default only reaches nodes still on Inherit, so a sprite an earlier
session pinned to Linear stays blurry until that line goes.

Editing `project.godot` while the editor is open is a race: the editor
holds the settings in memory and writes them back. Edit with it closed, or
follow with `godot_command restart_editor`.

# Wiring a spritesheet, tileset or 9-patch panel

These take one PNG from the image agent (brief in `asset-pipeline`) and
turn it into the node that uses it. The number that matters is always the
**frame/tile/margin size**, and it must be the size that was briefed, not
a size guessed from the sheet's total dimensions.

**Spritesheet  `AnimatedSprite2D`.** Import the sheet as a texture, then
cut it into an `AtlasTexture` per frame and collect them in a
`SpriteFrames` resource:

```
[sub_resource type="AtlasTexture" id="AtlasTexture_f0"]
atlas = ExtResource("1_sheet")
region = Rect2(0, 0, 64, 64)

[sub_resource type="AtlasTexture" id="AtlasTexture_f1"]
atlas = ExtResource("1_sheet")
region = Rect2(64, 0, 64, 64)

[sub_resource type="SpriteFrames" id="SpriteFrames_walk"]
animations = [{
"name": &"walk",
"speed": 8.0,
"loop": true,
"frames": [{"texture": SubResource("AtlasTexture_f0"), "duration": 1.0},
           {"texture": SubResource("AtlasTexture_f1"), "duration": 1.0}]
}]

[node name="Sprite" type="AnimatedSprite2D" parent="."]
sprite_frames = SubResource("SpriteFrames_walk")
autoplay = "walk"
```

`region`'s width/height is the frame size from the brief; its x is
`column * frame_width`. Building this by hand for more than a few frames
is what `godot_script` is for — loop `frame_count`, `region.position.x =
i * frame_width`. Reload, play, screenshot: a frame_size off by even a
few pixels shows as the sheet's seam cutting a character in half.

**Tileset  `TileSet` / `TileMapLayer`.** The atlas source needs the same
tile size and separation that were briefed, or the wrong texels end up in
each tile:

```
[sub_resource type="TileSetAtlasSource" id="TileSetAtlasSource_1"]
texture = ExtResource("1_tiles")
texture_region_size = Vector2i(32, 32)
separation = Vector2i(2, 2)
0:0/0 = 0
1:0/0 = 0
2:0/0 = 0

[sub_resource type="TileSet" id="TileSet_1"]
tile_size = Vector2i(32, 32)
sources/0 = SubResource("TileSetAtlasSource_1")

[node name="TileMapLayer" type="TileMapLayer" parent="."]
tile_set = SubResource("TileSet_1")
```

`texture_region_size` is the tile size, `separation` is the gap briefed
between tiles — both must match the sheet exactly, or the importer reads
from between two tiles. `X:Y/0 = 0` registers tile atlas-coordinate
`(X, Y)` as usable (alternative id 0); every tile the brief listed needs
one line. The editor's click-drag tile painter is not reachable through
the addon, but setting cells is: `godot_command command="set_cell"` with
the layer, coordinates, source id and atlas coordinates against an open
project, or the same call (`TileMapLayer.set_cell`) in a `godot_script`
when the editor is closed or the number of cells makes a loop the
shorter path.

**9-patch  `NinePatchRect`.** The margin is the border thickness from the
brief, on all four sides unless asked otherwise:

```
[node name="Panel" type="NinePatchRect" parent="."]
texture = ExtResource("1_panel")
size = Vector2(300, 120)
patch_margin_left = 16
patch_margin_top = 16
patch_margin_right = 16
patch_margin_bottom = 16
```

The margins cut the fixed corners out of the source texture; `size` is
the rect's on-screen size, independent of the texture's own size — that
is the point of a 9-patch, it can be larger than what was generated. If a
corner looks stretched, the margin is smaller than the border actually
drawn in the PNG; check the sheet, do not just change the number.

# Scripts

```gdscript
extends CharacterBody2D
class_name Player

signal died
@export var speed: float = 300.0
@onready var sprite: Sprite2D = $Sprite2D

func _ready() -> void: ...
func _physics_process(delta: float) -> void:
    velocity = Input.get_vector("move_left", "move_right", "move_up", "move_down") * speed
    move_and_slide()
```

- Physics and movement in `_physics_process`; drawing and input polling in `_process`.
- `$Path` is `get_node("Path")`, relative to this node. `%Name` needs the node marked unique.
- Actions used in code (`move_left`) must exist in `project.godot [input]`; a missing one is a runtime error on first use, not at parse.
- Connect signals in code (`died.connect(_on_died)`) or in the `.tscn` `[connection]` block; not both.
- `await get_tree().create_timer(1.0).timeout` — a coroutine; `_ready` may await, `_process` should not.
- Typed GDScript (`: float`, `-> void`) catches half the errors at parse time. Use it.

# Node classes that matter

| 2D | 3D | Both |
|---|---|---|
| `Node2D`, `Sprite2D`, `AnimatedSprite2D` | `Node3D`, `MeshInstance3D`, `Camera3D`, `DirectionalLight3D` | `Node`, `Timer`, `AudioStreamPlayer` |
| `CharacterBody2D`, `RigidBody2D`, `Area2D`, `StaticBody2D` + `CollisionShape2D` | `CharacterBody3D`, `RigidBody3D`, `Area3D` + `CollisionShape3D` | `AnimationPlayer`, `CanvasLayer` (UI) |
| `Camera2D`, `TileMapLayer`, `ParallaxBackground` | `GridMap`, `NavigationRegion3D`, `WorldEnvironment` | `Control`, `Label`, `Button` |

A body without a `CollisionShape*` child collides with nothing and reports
nothing.

# The errors, decoded

| Error | Means |
|---|---|
| `Node not found: "X"` | Wrong path from *this* node, or the node is not in the tree yet (`@onready`) |
| `Invalid get index 'y' (on base: 'Nil')` | A `$Path`/`get_node` returned null earlier and nobody checked |
| `Cannot open file 'res://...'` | Wrong path, or not imported |
| `Parse Error: ... after "="` | Read the line; a typed var without a value |
| `Identifier "X" not declared` | Class name typo, or `class_name` missing on the script |
| `Attempt to call function 'move_and_slide' on a null instance` | The script is not on a body node |

# Project layout that scales

```
res://
  project.godot
  main.tscn  main.gd
  scenes/    (one folder per scene: player/player.tscn + player.gd)
  assets/    models/ textures/ sprites/ audio/
  addons/    (third-party; never edit)
```

Small scenes, instanced. A scene owns its script. Global state is an
autoload (`[autoload] Game="res://game.gd"`), reachable as `Game.score`.
