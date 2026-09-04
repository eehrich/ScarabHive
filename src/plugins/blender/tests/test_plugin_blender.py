"""Guards for the blender plugin.

Everything here runs against a FAKE addon socket, so the suite needs no
Blender. What that buys is the ability to assert the things a live run cannot
show you reliably: which operator an export actually sends, that a path cannot
escape the output directory, and that an unreachable Blender produces an
instruction rather than a stack trace.

The export mapping is the centrepiece. Blender spells "only the selected
objects" five different ways across its exporters, and gltf/fbx still live in
the old ``export_scene`` namespace while everything else moved to ``wm.*``.
Getting one wrong does not raise — it writes the whole scene, reports success,
and the mistake surfaces much later as a suspiciously large file.
"""
from __future__ import annotations

import ast
import json
import socket
import threading
from pathlib import Path

import pytest

from agent_system.config.models import AgentSystemConfig, MCPConfig
from agent_system.mcp.status import StatusPhase, get_status_bus
from plugins.blender.plugin import PLUGIN_FACTORY


class FakeAddon:
    """A socket that answers like the BlenderMCP addon, and records the calls."""

    def __init__(self, out_dir: Path | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.out_dir = out_dir
        #: Make a handler answer the way the REAL addon answers a failure it
        #: did not raise on: {"status": "success", "result": {"error": ...}}.
        #: Without this the fake can only produce an envelope-level error --
        #: the one shape the plugin never got wrong -- so the defect that
        #: shape hides stays invisible to a green suite.
        self.soft_errors: dict[str, str] = {}
        #: The real addon returns BEFORE writing on every screenshot error
        #: path. A fake that always writes makes the stale-file case
        #: unreachable by construction.
        self.write_screenshots = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _reply(self, command: str, params: dict) -> dict:
        if command in self.soft_errors:
            # NOT the __error__ sentinel: this has to come back inside a
            # SUCCESS envelope, exactly the way addon.py does it.
            return {"error": self.soft_errors[command]}
        if command == "get_addon_info":
            return {"name": "Blender MCP", "addon_version": [1, 6],
                    "protocol_version": 5,
                    "capabilities": ["execute_code", "get_scene_info"]}
        if command.startswith("get_") and command.endswith("_status"):
            return {"enabled": command == "get_polyhaven_status"}
        if command == "get_world_state_snapshot":
            return {"name": "Scene", "object_count": 3, "selected_count": 1,
                    "frame_current": 1, "frame_end": 250,
                    "objects": [{"name": "Cube"}]}
        if command == "get_object_info":
            # The addon's parameter is `name`. Answering only to that is what
            # makes the test below able to catch the upstream spelling.
            if "name" not in params:
                return {"__error__": f"unexpected keyword arguments {sorted(params)}"}
            return {"name": params["name"], "type": "MESH",
                    "location": [0, 0, 0], "materials": []}
        if command == "get_viewport_screenshot":
            if not self.write_screenshots:
                return {"success": True}
            Path(params["filepath"]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 200)
            return {"success": True}
        if command == "execute_code":
            code = params.get("code", "")
            # An export is a bpy.ops call; make the file it claims to write.
            for line in code.splitlines():
                if "filepath" in line and self.out_dir is not None:
                    try:
                        kwargs = ast.literal_eval(line[line.index("(**") + 3:line.rindex(")")])
                        Path(kwargs["filepath"]).write_bytes(b"MESH" * 300)
                    except Exception:
                        pass
            return {"executed": True, "result": "exported\n"}
        return {"__error__": f"unknown command {command}"}

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                buf = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    try:
                        request = json.loads(buf.decode())
                        break
                    except json.JSONDecodeError:
                        continue
                if not buf:
                    conn.close()
                    continue
                command = request.get("type", "")
                params = request.get("params") or {}
                self.calls.append((command, params))
                result = self._reply(command, params)
                if "__error__" in result:
                    payload = {"status": "error", "message": result["__error__"]}
                else:
                    payload = {"status": "success", "result": result}
                conn.sendall(json.dumps(payload).encode())
            finally:
                conn.close()

    def close(self) -> None:
        self._stop.set()
        self._sock.close()


@pytest.fixture
def addon(tmp_path):
    fake = FakeAddon(out_dir=tmp_path)
    yield fake
    fake.close()


@pytest.fixture
def server(addon, tmp_path):
    config = MCPConfig(type="blender", enabled=True, port=addon.port,
                       output_directory=str(tmp_path), timeout=5, long_timeout=5)
    return PLUGIN_FACTORY(name="blender", system_config=AgentSystemConfig(),
                          mcp_config=config)


async def run_tool(server, action, params):
    """One real call through the framework, returning result and end line."""
    bus = get_status_bus()
    method = action[len(server.name) + 1:]
    queue = await bus.subscribe(server=f"{server.name}.{method}()")
    try:
        result = await server.call_with_status(action, params)
    finally:
        bus.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    closing = [e for e in events if e.phase in (StatusPhase.END, StatusPhase.ERROR)]
    assert len(closing) == 1, f"{action}: expected one closing event, got {closing}"
    return result, closing[0].message


# ── the export mapping: the knowledge this plugin exists to hold ──────────

@pytest.mark.parametrize("fmt,operator,selection_kwarg", [
    ("glb", "bpy.ops.export_scene.gltf", "use_selection"),
    ("gltf", "bpy.ops.export_scene.gltf", "use_selection"),
    ("fbx", "bpy.ops.export_scene.fbx", "use_selection"),
    ("obj", "bpy.ops.wm.obj_export", "export_selected_objects"),
    ("stl", "bpy.ops.wm.stl_export", "export_selected_objects"),
    ("ply", "bpy.ops.wm.ply_export", "export_selected_objects"),
    ("usd", "bpy.ops.wm.usd_export", "selected_objects_only"),
    ("abc", "bpy.ops.wm.alembic_export", "selected"),
])
async def test_export_sends_the_right_operator_and_selection_flag(
        server, addon, fmt, operator, selection_kwarg):
    """Measured against Blender 5.2.0 LTS. A wrong selection keyword does not
    raise — it exports the whole scene and reports success."""
    result, _ = await run_tool(
        server, "blender_export",
        {"format": fmt, "filename": f"m.{fmt}", "selected_only": True})
    assert result["status"] == "success", result

    code = [p["code"] for c, p in addon.calls if c == "execute_code"][-1]
    assert operator in code, f"{fmt} did not use {operator}: {code}"
    assert f"'{selection_kwarg}': True" in code, \
        f"{fmt} did not pass {selection_kwarg}=True: {code}"


@pytest.mark.parametrize("fmt,export_format", [
    ("glb", "GLB"),
    ("gltf", "GLTF_SEPARATE"),
])
async def test_gltf_variants_are_told_apart_by_export_format(server, addon, fmt, export_format):
    """glb and gltf share an operator AND a selection keyword, so the mapping
    test above cannot tell them apart -- swapping their `extra` values leaves
    it green. What separates them is export_format, and getting it wrong
    writes binary GLB into a file named .gltf (or a .gltf+.bin set under a
    .glb name). Nothing raises; the file is simply the wrong thing."""
    await run_tool(server, "blender_export",
                   {"format": fmt, "filename": f"x.{fmt}"})
    code = [p["code"] for c, p in addon.calls if c == "execute_code"][-1]
    assert f"'export_format': '{export_format}'" in code, code


async def test_whole_scene_export_sends_no_selection_flag(server, addon):
    """Counter-check: if the flag were always sent, the test above would pass
    for the wrong reason and `selected_only: false` would silently be a lie."""
    await run_tool(server, "blender_export",
                   {"format": "obj", "filename": "all.obj", "selected_only": False})
    code = [p["code"] for c, p in addon.calls if c == "execute_code"][-1]
    assert "export_selected_objects" not in code, code


async def test_blend_format_never_gets_a_selection_flag(server, addon):
    """A .blend is the whole session; there is no selection to restrict."""
    await run_tool(server, "blender_export",
                   {"format": "blend", "filename": "s.blend", "selected_only": True})
    code = [p["code"] for c, p in addon.calls if c == "execute_code"][-1]
    assert "bpy.ops.wm.save_as_mainfile" in code
    assert "selected" not in code, code


async def test_blend_export_does_not_hijack_the_users_session(server, addon):
    """Without copy=True, save_as_mainfile makes the exported file the
    session's CURRENT file -- the user's next Ctrl+S writes into our output
    directory instead of their own document. Measured against Blender 5.2.0:
    bpy.data.filepath went from '' to the exported path.

    This is the one export where getting it wrong damages something outside
    the workspace, so it gets its own guard rather than riding along in the
    mapping test."""
    await run_tool(server, "blender_export",
                   {"format": "blend", "filename": "scene.blend"})
    code = [p["code"] for c, p in addon.calls if c == "execute_code"][-1]
    assert "'copy': True" in code, (
        "save_as_mainfile without copy=True steals the session's file path: "
        f"{code}")


async def test_unknown_format_names_what_is_supported(server):
    result, line = await run_tool(server, "blender_export",
                                  {"format": "dae", "filename": "x.dae"})
    assert result["status"] == "error"
    assert "glb" in result["supported"] and "obj" in result["supported"]
    assert "dae" in line


# ── the sandbox ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("escape", [
    "../outside.glb",
    "../../etc/passwd",
    "sub/../../outside.glb",
])
async def test_export_cannot_write_outside_the_output_directory(server, escape):
    """Blender writes wherever it is told, so this check is the only thing
    between a model-chosen filename and someone else's file."""
    result, _ = await run_tool(server, "blender_export",
                               {"format": "glb", "filename": escape})
    assert result["status"] == "error", f"{escape} was accepted"
    assert "outside the output directory" in result["error"]


@pytest.mark.parametrize("absolute", [
    "C:/Windows/System32/evil.glb",
    "//server/share/evil.glb",
    "\\\\server\\share\\evil.glb",
    "/etc/passwd",
])
async def test_absolute_paths_are_rejected_too(server, absolute):
    """The docstring promises "escapes, absolute or not", but the ..-cases
    above would all stay green against a check that merely rejected "..".
    This is the half of that promise no test held."""
    result, _ = await run_tool(server, "blender_export",
                               {"format": "glb", "filename": absolute})
    assert result["status"] == "error", f"{absolute} was accepted"
    assert "outside the output directory" in result["error"]


async def test_screenshot_cannot_escape_either(server):
    result, _ = await run_tool(server, "blender_screenshot",
                               {"filename": "../shot.png"})
    assert result["status"] == "error"
    assert "outside the output directory" in result["error"]


async def test_a_subdirectory_below_the_root_is_allowed(server, tmp_path):
    """Counter-check for the escape tests: confinement that rejects everything
    is not confinement, it is a broken tool."""
    result, _ = await run_tool(
        server, "blender_export",
        {"format": "glb", "filename": "props/crate.glb"})
    assert result["status"] == "success", result
    assert Path(result["path"]).parent == (tmp_path / "props")


# ── the addon's second failure shape ─────────────────────────────────────

async def test_an_error_inside_a_success_envelope_is_still_an_error(server, addon):
    """The addon wraps whatever a handler RETURNS in status:success and only
    says status:error when the handler RAISED. Several handlers do not raise;
    they return {"error": ...}. Checking the envelope alone reports those as
    successes."""
    addon.soft_errors["get_world_state_snapshot"] = "boom inside Blender"
    result, line = await run_tool(server, "blender_scene", {})
    assert result["status"] == "error", (
        "a handler error wrapped in a success envelope was reported as success")
    assert "boom inside Blender" in result["error"]
    assert "boom" in line


async def test_a_failed_screenshot_never_reports_a_stale_file(server, addon, tmp_path):
    """The one that bites hardest: the agent's loop is look-change-look. A
    second screenshot that fails while an older file of the same name is still
    on disk would otherwise report success, and the agent reasons about a
    pre-edit frame with everything green."""
    first, _ = await run_tool(server, "blender_screenshot", {"filename": "v.png"})
    assert first["status"] == "success"
    stale_bytes = (tmp_path / "v.png").read_bytes()

    # No 3D viewport open: the addon returns {"error": ...} and writes nothing.
    addon.soft_errors["get_viewport_screenshot"] = "No 3D viewport found"
    addon.write_screenshots = False
    second, line = await run_tool(server, "blender_screenshot", {"filename": "v.png"})

    assert second["status"] == "error", "the stale file was reported as a fresh render"
    assert "No 3D viewport found" in second["error"], (
        f"the actionable reason was swallowed: {second}")
    assert (tmp_path / "v.png").read_bytes() == stale_bytes, "the old file was clobbered"
    assert "viewport" in line.lower() or "3D" in line


async def test_a_screenshot_that_reports_success_but_writes_nothing_is_caught(
        server, addon, tmp_path):
    """The case the error-envelope check does NOT cover: the addon answers
    with a clean success and simply does not write the file. Only comparing
    the file against what was there before catches that, and without this the
    stale-file guard is untested -- removing it leaves the suite green."""
    first, _ = await run_tool(server, "blender_screenshot", {"filename": "w.png"})
    assert first["status"] == "success"
    stale = (tmp_path / "w.png").read_bytes()

    addon.write_screenshots = False  # success, no error key, no file written
    second, _ = await run_tool(server, "blender_screenshot", {"filename": "w.png"})
    assert second["status"] == "error", "an unwritten screenshot reported success"
    assert second["error_type"] == "MissingOutput"
    assert (tmp_path / "w.png").read_bytes() == stale


async def test_an_export_that_writes_nothing_is_not_a_success(server, addon, tmp_path):
    """Same class on the export path: an operator that returns CANCELLED
    without raising, next to a same-named file from an earlier export."""
    first, _ = await run_tool(server, "blender_export",
                              {"format": "glb", "filename": "e.glb"})
    assert first["status"] == "success"

    addon.out_dir = None  # execute_code now runs but writes no file
    second, _ = await run_tool(server, "blender_export",
                               {"format": "glb", "filename": "e.glb"})
    assert second["status"] == "error", "an export that wrote nothing reported success"
    assert second["error_type"] == "MissingOutput"


# ── the wire ─────────────────────────────────────────────────────────────

async def test_object_uses_the_addon_parameter_name(server, addon):
    """The addon handler is get_object_info(name); upstream's TOOL calls the
    argument object_name, and sending that raises a TypeError inside Blender.
    This cost one live run to find."""
    result, line = await run_tool(server, "blender_object", {"name": "Cube"})
    assert result["status"] == "success", result
    command, params = [c for c in addon.calls if c[0] == "get_object_info"][-1]
    assert params == {"name": "Cube"}, params
    assert "Cube" in line


async def test_unreachable_blender_says_what_to_click(tmp_path):
    """The most common failure by far is a stopped addon server. The message
    has to be the fix, not a socket error."""
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()

    config = MCPConfig(type="blender", enabled=True, port=port,
                       output_directory=str(tmp_path), timeout=2)
    server = PLUGIN_FACTORY(name="blender", system_config=AgentSystemConfig(),
                            mcp_config=config)
    result, line = await run_tool(server, "blender_status", {})
    assert result["status"] == "error"
    assert result["error_type"] == "BlenderNotReachable"
    assert "Start MCP Server" in result["error"]
    assert str(port) in line


async def test_a_result_keeps_its_shape_whether_or_not_it_was_clipped(server, monkeypatch):
    """Clipping a dict can only produce a string, so folding truncation into
    the value would make `scene` an object on small scenes and a string on
    large ones — a contract an agent gets wrong exactly once, in the case that
    is hardest to reproduce. The flag carries it instead."""
    small, _ = await run_tool(server, "blender_scene", {})
    assert small["truncated"] is False
    assert isinstance(small["scene"], dict)

    monkeypatch.setattr("plugins.blender.server.MAX_RESULT_CHARS", 10)
    big, _ = await run_tool(server, "blender_scene", {})
    assert big["truncated"] is True, "clipping happened but nothing said so"
    assert isinstance(big["scene"], dict), (
        "the mapping turned into a string; a consumer reading "
        "scene['object_count'] now gets TypeError on exactly the large scenes")
    # The scalars survive, which is the point of trimming the list instead of
    # serialising the whole thing.
    assert big["scene"]["object_count"] == 3
    assert big["scene"]["objects_omitted"] == 1


# ── the status lines (tests/plugins/test_status_end_lines.py's rule) ──────

async def test_every_tool_closes_with_a_line_that_carries_a_result(server, addon):
    """The WebUI writes start, progress and end into one row, so the end line
    replaces everything before it — it has to carry subject AND result."""
    cases = [
        ("blender_status", {}, str(addon.port)),
        ("blender_scene", {}, "3 objects"),
        ("blender_object", {"name": "Cube"}, "Cube"),
        ("blender_execute", {"code": "print('hi')"}, "exported"),
        ("blender_screenshot", {"filename": "v.png"}, "v.png"),
        ("blender_export", {"format": "glb", "filename": "e.glb"}, "e.glb"),
    ]
    for action, params, expected in cases:
        result, line = await run_tool(server, action, params)
        assert result["status"] == "success", (action, result)
        assert len(line) <= 140, f"{action}: {len(line)} chars — {line}"
        assert expected in line, f"{action}: {expected!r} missing from {line!r}"


@pytest.mark.parametrize("action,params", [
    # A 63-character object name (Blender's limit) with the unrounded floats
    # get_object_info returns -- an ordinary imported asset, not a stress test.
    ("blender_object", {"name": "Sci_Fi_Corridor_Wall_Panel_Segment_B_LOD0_Damaged_Variant_02x"}),
    # A model-chosen filename is unbounded, and it lands in the row verbatim.
    ("blender_export", {"format": "glb", "filename": "a" * 200 + ".glb"}),
    ("blender_screenshot", {"filename": "b" * 200 + ".png"}),
    # The escape message repeats the whole output path.
    ("blender_export", {"format": "glb", "filename": "../" + "c" * 120 + ".glb"}),
])
async def test_long_inputs_do_not_blow_the_status_row(server, action, params):
    """The bus caps a row at 140 and cuts wherever it lands, which drops the
    tail -- and the tail is where the reason lives. The success-case assertion
    above only ever sees short names, so it cannot catch this."""
    _, line = await run_tool(server, action, params)
    assert len(line) <= 140, f"{action}: {len(line)} chars — {line}"
