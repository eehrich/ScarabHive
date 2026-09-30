# godot

Lets agents build and test games in Godot 4 on this machine, over two channels: the Godot binary as a headless
process (create a project, parse-check scripts, play a scene for N frames, run a GDScript against the project,
export), and a running editor through the vendored `godot_mcp` addon (scene tree, node properties, playing with a
frozen clock, injected input, screenshots, the error log). Why it is built this way: `docs/konzept.md`.

- **Tools** -- `godot_status`, `godot_setup`, `godot_import_assets`, `godot_check`, `godot_run`, `godot_script`,
  `godot_export` (headless); `godot_scene`, `godot_node`, `godot_play`, `godot_observe`, `godot_command` (editor).
- **Agents** -- `gamedev` (the coder harness plus Godot, a Godot tester and Blender/image sub-agents via
  `gamedev_sam`), `godot_agent` (Godot without the harness), `gamedev_tester`; skills `gamedev-loop`,
  `godot-conventions`, `asset-pipeline`.
- **Hooks / panel** -- none.

Configured in the plugin's own `agents/tools.yaml`: set `godot_binary` to the Godot executable (on Windows the
`_console.exe`), run `godot_setup` on a project with the editor closed, then open it in Godot for the editor tools. An
agent gets the tools with `+godot/*`.

The full manual -- getting a project ready, the agents, every tool and its answers, paths and processes, the settings
and the known gaps -- is the plugin's guide, `godot.guide`, in the Help panel.
