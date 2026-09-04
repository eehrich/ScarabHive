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
