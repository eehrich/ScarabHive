# blender plugin

Control a Blender running on this machine: inspect the scene, run bpy, capture
the viewport, export 3D files into the workspace.

Self-contained — the tools (`server.py`), the agent (`agents/`) and its prompt
all live here. Nothing has to be added to `config/agents/` or
`config/mcp_servers.yaml`.

## Quick start

Blender side, once: install the BlenderMCP addon, then in the 3D viewport open
the N-panel → tab **BlenderMCP** → **Start MCP Server** (port 9876).

```
agent-cli chat --agent blender_agent
```

`blender_status` tells you whether the addon answers.

---

# The design decision

The task was "package the blender agent as a plugin including MCP". Three
readings, and the cheapest one is not the best one.

## What existed

An agent in `config/agents/blender_agent.yaml` using `blender.*` — tools from
an **external** MCP server declared in `config/mcp_servers.yaml`. That server
is the `blender-mcp` PyPI package: a stdio process in its own virtualenv under
`external/mcp/`, which translates MCP calls into a TCP socket on `localhost:9876`
where a Blender addon listens.

So the chain was: agent → our MCP client → stdio bridge process → socket → addon.

## The options

| | | |
|---|---|---|
| **A** | Move the YAML and prompt into a plugin directory, keep the bridge | Cheapest. But "including MCP" stays false: the tool surface still comes from a config entry and a process outside the plugin |
| **B** | Speak the addon socket directly from our own plugin | Removes a process and a virtualenv, and lets us choose the tool surface |
| **C** | B, and keep the bridge alongside for the asset marketplaces | B plus a second path to the same Blender |

**B was chosen.** The reasons are measured, not asserted:

**The bridge is a thick wrapper on a thin protocol.** The addon speaks
`{"type": <command>, "params": {...}}` in, one JSON object out. Reproducing it
took ~40 lines, verified against the live Blender before any of this was
written:

```
get_scene_info -> success   Scene 'Scene', 3 objects: Cube, Light, Camera
execute_code   -> success   bpy 5.2.0 LTS
```

**Its tool surface is mostly not Blender.** The bridge exposes **28** tools.
Five are Blender control; the other 23 are asset marketplaces (PolyHaven,
Sketchfab, Hyper3D, Hunyuan3D, PolyPizza) and telemetry. Every one of them
costs schema tokens in every request, whether the task is 3D modelling or not.
And of those five marketplaces, exactly **one** (PolyHaven) is actually enabled
in this Blender — the rest answer "integration is currently disabled".

**It hides capability.** The addon reports what it supports, and two of its
commands were never exposed by the bridge: `get_world_state_snapshot`
(selection, frame range, fps — strictly richer than `get_scene_info`) and
`drain_human_activity`. This plugin's `blender_scene` uses the former.

**It carries baggage we do not want.** A second Python environment, a second
process, and a telemetry package that had to be silenced with
`BLENDER_MCP_DISABLE_TELEMETRY: "true"` because it otherwise sent prompts and
screenshots to a third-party Supabase. This plugin has **no dependencies** —
the standard library covers a TCP socket and JSON.

### What was deliberately not taken from it

The 23 marketplace tools. PolyHaven downloads are reachable through
`blender_execute` when needed.

If you want them as first-class tools, the external `blender` server is still
declared in `config/mcp_servers.yaml` — set `enabled: true` there and add
`blender.*` to the agent's allowlist. Both then run side by side: local plugins
(`server/*`, slash) and external MCP tools (`server.*`, dot) are strictly
separate namespaces, so nothing collides.

It is switched **off** by default now, because nothing referenced `blender.*`
any more once this plugin existed, and an enabled external server is started
for every agent run — the first end-to-end run still logged
`BlenderMCPServer … shut down`, a whole process spawned for nobody.

---

## The tool surface

Six, against the bridge's 28. Anything missing is reachable through
`blender_execute`, which is the whole of bpy.

| Tool | |
|---|---|
| `blender_status` | Reachable? addon version, capabilities, which asset providers are on |
| `blender_scene` | Objects, selection, frame range, fps |
| `blender_object` | One object in detail |
| `blender_execute` | Python inside Blender — the escape hatch |
| `blender_screenshot` | Viewport → PNG in the output directory, returns the path |
| `blender_export` | Scene or selection → glb/gltf/fbx/obj/stl/ply/usd/abc/blend |

### Why `blender_export` exists rather than "just write bpy"

Because the export API is a minefield that fails **silently**. Measured against
Blender 5.2.0 LTS:

- glTF and FBX still live in the old `bpy.ops.export_scene.*` namespace;
  OBJ, STL, PLY, USD and Alembic moved to `bpy.ops.wm.*_export`.
- "Only the selected objects" is spelled **five different ways**:
  `use_selection` (gltf, fbx), `export_selected_objects` (obj, stl, ply),
  `selected_objects_only` (usd), `selected` (abc), and nothing at all for
  `.blend`, which is the whole session.

Get the keyword wrong and nothing raises — Blender exports the entire scene and
reports success. The mistake surfaces much later, as a file that is
suspiciously large. The mapping lives in `_EXPORTERS` and a parametrised test
holds every row of it.

### Screenshots return a path, not a picture

`media_ops_load` is what puts an image into the model's context. Returning it
from the tool would push a picture into every call whether the agent needed to
look or not.

## Design notes

**One connection per call.** The bridge keeps a persistent socket and documents
what that costs: two commands overlapping on one wire desync the response
stream until a timeout fires. An agent makes occasional calls, so reconnecting
each time is free next to being immune to that class. Nothing here holds state
between calls.

**Blender is the only writer.** Screenshots and exports are produced by
Blender's own process — this plugin hands it an absolute path. Those paths are
confined to `output_directory` (default `data/workspace/blender`), because
Blender writes wherever it is told and a model-chosen filename eventually
picks a bad one. `../` escapes are rejected; a subdirectory below the root is
allowed.

**A remote Blender is possible but asymmetric.** Point `host` elsewhere and the
tools work — but exports and screenshots are then written on *that* machine,
and this plugin's confinement check runs against a local path that means
nothing there.

## Configuration

Everything has a default in `schema.yaml`; override per instance:

| Key | Default | |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `9876` | Where the addon listens |
| `timeout` | 60 | Plain queries |
| `long_timeout` | 600 | execute / screenshot / export — a render runs for minutes |
| `output_directory` | `data/workspace/blender` | Everything Blender writes for us, and nothing may escape it |

## Tests

`tests/test_plugin_blender.py` runs against a **fake addon socket**, so the
suite needs no Blender. That is what makes it able to assert things a live run
cannot show reliably — which operator an export actually sent, that a path
cannot escape, that an unreachable Blender produces an instruction.

Each guard was mutation-proven: break the production line, watch the right test
go red, restore.

| Break this | This fails |
|---|---|
| USD's selection keyword → `use_selection` | export-sends-the-right-operator-and-selection-flag[usd] |
| glb/gltf `export_format` swapped | gltf-variants-are-told-apart-by-export-format |
| The path confinement check | export/screenshot-cannot-escape + absolute-paths-are-rejected (8 cases) |
| `get_object_info` argument → `object_name` | object-uses-the-addon-parameter-name |
| The "Start MCP Server" instruction | unreachable-blender-says-what-to-click |
| The `result["error"]` check in `_call` | error-inside-a-success-envelope + stale-file |
| The freshness comparison | screenshot-that-writes-nothing-is-caught |
| `save_as_mainfile`'s `copy=True` | blend-export-does-not-hijack-the-users-session |
| The status-line cap | long-inputs-do-not-blow-the-status-row |

## What the review found

Two defects were only visible from the addon's source, not from a green run:

**The addon has two failure shapes, and the second one looks like success.**
`addon.py` wraps *whatever a handler returns* in `{"status": "success",
"result": …}` and reports `status: error` only when the handler **raised**.
Several handlers do not raise — `get_viewport_screenshot` with no 3D viewport
open returns `{"error": "No 3D viewport found"}`, and `get_world_state_snapshot`
catches and returns the same shape. Checking the envelope alone therefore
reported a failed screenshot as a good one. `_call` now unwraps both shapes at
the single point every tool passes through.

**`exists()` is not proof that this call wrote the file.** Combined with the
above, a second screenshot that failed while an older file of the same name sat
on disk reported success *with the stale image* — and the agent's whole loop is
look-change-look, so it would have reasoned about a pre-edit frame with
everything green. Screenshot and export now compare (mtime, size) against what
was there before.

The `.blend` session hijack (`copy=True`) was the third; it is described at
`_EXPORTERS` in `server.py`.

Two smaller ones: `blender_screenshot` forced PNG bytes into whatever suffix
the model chose, and `media_ops` keys its MIME type off that suffix — the name
is now normalised to `.png`. And the agent's `tools.allowed` was a bare list,
which **replaces** the inherited one, so `context_engineer/*` was dropped while
its hook stayed enabled: the hook would evict a large scene dump and then tell
the agent to fetch it back with a tool it did not have. Every entry now carries
`+`.

The live run against Blender 5.2.0 LTS is what found the last one: upstream's
*tool* calls the argument `object_name`, but the addon *handler* is
`get_object_info(name)`, and sending the former raises a TypeError inside
Blender.
