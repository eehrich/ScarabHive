# godot plugin

Control Godot 4 on this machine: check and run projects headless, and drive
a running editor — scene tree, node properties, frozen-time playtesting with
injected input, screenshots, the error log.

Self-contained: the tools (`server.py`), the vendored editor addon
(`addon/`), the agents (`agents/`), their prompts and skills all live here.
Why it is built this way: [docs/konzept.md](docs/konzept.md).

## Setup

**1. Point the plugin at your Godot.** One knob, in `agents/tools.yaml`:

```yaml
godot_binary: "C:/prog/Godot/Godot_v4.7.2-stable_win64_console.exe"
```

On Windows this must be the `_console.exe`. The plain `.exe` detaches and
prints nothing, so every headless tool would report silence. It is not
looked up in `PATH` on purpose: say which Godot this is.

**2. Prepare each project once, with the editor closed:**

```
godot_setup(project="my_game")
```

It creates `data/workspace/my_game` if it does not exist, copies the addon
into `addons/godot_mcp`, enables it, and runs an import so the addon
registers its runtime autoload. Running it again on a prepared project is
harmless; it reports `present`, `updated` or `reinstalled`.

An existing project of your own works the same way: put it under
`data/workspace/` (or repoint `projects_root`) and run setup on it.

**3. Open the project in Godot** for anything that touches the editor. The
addon listens on `127.0.0.1:6550` from the moment the editor loads it.

**4. Talk to it:**

```
agent-cli chat --agent gamedev        # the harness: coder + Godot + asset sub-agents
agent-cli chat --agent godot_agent    # Godot only, no harness
```

`godot_status` answers the two questions you have when something feels
wrong: does the binary run, and which project does the editor have open.

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

## What to expect

Things that surprise people on the first day:

- **"It ran" comes from the errors, not the exit code.** A runtime
  `push_error` leaves Godot's exit code at 0, so `godot_run` reads the
  parsed error blocks instead. A parse error under `godot_check` does exit
  1, and it is reported with `file:line`.
- **The addon cannot create nodes.** It reads, changes and reparents what
  exists. New nodes come from editing the `.tscn` or from a script.
- **`godot_play` starts frozen.** Nothing advances until you `step` frames
  or milliseconds, which is what makes a run reproducible. `freeze: false`
  if you want it live.
- **A freshly opened project sits on the 3D tab.** An editor screenshot
  then shows an empty grid; pass `viewport: "2d"` for a 2D game.
- **The editor is single-client.** One connection per command, one client
  at a time, so a second agent on the same editor gets `BUSY`.
- **Screenshots return a path,** not an image. `media_ops_load` puts the
  picture into the context when the agent actually wants to look.

## When something does not work

| The message | What it means |
|---|---|
| `cannot start the Godot binary ...` | `godot_binary` is wrong or not executable. Give the full path to the console executable. |
| `no Godot editor is listening on 127.0.0.1:6550` | The project is not open in the editor, or it has no addon yet. Open it, or run `godot_setup` first with the editor closed. |
| `BUSY: another client is already connected` | Something else holds the editor: a second agent, or a stale session. Close it, or restart the editor. |
| `STALE: the addon closed the connection as idle` | The addon drops idle sockets after 45 s. Harmless, the next call reconnects. |
| `TIMEOUT: <command> gave no answer` | The editor is busy or a step is longer than the timeout. Raise `long_timeout`, or step in smaller pieces. |
| `PROTOCOL: the reply is not JSON` | Something other than the godot_mcp addon answers on that port. Check `port`. |
| `CAPTURE_FAILED` on a screenshot | The editor runs headless, where the dummy renderer draws nothing. Screenshots need a real editor window. |
| Every headless tool reports empty output on Windows | The plain `Godot.exe` instead of `Godot_..._console.exe`. |

## Configuration

In `agents/tools.yaml`, or wherever the instance is declared:

| Key | Default | |
|---|---|---|
| `godot_binary` | `godot` | The console executable. The one you must set. |
| `projects_root` | `data/workspace` | Every project a tool touches must live below this. |
| `output_directory` | `data/workspace/godot` | Where screenshots and exports land, and nothing may escape it. |
| `host` / `port` | `127.0.0.1` / `6550` | Where the editor addon listens. |
| `timeout` | `60` | Plain editor queries. |
| `long_timeout` | `600` | Subprocess runs, game-time steps, exports, big imports. |

## Tests

`tests/test_plugin_godot.py` runs against a stub binary (`godot_stub.py`,
answering the way 4.7.2 was measured to) and a fake addon over websockets;
`tests/test_gamedev_config.py` guards the resolved agent configuration —
every `!`/`+` swap, the hook overrides, the sub-agent list, and that prompts
and skills name only tools that render.

Every guard in `server.py` was broken on purpose once, to check that a test
goes red for it. Two read-only review rounds ran over the finished plugin
before it was committed.
