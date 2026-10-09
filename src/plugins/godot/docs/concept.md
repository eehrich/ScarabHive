# Godot plugin and gamedev agent — concept

As of 2026-09-04. The decisions here are measured, not guessed; where a
number appears, next to it is how it came about.

## What was built

Three things, in two plugin directories:

| | Where | What |
|---|---|---|
| **Plugin `godot`** | `src/plugins/godot/` | Twelve tools over two channels: the Godot binary headless, and a running editor via the bundled `godot_mcp` addon |
| **Agent `gamedev`** | `src/plugins/godot/agents/` | Inherits the coder harness (`type: coder`), gets the Godot tools, a Godot tester, Blender and the image agent as sub-agents, its own skills and its own knowledge bundle |
| **Agent `image_agent`** | `src/plugins/image_compose/agents/` | Textures, sprites, UI as PNG of exact size: ComfyUI generates, `image_compose` composes, the agent looks at the result |

In addition `godot_agent` for Godot work without the harness, the counterpart to
`blender_agent`.

## The decision: ready-made MCP server or a plugin of our own

Every "Godot MCP" consists of two parts. An **addon** in GDScript lives
in the editor and does the work: scene tree, nodes, starting the game, input,
screenshots, log. A **server** next to it translates MCP into the addon's
protocol. The addon is indispensable, the server is a translator.

This is the same situation as with the Blender plugin, and the answer is the same:
take over the addon, replace the server with a plugin of our own. Reasons:

- **The protocol is three fields.** `{id, command, params}` as one
  WebSocket text frame, back `{id, status, result|error}`. Measured against
  a headless-started editor: port open after 2.5 s, handshake,
  scene tree, error envelope, reconnect after a clean close.
- **No Node process per agent run.** The upstream server is Node 20 and
  is spawned on every start. `websockets` is already in the venv as a
  direct dependency.
- **The ready-made server lacks the headless channel.** Parse check, run without
  editor, export, create project: none of the servers can do that, because they speak
  only to the addon. For an agent that is half the work.
- **Twelve tools instead of 21 times 88 actions**, plus `godot_command` as an
  escape hatch for the rest.

### Which addon

Two candidates remained after the research.

| | satelliteoflove/godot-mcp | hybridindie/godot-mcp |
|---|---|---|
| Direction | Addon **listens** on 127.0.0.1:6550, we dial per call | Addon **dials out** to ws://9080, our plugin would have to listen process-wide |
| Playtest | Frozen time: `freeze`, `step` N frames with inputs, `step_until` predicate | Input simulation, no time control |
| Godot | 4.5+, addon 4.1.11, MIT | 4.4+, MIT |

The direction decides: the API process and `agent-cli` cannot both bind
port 9080. With a listening addon we connect per call, as with
Blender, with no state in the plugin. The frozen-time control is the
second reason: a game that runs freely between two tool calls is somewhere else
at the next look. `godot_play run` therefore starts frozen.

The addon is pinned under `addon/godot_mcp` (commit in
`addon/VENDORED.md`). No submodule: it would drag in the Node server, tests and docs,
and the addon has to be copied into every game project anyway.
`godot_setup` does that.

### What neither addon can do

No addon runs GDScript **in the editor**; satelliteoflove can do it only in the
running game (`exec_run`). And neither creates **nodes**, they only read,
change, reparent. The headless channel covers both: `godot_script` runs a
SceneTree script with the whole engine API against the project, and new nodes
are a text change to the `.tscn`. The editor re-reads the file after
`godot_scene reload`. This rule is in the skill, not in the code.

## The headless channel, measured

With Godot 4.7.2 on this machine:

| Call | Result |
|---|---|
| `--headless --check-only -s kaputt.gd` | Exit 1, `SCRIPT ERROR: Parse Error … (kaputt.gd:3)` on stderr |
| SceneTree script with `quit(3)` | Exit 3, stdout and stderr cleanly separated |
| Scene with `push_error` in `_ready`, `--quit-after 5` | **Exit 0**, error block on stderr |
| `--headless --import` in a project with the addon enabled | Addon loads, writes `autoload/MCPGameBridge` into `project.godot` |

The third row is the one that shapes the design: `godot_run` does not trust the
exit code. It parses stderr into blocks (`ERROR:`, `SCRIPT ERROR:`,
`WARNING:` with `at:` and backtrace) and takes the first script frame as the
location, not the C++ file that `push_error` always reports as `at:`.
A parse error produces two blocks, the second (`Failed to load script`)
is deduplicated so that one error counts as one.

The fourth row makes `godot_setup` fully possible without an editor: create
project, copy addon, register in `[editor_plugins]`, `--import`. The
test pins the order, because an import before activation
registers nothing and still reports success.

`godot_check` without a script argument runs `scripts/check_scripts.gd`, which loads every
`.gd` below `res://` (without `addons/` and `.godot/`). `load()` does **not** return
`null` for a broken script, measured; the counter checks
`can_instantiate()`. An `@abstract` script passes this check on 4.7.2
(measured, `can_instantiate() == true`), so it is not wrongly reported.
If a script fails without a `SCRIPT ERROR` block, the walker prints
`FAILED res://x.gd`, and the plugin turns that into an error with the file.

Everything on stderr that is not a block is kept (`stderr` in the result):
a crashed game writes its backtrace without an `ERROR:` prefix, and
`printerr()` likewise. Without that, a crash would be "exit -1073741819, 0 errors" and
nothing else.

## The gamedev agent

`type: coder` inherits sandbox, shell, model chain, explorer, reviewer and 300
steps. The file names only what a game changes:

- `+godot/*`, `+media_ops/*` added.
- `!coder_sam/*` → `+gamedev_sam/*` with `coder_explorer`, `coder_reviewer`,
  `gamedev_tester`, `blender_agent`, `image_agent`.
- `!coder_okf/*` → `+gamedev_okf/*`, bundle `data/okf/gamedev`. Godot facts
  do not belong in the coder's Python conventions.
- Skills: `gamedev-loop` always; `godot-conventions`, `asset-pipeline` and
  the coder skills on demand. The prompt is tools and limits, the
  knowledge lives in the skills.

**Blender and the image agent are sub-agents, not tools.** Their loop
is look, change, look; every step carries an image. Those belong in
their own context. What comes back is a path under `data/workspace/`, which the
gamedev agent moves into the project, imports (`godot_import_assets`) and
references. The `asset-pipeline` skill contains the briefing tables: what
a model assignment must state (size in meters, polygon budget, origin,
glb), what an image assignment must state (pixels, tileable, background).

**The tester** is `coder_tester` plus the headless tools, without the
editor tools: two drivers on one editor would be one too many.

The acceptance lines in the report: `Ran:` / `Played:` / `Check:` / `Reviewed:`.
`Played` is the one that is new for a game: played in the editor, looked at, or
a justification why headless was enough.

## The image agent

`image_agent` is a cover artist: ComfyUI
(`juggernaut_xl`, `sdxl_txt2img` on the GPU server) generates, `images_render`
composes onto the exact canvas, `media_ops_load` looks. The skill
`image-assets` holds the rules that a generated image does not meet by itself:
exact pixels (compose, don't hope), tileability (render 2×2
and look at the seam), and that alpha is **made**: a generated image is opaque, the
cutout comes from the composition step
(`cutout: true` on the image layer, rembg with `isnet-general-use`, for
pixel art plus `alpha_threshold`). Measured on 2026-09-04: a painted
figure with a boat comes out cleanly (1.4 % soft pixels, 0.9 s on the CPU),
`u2net` leaves the boat half transparent; an 80×110-pixel sprite gets
a dark halo, which the threshold removes. Thin structures
(whiskers) are lost, and the report says so.

`images` is an `image_compose` instance of its own with the new option
`output_directories`: all three write paths of a render (composite,
layer directory, spec) must lie under `data/workspace/images`. Left empty,
the option stays unrestricted, which the cover pipeline depends on; a test
checks both directions.

## End to end, measured

Against the copy of the "Classic Shmup" in `data/workspace/shmup` (Godot 4.7.2):

| Step | Result |
|---|---|
| `godot_setup` | Addon 4.1.11 installed, plugin enabled, autoload registered, import without errors |
| `godot_check` | 9 scripts, no parse errors; 16 warnings about invalid UIDs, because the copy was made without `.import` files |
| `godot_run` 60 frames | Verdict `ok`, 0 errors |
| Editor channel | Port open 4.5 s after GUI start; `main.tscn` opened, tree 7 nodes, `Player` 44 properties, editor screenshot 900×378 |
| `godot_play` | `run` frozen; `step frames=2`; `step 1500 ms` with action `left` 1200 ms, report `Player.position = (8, 256)`; `exec` reads the same position; `step_until x < 0` runs to the limit of 3000 ms, `predicate_met: false` (the player is clamped at x=8, correct) |
| `godot_observe` | Game screenshots 900×1200 before and after the input, the ship stands at the left edge afterwards; log with cursor 17 |

Two things were only noticed here and are fixed: Godot's leak message
"resources still in use at exit" counted as an error in `godot_script`
(now a field of its own, `exit_leaks`), and the addon puts the text of an
engine error into the field `type`, not `message` (the log line reads both).

## Review round

Two reviewers, read-only, over the finished state. 23 findings, all verified
against the code and built; none fell. The class that counted: **success without
evidence**. Export reported success as soon as the target file had changed,
even after a timeout or exit 1 (Godot writes the PCK incrementally). Setup
never read the import timeout. `_clip` returned a dict without lists unshortened
and claimed `truncated: True`. Base64 garbage was written before the check and
thereby deleted the previous screenshot. `ConnectionClosed` escaped
raw, and `godot_status`, the tool for exactly this case, threw. In
image_compose, `spec_path` was confined only after rendering.

In addition a **silent wrong answer**: `observe state` sent `specs`, which
`get_runtime_state` in the addon never reads; the answer was a plausible
package about other nodes. The fake addon in the test now answers every command
with an echo, so that a renamed or wrongly parameterized command
shows immediately.

The docstring claimed that connecting per call makes it immune to the addon's
45-s idle lock. Wrong: the clock counts from the last *incoming* packet,
while a command is running. The game-side commands cap themselves at 28–30 s;
`rescan_filesystem` (60 s) can run beyond that and then arrives as `STALE`, not as a raw
disconnect.

All new bolts are mutation-checked (31 mutations, all red). One
mutation stayed green at first, because the stub hung at the import *before* the
autoload entry and the setup already failed because of that; the stub now hangs
after it, like the real import on a large project.

## Open

- **Cutout on the GPU.** rembg on the CPU is enough for sprites, and
  for thin structures BiRefNet is ready as a model name
  (`birefnet-general-lite`, measured 14 s instead of 2.6 s, keeps the
  whiskers). It would get onto the GPU only with `rembg[gpu]` on the ComfyUI host;
  generating with real alpha (LayerDiffuse) remains a node of its own.
- **Editor screenshot headless.** Without a window the dummy renderer returns
  `CAPTURE_FAILED`; the error arrives cleanly, only the image is missing. For
  CI runs `godot_run` is the way.
- **Projects outside the sandbox.** The sandbox is `data/workspace`; moving
  is a single knob (`projects_root` plus the file instances), no
  change to the code.
