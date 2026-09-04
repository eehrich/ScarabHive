# godot plugin

Control Godot 4 on this machine: check and run projects headless, and drive
a running editor — scene tree, node properties, frozen-time playtesting with
injected input, screenshots, the error log.

Self-contained: the tools (`server.py`), the vendored editor addon
(`addon/`), the agents (`agents/`), their prompts and skills all live here.
The concept and the measurements behind it: [docs/konzept.md](docs/konzept.md).

## Quick start

Once per project, editor closed:

```
godot_setup(project="shmup")
```

creates `data/workspace/shmup` if needed, copies the addon into
`addons/godot_mcp`, enables it and runs an import so the addon registers its
runtime autoload. Then open the project in Godot; the addon listens on
`127.0.0.1:6550` from the moment the editor loads it.

```
agent-cli chat --agent gamedev        # the harness: coder + Godot + asset sub-agents
agent-cli chat --agent godot_agent    # Godot only, no harness
```

`godot_status` says whether the binary answers and which project the editor
has open. The binary is set once in `agents/tools.yaml`
(`godot_binary`) — on Windows the `_console.exe`, the plain one returns no
stdout.

## Tools

| Headless — editor may be closed | |
|---|---|
| `godot_status` | Binary version; editor reachable, addon version, open project |
| `godot_setup` | Create a project and/or install + enable the addon; idempotent |
| `godot_import_assets` | Import new files (`--headless --import`) |
| `godot_check` | Parse errors with `file:line` — one script, or every `.gd` outside `addons/` |
| `godot_run` | Play a scene N frames; verdict from the parsed error blocks, **not** the exit code |
| `godot_script` | A SceneTree script against the project: the engine API without the editor |
| `godot_export` | Export with a preset into the output directory |

| Editor — project open in Godot | |
|---|---|
| `godot_scene` | Tree, open, save, reload |
| `godot_node` | Get / find / update / reparent existing nodes |
| `godot_play` | Run frozen, step frames or ms with inputs, step_until, exec in the game, stop |
| `godot_observe` | Screenshot → PNG path, logs since a cursor, live state, stack, editor state |
| `godot_command` | Any of the addon's 88 commands by name |

Screenshots return a **path**; `media_ops_load` puts the picture into the
context when the agent actually wants to look.

## Agents

| | |
|---|---|
| `gamedev` | `type: coder` — inherits the whole coder harness and adds the Godot tools, a Godot-aware tester, `blender_agent` and `image_agent` as sub-agents, its own OKF bundle (`data/okf/gamedev`) and the skills below |
| `godot_agent` | Godot without the harness, for interactive work in an open project |
| `gamedev_tester` | `coder_tester` plus the headless tools; no editor tools by design |

Skills: `gamedev-loop` (always), `godot-conventions`, `asset-pipeline`
(on demand). The image agent lives with its tools in
`src/plugins/image_compose/agents/`.

## What was measured

Against Godot 4.7.2 and the Shmup project in `data/workspace/shmup`:

- A runtime `push_error` leaves the exit code at 0; `--check-only` on a
  parse error exits 1. Hence `godot_run` parses stderr.
- `--headless --import` loads the addon, which writes `autoload/MCPGameBridge`
  into `project.godot` — `godot_setup` needs no editor.
- The addon answers in a headless editor 2.5 s after start (no screenshots
  there: dummy renderer) and in the GUI editor 4.5 s after start.
- End to end in the GUI editor: open scene, tree of 7 nodes, 44 properties
  of `Player`, editor screenshot, run frozen, step 2 frames, game screenshot,
  step 1.5 s holding the `left` action with a position report `(8, 256)`,
  `step_until` hitting its cap, logs with cursor, stop.

## Tests

`tests/test_plugin_godot.py` runs against a stub binary (`godot_stub.py`,
answering the way 4.7.2 was measured to) and a fake addon over websockets;
`tests/test_gamedev_config.py` guards the resolved agent configuration —
every `!`/`+` swap, the hook overrides, the sub-agent list, and that prompts
and skills name only tools that render.

Mutation-proven guards (break the line, watch the test go red, restore):

| Break this | This fails |
|---|---|
| Verdict from exit code instead of errors | a-runtime-error-is-a-failed-run-even-though-godot-exits-0 |
| `dedupe_load_failures` | check-all-reports-the-broken-file-with-its-line-once |
| Script-frame preference in `parse_godot_stderr` | a-push-error-is-located-in-the-script |
| `_MESSAGE_LOCATION` | a-resource-message-keeps-its-own-location |
| `_enable_plugin` before `--import` | setup-creates-a-project…autoload-lands |
| `_resolve_project` / `_resolve_output` confinement | outside-the-root / outside-the-output-directory cases |
| `frozen` default, and `null` meaning frozen | play-run-starts-frozen-by-default / with-frozen-null |
| Reply id check; close codes 4001/4002 → BUSY/STALE; non-JSON → PROTOCOL | foreign-id / close-during-the-command / not-json |
| `status` never raising | status-survives-whatever-the-editor-does |
| Unparsed stderr carried (crash dumps, `printerr`) | unprefixed-stderr-is-carried-not-dropped |
| `frames` default 60 applied in code | run-without-frames-quits-after-60 |
| Export needs a clean run, not just a changed file | export-that-wrote-a-file-but-did-not-finish |
| Setup sees an import timeout; refuses to guess the version | setup-that-times-out / refuses-to-guess-the-engine-version |
| `FAILED <path>` lines become CHECK errors | script-that-fails-without-a-parse-block-is-still-named |
| Screenshot decoded and PNG-checked before the write | bad-image-data-never-touches-the-previous-screenshot |
| Images inside play/command replies spilled to files | screenshots-inside-an-input-sequence-become-files |
| `observe state` sends `paths`/`include`, not `specs` | every-observe-action-sends-what-the-game-bridge-reads |
| `_clip` bounds nested dicts and big strings | clip-bounds-a-dict-with-no-list-to-halve |
| `_enable_plugin` bounded to its section | does-not-mistake-a-later-sections-enabled-key |
| `image_compose._confine`, all three writes, spec BEFORE the render | test_output_sandbox |

Not reachable from pytest: `scripts/check_scripts.gd` itself (the walker's
skip list and its `FAILED`/`CHECKED` lines) — the stub mirrors it, so those
tests measure the stub. It was measured live instead: a broken script gives
`CHECKED 2 FAILED 1`, exit 1; an `@abstract` class still instantiates on
4.7.2, so it is not flagged.

## Review

Two read-only reviewers over the finished plugin found 23 things, all
verified at the code and fixed before the commit. The class that mattered:
**success reported without proof** — an export that timed out but had
changed its file, a setup whose import hung after the autoload landed, a
`_clip` that returned an oversized dict with `truncated: True`, a screenshot
whose garbage base64 became an empty file over the previous frame. And one
**silent wrong answer**: `observe state` sent a parameter the game bridge
never reads and got a plausible reply about other nodes.
