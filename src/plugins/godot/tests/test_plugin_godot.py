"""Guards for the godot plugin.

Two fakes, so the suite needs neither Godot nor an editor:

* ``godot_stub.py`` stands in for the binary and answers the way 4.7.2 was
  measured to -- a runtime ``push_error`` exits 0, a parse error exits 1,
  every parse error arrives as TWO stderr blocks.
* ``FakeAddon`` is a WebSocket server speaking the godot_mcp envelope.

What the fakes make assertable that a live run cannot show reliably: which
flags a run actually sent, that the verdict comes from stderr and not from
the exit code, that setup enables the addon BEFORE the import that registers
its autoload, and that no path escapes its root.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.tools.status import StatusPhase, get_status_bus
from plugins.godot.plugin import PLUGIN_FACTORY
from plugins.godot.server import (ADDON_RES_PATH, ADDON_SOURCE, dedupe_load_failures,
                                  parse_godot_stderr)

STUB = Path(__file__).parent / "godot_stub.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 300

# ── fakes ────────────────────────────────────────────────────────────────


class FakeAddon:
    """Answers like the godot_mcp addon and records every command."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.wrong_id = False
        self.no_image = False
        #: Instead of an image: "garbage" (undecodable base64) or "jpeg".
        self.image_kind = "png"
        self.sleep: dict[str, float] = {}
        #: Close with this code instead of answering (4001 busy, 4002 stale).
        self.close_code: int | None = None
        #: Answer these commands with an error envelope instead of a result.
        self.errors: set[str] = set()
        #: What mcp_handshake reports as the open project (the server fixture
        #: points it at the test project).
        self.project_path = "C:/fake/"
        #: Send this text instead of a JSON reply.
        self.raw_reply: str | None = None
        self.port = 0

    async def handler(self, ws) -> None:
        async for raw in ws:
            request = json.loads(raw)
            command, params = request["command"], request.get("params") or {}
            self.calls.append((command, params))
            if command in self.sleep:
                await asyncio.sleep(self.sleep[command])
            if self.close_code is not None:
                await ws.close(self.close_code, "Another client is already connected"
                               if self.close_code == 4001 else "stale connection")
                return
            if self.raw_reply is not None:
                await ws.send(self.raw_reply)
                continue
            reply = self._reply(command, params)
            reply["id"] = "nope" if self.wrong_id else request["id"]
            await ws.send(json.dumps(reply))

    def _image(self) -> str:
        if self.image_kind == "garbage":
            return "!!!!"
        if self.image_kind == "jpeg":
            return base64.b64encode(b"\xff\xd8\xff" + b"j" * 100).decode()
        return base64.b64encode(PNG).decode()

    def _reply(self, command: str, params: dict) -> dict:
        ok = lambda result: {"status": "success", "result": result}  # noqa: E731
        if command in self.errors:
            return {"status": "error",
                    "error": {"code": "SCAN_TIMEOUT", "message": f"{command} refused"}}
        if command == "mcp_handshake":
            return ok({"addon_version": "4.1.11", "godot_version": "4.7.2-stable (official)",
                       "project_name": "fake_project", "project_path": self.project_path})
        if command == "get_scene_tree":
            return ok({"tree": {"name": "Main", "type": "Node", "children": [
                {"name": "Player", "type": "CharacterBody2D", "children": []},
                {"name": "Camera", "type": "Camera2D", "children": []}]}})
        if command == "get_node_properties":
            return ok({"properties": {"position": [0, 0], "visible": True}})
        if command == "find_nodes":
            return ok({"matches": [{"path": "/root/Main/Player", "type": "CharacterBody2D"}], "count": 1})
        if command == "update_node":
            return ok({})
        if command == "reparent_node":
            return ok({"new_path": f"{params['new_parent_path']}/X"})
        if command in ("open_scene", "save_scene", "reload_scene"):
            return ok({"path": params.get("scene_path") or params.get("path") or "current"})
        if command == "run_project":
            return ok({"running": True, "frozen": params.get("frozen"), "scene": params.get("scene_path")})
        if command == "game_time_step":
            return ok({"stepped_ms": params.get("duration_ms", 0), "frames": params.get("frames", 0)})
        if command == "capture_game_screenshot":
            result = {"width": 320, "height": 180}
            if not self.no_image:
                result["image_base64"] = self._image()
            return ok(result)
        if command == "execute_input_sequence":
            result = {"completed": True, "events_fired": len(params.get("inputs") or [])}
            if params.get("screenshot_at_ms"):
                result["screenshots"] = [{"requested_ms": ms, "ok": True, "width": 320, "height": 180,
                                          "image_base64": self._image()}
                                         for ms in params["screenshot_at_ms"]]
            return ok(result)
        if command in ("stop_project", "game_time_freeze", "game_time_thaw", "game_time_status",
                       "game_time_step_until", "type_text", "exec_run", "get_stack_trace",
                       "get_editor_state", "get_runtime_state"):
            # Echo so a test can assert what arrived under which name.
            return ok({"echo": command, "received": params})
        if command == "get_log_messages":
            # The real shape of an engine error: text in `type`, `message`
            # empty. A script's push_error would fill `message` instead.
            return ok({"cursor": 7, "match_count": 1,
                       "messages": [{"error_type": 1, "message": "",
                                     "type": "res://main.tscn:3 - ext_resource, invalid UID: boom"}]})
        if command == "get_input_map":
            return ok({"actions": {"jump": ["Space"]}})
        if command == "rescan_filesystem":
            return ok({"scanned": True, "reimported": params.get("paths") or [],
                       "duration_ms": 12})
        return {"status": "error", "error": {"code": "UNKNOWN_COMMAND",
                                             "message": f"Unknown command: {command}"}}


@pytest.fixture
async def addon():
    fake = FakeAddon()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        fake.port = server.sockets[0].getsockname()[1]
        yield fake


@pytest.fixture
def project(tmp_path):
    """A projects root with one project in it."""
    root = tmp_path / "projects"
    proj = root / "shmup"
    proj.mkdir(parents=True)
    (proj / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="Shmup"\n'
        'run/main_scene="res://main.tscn"\n', encoding="utf-8")
    (proj / "main.gd").write_text("extends Node\nfunc _ready():\n\tprint('ok')\n", encoding="utf-8")
    (proj / "main.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
    return proj


@pytest.fixture
def server(addon, project, tmp_path, monkeypatch):
    log = tmp_path / "stub_calls.jsonl"
    monkeypatch.setenv("GODOT_STUB_LOG", str(log))
    config = ToolServerConfig(type="godot", enabled=True, port=addon.port,
                       godot_binary="unused", projects_root=str(project.parent),
                       output_directory=str(tmp_path / "out"), timeout=3, long_timeout=5)
    srv = PLUGIN_FACTORY(name="godot", system_config=AgentSystemConfig(), server_config=config)
    srv._godot = [sys.executable, str(STUB)]
    srv._stub_log = log  # test-only handle
    addon.project_path = str(project)  # the editor has this project open
    return srv


@pytest.fixture
def no_editor(server):
    """Port 1 answers nothing and cannot be taken by a stray process."""
    server._port = 1
    return server


def stub_calls(server) -> list[list[str]]:
    log: Path = server._stub_log
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


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


# ── stderr parsing: the knowledge the headless channel rests on ─────────

PUSH_ERROR_BLOCK = (
    "ERROR: runtime probe error\n"
    "   at: push_error (core/variant/variant_utility.cpp:1023)\n"
    "   GDScript backtrace (most recent call first):\n"
    "       [0] _ready (res://main.gd:4)\n")

PARSE_ERROR_BLOCKS = (
    'SCRIPT ERROR: Parse Error: Expected expression for variable initial value after "=".\n'
    "   at: GDScript::reload (res://broken.gd:3)\n"
    'ERROR: Failed to load script "res://broken.gd" with error "Parse error".\n'
    "   at: load (modules/gdscript/gdscript_resource_format.cpp:46)\n"
    "   GDScript backtrace (most recent call first):\n"
    "       [0] _init (res://check.gd:17)\n")


def test_a_push_error_is_located_in_the_script_not_in_the_engine():
    """``at:`` for push_error is always variant_utility.cpp; the line the
    agent needs is the first script frame of the backtrace."""
    (block,) = parse_godot_stderr(PUSH_ERROR_BLOCK)
    assert block["kind"] == "ERROR"
    assert block["message"] == "runtime probe error"
    assert (block["file"], block["line"]) == ("res://main.gd", 4)
    assert block["at"].startswith("push_error")


def test_a_parse_error_keeps_its_own_line_and_the_follow_up_block_is_dropped():
    blocks = parse_godot_stderr(PARSE_ERROR_BLOCKS)
    assert len(blocks) == 2, "the raw parse must see both blocks"
    assert (blocks[0]["file"], blocks[0]["line"]) == ("res://broken.gd", 3)
    kept = dedupe_load_failures(blocks)
    assert len(kept) == 1 and kept[0]["kind"] == "SCRIPT ERROR"


def test_a_load_failure_without_a_parse_block_survives_the_dedupe():
    """Dedupe must only remove what is redundant: a script that failed to
    load for another reason (missing file) has no SCRIPT ERROR twin."""
    blocks = parse_godot_stderr('ERROR: Failed to load script "res://gone.gd" with error "File not found".\n'
                                "   at: load (modules/gdscript/gdscript_resource_format.cpp:46)\n")
    assert len(dedupe_load_failures(blocks)) == 1


def test_a_resource_message_keeps_its_own_location_over_the_calling_frame():
    """``load()`` warnings name the resource and line in the MESSAGE; the
    backtrace only says who called load(). Measured on the Shmup: every
    warning pointed at check_scripts.gd:18 instead of the .tscn."""
    (block,) = parse_godot_stderr(
        "WARNING: res://enemy.tscn:4 - ext_resource, invalid UID: uid://x - using text path instead: res://a.png\n"
        "   at: load (scene/resources/resource_format_text.cpp:501)\n"
        "   GDScript backtrace (most recent call first):\n"
        "       [0] _init (res://check_scripts.gd:18)\n")
    assert (block["file"], block["line"]) == ("res://enemy.tscn", 4)
    assert "_message_location" not in block


def test_warnings_are_told_apart_and_an_engine_location_is_kept_when_no_script_frame_follows():
    blocks = parse_godot_stderr(
        "WARNING: probe warning\n   at: push_warning (core/variant/variant_utility.cpp:1033)\n"
        "ERROR: This project doesn't have an `export_presets.cfg` file at its root.\n"
        "   at: _fs_changed (editor/editor_node.cpp:1417)\n")
    assert [b["kind"] for b in blocks] == ["WARNING", "ERROR"]
    assert (blocks[1]["file"], blocks[1]["line"]) == ("editor/editor_node.cpp", 1417)


# ── check ────────────────────────────────────────────────────────────────

async def test_check_all_is_clean_on_a_healthy_project(server, project):
    result, line = await run_tool(server, "godot_check", {"project": "shmup"})
    assert result["ok"] is True and result["scripts_checked"] == 1
    assert "parse clean" in line


async def test_check_all_reports_the_broken_file_with_its_line_once(server, project):
    (project / "broken.gd").write_text("extends Node\nBROKEN\n", encoding="utf-8")
    result, line = await run_tool(server, "godot_check", {"project": "shmup"})
    assert result["ok"] is False
    assert len(result["errors"]) == 1, "one parse error must count once, not twice"
    assert (result["errors"][0]["file"], result["errors"][0]["line"]) == ("res://broken.gd", 3)
    assert "res://broken.gd:3" in line


async def test_check_one_script_uses_check_only_and_a_res_path(server, project):
    (project / "enemy.gd").write_text("extends Node\n", encoding="utf-8")
    result, _ = await run_tool(server, "godot_check", {"project": "shmup", "script": "enemy.gd"})
    assert result["ok"] is True
    (argv,) = stub_calls(server)
    assert "--check-only" in argv and "res://enemy.gd" in argv and "--headless" in argv


async def test_a_script_that_fails_without_a_parse_block_is_still_named(server, project):
    """can_instantiate() false, nothing on stderr: the walker's FAILED line
    is the only evidence, and the tool must turn it into an error with a
    file rather than 'ok: False, errors: []'."""
    (project / "dep.gd").write_text("extends Node\nNOINST\n", encoding="utf-8")
    result, line = await run_tool(server, "godot_check", {"project": "shmup"})
    assert result["ok"] is False
    assert [(e["kind"], e["file"]) for e in result["errors"]] == [("CHECK", "res://dep.gd")]
    assert "res://dep.gd" in line


async def test_check_skips_the_addon_and_the_import_cache(server, project):
    """The vendored addon has 60 scripts; a check that walked into it would
    report on code the agent does not own."""
    (project / "addons" / "godot_mcp").mkdir(parents=True)
    (project / "addons" / "godot_mcp" / "x.gd").write_text("BROKEN", encoding="utf-8")
    (project / ".godot").mkdir()
    (project / ".godot" / "y.gd").write_text("BROKEN", encoding="utf-8")
    result, _ = await run_tool(server, "godot_check", {"project": "shmup"})
    assert result["ok"] is True and result["scripts_checked"] == 1


# ── run: the verdict comes from stderr, never from the exit code ─────────

async def test_a_runtime_error_is_a_failed_run_even_though_godot_exits_0(server, project):
    (project / "main.gd").write_text("extends Node\nfunc _ready():\n\tpass  # PUSH_ERROR\n",
                                     encoding="utf-8")
    result, line = await run_tool(server, "godot_run", {"project": "shmup", "frames": 5})
    assert result["exit"] == 0, "the stub reproduces Godot: push_error keeps exit 0"
    assert result["verdict"] == "errors"
    assert (result["errors"][0]["file"], result["errors"][0]["line"]) == ("res://main.gd", 4)
    assert "res://main.gd:4" in line and "1 errors" in line


async def test_a_clean_run_is_ok_and_its_output_has_no_banner(server, project):
    result, line = await run_tool(server, "godot_run", {"project": "shmup", "frames": 5})
    assert result["verdict"] == "ok"
    assert result["output"] == "scene ready"
    assert "Godot Engine" not in result["output"]
    assert "ok" in line and "shmup" in line


async def test_run_without_frames_quits_after_60_as_the_schema_promises(server, project):
    """Nothing in the framework applies schema defaults; the code has to. A
    missing --quit-after means the game runs until the 600 s long_timeout."""
    await run_tool(server, "godot_run", {"project": "shmup"})
    (argv,) = stub_calls(server)
    assert argv[argv.index("--quit-after") + 1] == "60"


async def test_unprefixed_stderr_is_carried_not_dropped(server, project):
    """printerr() and a crash handler's dump carry no ERROR: prefix. A run
    that drops them reports a crash as 'exit 139, 0 errors' and nothing."""
    (project / "main.gd").write_text("extends Node  # PRINTERR\n", encoding="utf-8")
    result, line = await run_tool(server, "godot_run", {"project": "shmup"})
    assert result["verdict"] == "ok" and result["stderr"] == "to stderr"
    assert "1 stderr lines" in line
    (project / "main.gd").write_text("extends Node  # CRASH\n", encoding="utf-8")
    result, line = await run_tool(server, "godot_run", {"project": "shmup"})
    assert result["verdict"] == "exit 139" and result["errors"] == []
    assert "Program crashed" in result["stderr"]
    assert "Program crashed" in line, "the reason must reach the status row"


def test_a_shader_error_is_an_error_block():
    (block,) = parse_godot_stderr("SHADER ERROR: Unknown identifier 'foo'.\n   at: ... (drivers/x.cpp:1)\n")
    assert block["kind"] == "SHADER ERROR"


async def test_run_sends_headless_quit_after_scene_and_user_args(server, project):
    await run_tool(server, "godot_run", {"project": "shmup", "frames": 12,
                                         "scene": "levels/one.tscn", "args": ["--fast", "x"]})
    (argv,) = stub_calls(server)
    assert "--headless" in argv
    assert argv[argv.index("--quit-after") + 1] == "12"
    assert "res://levels/one.tscn" in argv
    assert argv[argv.index("--") + 1:] == ["--fast", "x"]


async def test_windowed_omits_headless_and_frames_0_omits_quit_after(server, project):
    await run_tool(server, "godot_run", {"project": "shmup", "frames": 0, "windowed": True, "timeout": 5})
    (argv,) = stub_calls(server)
    assert "--headless" not in argv and "--quit-after" not in argv


async def test_a_run_that_never_quits_is_killed_and_reported_as_timeout(server, project):
    result, line = await run_tool(server, "godot_run",
                                  {"project": "shmup", "scene": "hang.tscn", "timeout": 1.5})
    assert result["verdict"] == "timeout" and result["timed_out"] is True
    assert "timeout" in line


def _gone(pid: int) -> bool:
    import psutil
    try:
        psutil.Process(pid).wait(timeout=5)
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        return False
    return True


async def test_a_timeout_kills_what_the_binary_started_and_answers_at_once(server, project):
    """The console exe is a wrapper around the real one, and a game may start
    processes. Killing only the direct child left the grandchild running, and
    the wait for the pipes it held kept the call from answering until it ended
    (20 s here)."""
    import time
    began = time.monotonic()
    result, _ = await run_tool(server, "godot_run",
                               {"project": "shmup", "scene": "spawn.tscn", "timeout": 2})
    elapsed = time.monotonic() - began
    child = int((project / "child.pid").read_text(encoding="utf-8"))
    assert result["timed_out"] is True
    assert _gone(child), "the child the binary started is still running"
    assert elapsed < 12, f"the call answered after {elapsed:.0f}s"


async def test_a_cancelled_run_kills_the_binary(server, project):
    """The thread running the binary cannot be cancelled; without killing the
    process a stopped call left the game running until its timeout."""
    task = asyncio.create_task(server.call_with_status(
        "godot_run", {"project": "shmup", "scene": "hang.tscn", "timeout": 25}))
    pid_file = project / "stub.pid"
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text(encoding="utf-8"):
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text(encoding="utf-8"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _gone(pid), "the binary outlived the cancelled call"


async def test_a_timeout_keeps_what_was_printed_when_an_orphan_holds_the_pipes(server, project):
    """The tree kill cannot reach a process whose parent already ended. On
    Windows communicate() then timed out once more and raised WITHOUT the
    output read so far: the answer carried nothing the game had printed."""
    import time

    import psutil
    began = time.monotonic()
    try:
        result, _ = await run_tool(server, "godot_run",
                                   {"project": "shmup", "scene": "orphan.tscn", "timeout": 2})
    finally:
        orphan = project / "orphan.pid"
        if orphan.exists():
            try:
                psutil.Process(int(orphan.read_text(encoding="utf-8"))).kill()
            except psutil.Error:
                pass
    assert result["timed_out"] is True
    assert "printed before the hang" in result["output"], result
    assert time.monotonic() - began < 15


async def test_a_run_whose_orphan_keeps_the_pipes_answers_within_one_grace_period(server, project):
    """Both readers share one five-second deadline; one after the other was ten."""
    import time

    import psutil
    began = time.monotonic()
    try:
        result, _ = await run_tool(server, "godot_run", {"project": "shmup", "scene": "orphan_exit.tscn"})
    finally:
        orphan = project / "orphan.pid"
        if orphan.exists():
            try:
                psutil.Process(int(orphan.read_text(encoding="utf-8"))).kill()
            except psutil.Error:
                pass
    assert result["exit"] == 0 and "printed before the hang" in result["output"], result
    assert time.monotonic() - began < 8.5


def test_the_tree_kill_reaps_through_the_popen():
    """psutil's wait would reap the root behind the Popen's back (POSIX): its
    returncode stayed None and the reaped-process guard no longer held."""
    import subprocess

    from plugins.godot import server as mod
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    mod._kill_tree(proc)
    assert proc.returncode is not None


def test_the_binary_gets_no_stdin(server, project, monkeypatch):
    import subprocess

    from plugins.godot import server as mod
    seen = {}
    real = subprocess.Popen

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(mod.subprocess, "Popen", spy)
    server._run_binary(["--version"], 10)
    assert seen.get("stdin") is subprocess.DEVNULL


def test_a_character_split_across_two_reads_is_decoded_whole():
    from plugins.godot import server as mod
    data = "Größe ✓".encode("utf-8")
    chunks = [data[:3], data[3:9], data[9:]]  # both multibyte characters cut
    assert mod._text(chunks) == "Größe ✓"


def test_the_tree_kill_leaves_a_reaped_process_alone(monkeypatch):
    """Once reaped, the pid may belong to someone else (POSIX reuses it)."""
    import subprocess

    import psutil

    from plugins.godot import server as mod
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    touched = []
    monkeypatch.setattr(psutil, "Process", lambda pid: touched.append(pid))
    mod._kill_tree(proc)
    assert not touched


async def test_a_cancelled_status_kills_the_version_probe(server, tmp_path, monkeypatch):
    pid_file = tmp_path / "version.pid"
    monkeypatch.setenv("GODOT_STUB_HANG_VERSION", str(pid_file))
    task = asyncio.create_task(server.call_with_status("godot_status", {}))
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text(encoding="utf-8"):
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text(encoding="utf-8"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _gone(pid), "--version outlived the cancelled status call"


async def test_a_cancel_that_comes_before_the_binary_started_still_kills_it(server, project):
    """The cancel can land while the thread has not started the process yet;
    it leaves a None in the list, and the process kills itself on arrival."""
    import time
    began = time.monotonic()
    raw = await asyncio.to_thread(server._run_binary,
                                  ["--path", str(project), "hang.tscn"], 25, [None])
    assert raw["timed_out"] is False and raw["exit"] != 0
    assert time.monotonic() - began < 10, "the process ran on after the cancel"


# ── script ───────────────────────────────────────────────────────────────

async def test_script_prepends_extends_and_removes_its_temp_file(server, project):
    result, line = await run_tool(server, "godot_script",
                                  {"project": "shmup", "code": "func _init():\n\tprint('hello')\n"})
    assert result["exit"] == 0 and result["output"] == "hello"
    (argv,) = stub_calls(server)
    tmp = Path(argv[argv.index("-s") + 1])
    assert not tmp.exists(), "the agent's script must not pile up in the temp directory"
    assert "--quit-after" in argv and argv[argv.index("--quit-after") + 1] == "1"
    assert "hello" in line


async def test_script_keeps_an_explicit_extends(server, project, monkeypatch):
    seen = {}
    real = server._run_binary

    def spy(args, timeout, *rest):
        seen["code"] = Path(args[args.index("-s") + 1]).read_text(encoding="utf-8")
        return real(args, timeout, *rest)

    monkeypatch.setattr(server, "_run_binary", spy)
    await run_tool(server, "godot_script", {"project": "shmup", "code": "extends MainLoop\nfunc _init():\n\tpass\n"})
    assert seen["code"].startswith("extends MainLoop\n")
    assert seen["code"].count("extends") == 1


async def test_a_leak_report_at_exit_does_not_fail_the_script(server, project):
    result, line = await run_tool(server, "godot_script",
                                  {"project": "shmup", "code": "func _init():\n\tprint('done')  # LEAK\n"})
    assert result["errors"] == [] and len(result["exit_leaks"]) == 1
    assert "exit 0" in line and "errors" not in line


async def test_script_reports_a_parse_error_with_exit_1(server, project):
    result, line = await run_tool(server, "godot_script", {"project": "shmup", "code": "func _init():\n\tBROKEN\n"})
    assert result["exit"] == 1 and len(result["errors"]) == 1
    assert result["errors"][0]["kind"] == "SCRIPT ERROR"
    assert "1 errors" in line


# ── setup: the order matters ─────────────────────────────────────────────

async def test_setup_creates_a_project_installs_and_enables_the_addon_and_the_autoload_lands(server, project):
    result, line = await run_tool(server, "godot_setup",
                                  {"project": "fresh", "name": "Fresh Game", "main_scene": "main.tscn"})
    assert result["status"] == "success", result
    fresh = project.parent / "fresh"
    text = (fresh / "project.godot").read_text(encoding="utf-8")
    assert 'config/name="Fresh Game"' in text
    assert 'run/main_scene="res://main.tscn"' in text
    assert 'config/features=PackedStringArray("4.7"' in text, "features come from the binary's version"
    assert (fresh / "addons" / "godot_mcp" / "plugin.cfg").is_file()
    assert f'enabled=PackedStringArray("{ADDON_RES_PATH}")' in text
    assert "MCPGameBridge=" in text, "the import must have run AFTER install+enable, or the addon never loads"
    assert result["created"] and result["addon"].endswith("installed") and result["autoload_registered"]
    assert "created" in line and "autoload registered" in line
    # The stub also reports the main scene as missing, which is true here --
    # by its message, not by the engine source file it was raised from.
    assert "import: 1 errors" in line and "Cannot open file" in line
    assert "resource_format_text.cpp" not in line


async def test_the_import_runs_headless_and_then_refreshes_the_open_editor(server, addon, project):
    """The headless run is what decides: it is the one that takes the
    project as an argument and names the files that failed. The editor is
    refreshed afterwards so it sees the result."""
    result, line = await run_tool(server, "godot_import_assets", {"project": "shmup"})
    assert result["ok"] is True and result["editor_refreshed"] is True
    (argv,) = stub_calls(server)
    assert "--import" in argv and "--headless" in argv
    assert ("rescan_filesystem", {}) in addon.calls
    assert "shmup" in line and "editor refreshed" in line


async def test_an_editor_on_another_project_is_not_rescanned(server, addon, project):
    """`rescan_filesystem` takes no project -- it scans whatever the editor
    has open. Rescanning the wrong one and reporting success for this one is
    a silent wrong answer."""
    addon.project_path = str(project.parent / "other")
    result, line = await run_tool(server, "godot_import_assets", {"project": "shmup"})
    assert result["ok"] is True and result["editor_refreshed"] is False
    assert "rescan_filesystem" not in [c for c, _ in addon.calls]
    assert "editor not refreshed" in line


@pytest.mark.parametrize("close_code, code", [(4001, "BUSY"), (4002, "STALE")])
async def test_a_refresh_that_fails_does_not_fail_the_import(server, addon, project,
                                                             close_code, code):
    """A busy editor, or the addon dropping a socket it considers idle while
    its own scan runs on: the assets are imported either way."""
    addon.close_code = close_code
    result, line = await run_tool(server, "godot_import_assets", {"project": "shmup"})
    assert result["ok"] is True and result["editor_refreshed"] is False
    assert code in (result["editor_note"] or "")
    assert stub_calls(server), "the import still ran"


async def test_a_rescan_the_editor_refuses_does_not_fail_the_import(server, addon, project):
    """The handshake succeeds, the scan does not: the assets are imported,
    the editor's view is stale, and the reply says which."""
    addon.errors = {"rescan_filesystem"}
    result, line = await run_tool(server, "godot_import_assets", {"project": "shmup"})
    assert result["ok"] is True and result["editor_refreshed"] is False
    assert "SCAN_TIMEOUT" in (result["editor_note"] or "")
    assert stub_calls(server), "the import still ran"
    assert "editor not refreshed" in line


async def test_an_addon_error_that_is_not_the_port_complaint_still_counts(server, project, no_editor):
    """The exemption is one measured line, not the addon's whole output. A
    broader match would swallow a real failure inside the addon."""
    (project / "ADDON_ERROR").write_text("", encoding="utf-8")
    result, line = await run_tool(server, "godot_import_assets", {"project": "shmup"})
    assert result["ok"] is False
    assert any("godot-mcp" in e["message"] for e in result["errors"])


async def test_without_an_editor_the_import_stands_on_its_own(server, project, no_editor):
    result, line = await run_tool(server, "godot_import_assets", {"project": "shmup"})
    assert result["ok"] is True and result["editor_refreshed"] is False
    assert result["editor_note"] is None, "no editor is not a defect worth a note"
    (argv,) = stub_calls(server)
    assert "--import" in argv and "--headless" in argv
    assert "shmup" in line and "0 errors" in line and "not refreshed" not in line
    (project / "main.tscn").unlink()
    result, line = await run_tool(server, "godot_import_assets", {"project": "shmup"})
    assert result["ok"] is False and "Cannot open file" in line


async def test_the_addons_own_port_complaint_is_a_warning_not_a_project_error(server, project, no_editor):
    """Every run that loads the project in EDITOR mode starts the bundled
    addon, which cannot bind the port a running editor holds. Measured in a
    real session: that complaint made a working import read as failed."""
    (project / "PORT_TAKEN").write_text("", encoding="utf-8")
    result, line = await run_tool(server, "godot_import_assets", {"project": "shmup"})
    assert result["ok"] is True and result["errors"] == []
    assert any("Failed to start server" in w["message"] for w in result["warnings"]),         "kept as a warning, not dropped"
    assert "0 errors" in line


async def test_the_addons_own_port_complaint_does_not_fail_an_export(server, project, tmp_path, no_editor):
    """The export is the expensive one: it demands a clean run, so the same
    complaint turned a correctly written pack into ExportFailed."""
    (project / "PORT_TAKEN").write_text("", encoding="utf-8")
    result, line = await run_tool(server, "godot_export",
                                  {"project": "shmup", "preset": "Windows",
                                   "output": "game.exe"})
    assert result["status"] == "success", result
    assert any("Failed to start server" in w["message"] for w in result["warnings"])


async def test_setup_is_idempotent_and_says_present(server, project):
    await run_tool(server, "godot_setup", {"project": "fresh"})
    result, line = await run_tool(server, "godot_setup", {"project": "fresh"})
    assert result["created"] is False and "present" in result["addon"]
    assert "present" in line


async def test_setup_appends_to_an_existing_plugin_list_instead_of_replacing_it(server, project):
    (project / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="Shmup"\n\n[editor_plugins]\n\n'
        'enabled=PackedStringArray("res://addons/other/plugin.cfg")\n', encoding="utf-8")
    result, _ = await run_tool(server, "godot_setup", {"project": "shmup"})
    text = (project / "project.godot").read_text(encoding="utf-8")
    assert 'enabled=PackedStringArray("res://addons/other/plugin.cfg", "res://addons/godot_mcp/plugin.cfg")' in text
    assert result["autoload_registered"] is True


async def test_setup_force_reinstalls_the_addon(server, project):
    await run_tool(server, "godot_setup", {"project": "shmup"})
    marker = project / "addons" / "godot_mcp" / "stale.txt"
    marker.write_text("x", encoding="utf-8")
    result, _ = await run_tool(server, "godot_setup", {"project": "shmup", "force": True})
    assert not marker.exists() and "reinstalled" in result["addon"]


async def test_setup_with_an_empty_plugin_list_writes_a_well_formed_one(server, project):
    """Godot leaves `enabled=PackedStringArray()` behind after the last plugin
    is disabled; appending with a comma would corrupt project.godot."""
    (project / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="Shmup"\n\n[editor_plugins]\n\n'
        'enabled=PackedStringArray()\n', encoding="utf-8")
    result, _ = await run_tool(server, "godot_setup", {"project": "shmup"})
    text = (project / "project.godot").read_text(encoding="utf-8")
    assert 'enabled=PackedStringArray("res://addons/godot_mcp/plugin.cfg")' in text
    assert result["autoload_registered"] is True


async def test_setup_does_not_mistake_a_later_sections_enabled_key_for_the_plugin_list(server, project):
    (project / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="Shmup"\n\n[editor_plugins]\n\n'
        '[thirdparty]\n\nenabled=PackedStringArray("res://other.cfg")\n', encoding="utf-8")
    await run_tool(server, "godot_setup", {"project": "shmup"})
    text = (project / "project.godot").read_text(encoding="utf-8")
    assert 'enabled=PackedStringArray("res://other.cfg")\n' in text, "the foreign section stays untouched"
    assert text.count("godot_mcp/plugin.cfg") == 1
    assert text.count("[editor_plugins]") == 1, "no duplicate section appended at the end"
    plugins_section = text[text.index("[editor_plugins]"):].split("[thirdparty]")[0]
    assert 'enabled=PackedStringArray("res://addons/godot_mcp/plugin.cfg")' in plugins_section


async def test_setup_that_times_out_importing_is_not_a_success(server, project):
    (project / "HANG_IMPORT").write_text("", encoding="utf-8")
    result, line = await run_tool(server, "godot_setup", {"project": "shmup"})
    assert result["status"] == "error" and result["import"]["timed_out"] is True
    assert "timed out" in line


@pytest.mark.parametrize("field, value", [
    ("name", 'Evil"\n[autoload]\nX="*res://x.gd'),
    ("main_scene", 'main.tscn"\nrun/x="y'),
    ("name", "back\\slash"),
])
async def test_setup_refuses_a_value_that_would_rewrite_project_godot(server, project, field, value):
    """A quote or a line break closes the quoted string and adds lines of its own."""
    result, line = await run_tool(server, "godot_setup", {"project": "fresh", field: value})
    assert result["status"] == "error" and field in result["error"], result
    assert not (project.parent / "fresh" / "project.godot").exists()


async def test_setup_refuses_to_guess_the_engine_version(server, project):
    """A binary that cannot answer --version must not produce a project.godot
    with a made-up feature tag."""
    server._godot = [sys.executable, "-c", "import sys; sys.exit(2)"]
    result, line = await run_tool(server, "godot_setup", {"project": "fresh"})
    assert result["status"] == "error" and "version" in result["error"]
    assert not (project.parent / "fresh" / "project.godot").exists()
    assert "version" in line


async def test_import_assets_with_a_bad_exit_and_no_blocks_is_an_error(server, project, no_editor):
    (project / "IMPORT_FAIL").write_text("", encoding="utf-8")
    result, line = await run_tool(server, "godot_import_assets", {"project": "shmup"})
    assert result["ok"] is False and "exit 2" in line


async def test_setup_reports_when_the_autoload_did_not_land(server, project, monkeypatch):
    """A stub that skips the autoload = an import that did not load the addon.
    That must not read as success."""
    monkeypatch.setattr(server, "_enable_plugin", staticmethod(lambda cfg: False))
    result, line = await run_tool(server, "godot_setup", {"project": "shmup"})
    assert result["status"] == "error" and result["autoload_registered"] is False
    assert "did not register" in line


# ── export ───────────────────────────────────────────────────────────────

async def test_export_writes_into_the_output_directory(server, project, tmp_path):
    result, line = await run_tool(server, "godot_export",
                                  {"project": "shmup", "preset": "Windows Desktop", "filename": "shmup.exe"})
    assert result["status"] == "success"
    assert Path(result["path"]) == tmp_path / "out" / "shmup.exe" and result["bytes"] == 2100
    (argv,) = stub_calls(server)
    assert argv[argv.index("--export-release") + 1] == "Windows Desktop"
    assert "shmup.exe" in line


async def test_export_that_writes_nothing_carries_godots_reason(server, project):
    result, line = await run_tool(server, "godot_export", {"project": "shmup", "preset": "Nope", "filename": "x.zip"})
    assert result["status"] == "error" and result["error_type"] == "MissingOutput"
    assert "export_presets.cfg" in line
    assert "[" not in result["output"], "progress lines are noise and must be stripped"


async def test_an_export_that_wrote_a_file_but_did_not_finish_is_not_a_success(server, project, tmp_path):
    """Godot writes the pack incrementally: a run that exits 1 or is killed
    leaves a file that changed and is garbage."""
    result, line = await run_tool(server, "godot_export",
                                  {"project": "shmup", "preset": "Partial", "filename": "p.zip"})
    assert result["status"] == "error" and result["error_type"] == "ExportFailed"
    assert (tmp_path / "out" / "p.zip").exists(), "the half file is there -- and must not count"
    assert "exit 1" in line
    result, line = await run_tool(server, "godot_export",
                                  {"project": "shmup", "preset": "Slow", "filename": "s.zip"})
    assert result["status"] == "error" and result["timed_out"] is True
    assert "timed out" in line


@pytest.mark.parametrize("escape", ["../x.exe", "../../x.exe", "a/../../x.exe", "C:/x.exe"])
async def test_export_cannot_write_outside_the_output_directory(server, project, escape):
    result, _ = await run_tool(server, "godot_export", {"project": "shmup", "preset": "P", "filename": escape})
    assert result["status"] == "error" and "outside the output directory" in result["error"]
    assert not stub_calls(server), "the binary must not even be started"


# ── project resolution ───────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["../..", "../../etc", "C:/Windows", "/tmp"])
async def test_a_project_outside_the_root_is_refused_before_anything_runs(server, value):
    result, _ = await run_tool(server, "godot_check", {"project": value})
    assert result["status"] == "error" and "outside the projects root" in result["error"]
    assert not stub_calls(server)


# \??\UNC\... is refused too, but on Windows it is not absolute: joined onto the
# root it lands below the drive, so without the guard it is refused all the same.
REMOTE = ["//host/share/p", r"\\host\share\p", r"\\?\UNC\host\share\p"]


@pytest.fixture
def no_remote_resolve(monkeypatch):
    """Resolving a share path connects to the host (and signs in) on Windows;
    it must be refused on its text, before any such call."""
    import pathlib
    real = pathlib.Path.resolve

    def guard(self, *a, **kw):
        text = str(self).replace("/", "\\")
        assert not text.startswith((r"\\", r"\??")), f"resolved {self}"
        return real(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "resolve", guard)


@pytest.mark.parametrize("value", REMOTE)
async def test_a_project_on_a_share_is_refused_before_any_file_system_call(server, value, no_remote_resolve):
    result, _ = await run_tool(server, "godot_check", {"project": value})
    assert result["status"] == "error" and "outside the projects root" in result["error"], result
    assert not stub_calls(server)


@pytest.mark.parametrize("value", REMOTE)
async def test_an_output_file_on_a_share_is_refused(server, addon, value, no_remote_resolve):
    result, _ = await run_tool(server, "godot_observe",
                               {"action": "screenshot", "filename": value + "/x.png"})
    assert result["status"] == "error" and "outside the output directory" in result["error"], result
    assert not addon.calls


@pytest.mark.parametrize("value", REMOTE)
async def test_a_scene_on_a_share_never_reaches_godot(server, project, value):
    """Godot would load it -- a scene and its scripts from another host."""
    result, _ = await run_tool(server, "godot_run", {"project": "shmup", "scene": value + "/level.tscn"})
    assert result["status"] == "error" and "network share" in result["error"], result
    assert not stub_calls(server)


async def test_a_preset_that_reads_as_an_option_is_refused(server, project):
    """Godot's first pass over the arguments does not consume the preset after
    --export-release; "--path" there would repoint the project."""
    result, _ = await run_tool(server, "godot_export",
                               {"project": "shmup", "preset": "--path", "filename": "a/b/p.zip"})
    assert result["status"] == "error" and "preset" in result["error"], result
    assert not stub_calls(server)
    assert not (server._out_dir / "a").exists(), "a refused call left folders behind"


async def test_a_missing_project_points_at_setup(server, project):
    result, _ = await run_tool(server, "godot_run", {"project": "nothing_here"})
    assert result["status"] == "error" and "setup" in result["error"]


async def test_an_absolute_path_inside_the_root_is_fine(server, project):
    result, _ = await run_tool(server, "godot_check", {"project": str(project)})
    assert result["ok"] is True


# ── editor channel ───────────────────────────────────────────────────────

async def test_status_names_the_project_the_editor_has_open(server, addon):
    result, line = await run_tool(server, "godot_status", {})
    assert result["binary"]["ok"] and result["editor"]["reachable"]
    assert "fake_project" in line and "4.1.11" in line and "4.7.2" in line


async def test_status_without_an_editor_is_information_not_failure(server, addon, monkeypatch):
    monkeypatch.setattr(server, "_port", 1)
    result, line = await run_tool(server, "godot_status", {})
    assert result["status"] == "success" and result["editor"]["reachable"] is False
    assert "setup" in result["editor"]["hint"]
    assert "editor: none" in line


async def test_scene_tree_counts_the_nodes(server, addon):
    result, line = await run_tool(server, "godot_scene", {"action": "tree", "max_depth": 2})
    assert result["result"]["tree"]["name"] == "Main"
    assert addon.calls == [("get_scene_tree", {"max_depth": 2, "max_children": 0})]
    assert "root 'Main'" in line and "3 nodes" in line


async def test_scene_open_normalises_to_a_res_path(server, addon):
    await run_tool(server, "godot_scene", {"action": "open", "scene_path": "levels/one.tscn"})
    assert addon.calls == [("open_scene", {"scene_path": "res://levels/one.tscn"})]


async def test_node_update_sends_the_properties_and_names_them(server, addon):
    result, line = await run_tool(server, "godot_node", {
        "action": "update", "node_path": "Player", "properties": {"position": [1, 2], "visible": False}})
    assert result["status"] == "success"
    assert addon.calls == [("update_node", {"node_path": "Player", "properties": {"position": [1, 2], "visible": False}})]
    assert "Player" in line and "position" in line


async def test_node_update_without_properties_never_reaches_the_editor(server, addon):
    result, _ = await run_tool(server, "godot_node", {"action": "update", "node_path": "Player"})
    assert result["status"] == "error" and not addon.calls


async def test_play_run_starts_frozen_by_default(server, addon):
    """A game that runs free between two tool calls is somewhere else by the
    time the agent looks; frozen is the only deterministic default."""
    await run_tool(server, "godot_play", {"action": "run", "scene_path": "main.tscn"})
    assert addon.calls == [("run_project", {"frozen": True, "scene_path": "res://main.tscn"})]


async def test_play_step_needs_a_duration_or_frames(server, addon):
    result, _ = await run_tool(server, "godot_play", {"action": "step"})
    assert result["status"] == "error" and not addon.calls
    result, line = await run_tool(server, "godot_play", {"action": "step", "frames": 3,
                                                        "inputs": [{"action_name": "jump"}]})
    assert result["status"] == "success"
    assert addon.calls == [("game_time_step", {"frames": 3, "inputs": [{"action_name": "jump"}]})]
    assert "step" in line


async def test_screenshot_writes_a_png_and_returns_its_path(server, addon, tmp_path):
    result, line = await run_tool(server, "godot_observe", {"action": "screenshot", "filename": "shot.jpg"})
    assert result["status"] == "success"
    path = Path(result["path"])
    assert path == tmp_path / "out" / "shot.png", "PNG bytes get a PNG suffix, whatever was asked"
    assert path.read_bytes() == PNG
    assert addon.calls == [("capture_game_screenshot", {"max_width": 900})]
    assert "shot.png" in line and "320x180" in line


@pytest.mark.parametrize("kind,expected", [("garbage", "undecodable"), ("jpeg", "not a PNG")])
async def test_bad_image_data_never_touches_the_previous_screenshot(server, addon, tmp_path, kind, expected):
    """b64decode without validation turns garbage into b'' -- and writing
    that would replace the last good frame with an empty file."""
    old = tmp_path / "out" / "game.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old frame")
    addon.image_kind = kind
    result, line = await run_tool(server, "godot_observe", {"action": "screenshot"})
    assert result["status"] == "error" and expected in result["error"]
    assert old.read_bytes() == b"old frame"
    assert expected in line


async def test_a_screenshot_without_image_data_is_an_error_not_a_stale_file(server, addon, tmp_path):
    stale = tmp_path / "out" / "game.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    addon.no_image = True
    result, line = await run_tool(server, "godot_observe", {"action": "screenshot"})
    assert result["status"] == "error" and "no image" in result["error"]
    assert stale.read_bytes() == b"old"
    assert "no image" in line


@pytest.mark.parametrize("escape", ["../shot.png", "../../x/shot.png"])
async def test_screenshot_cannot_escape_the_output_directory(server, addon, escape):
    result, _ = await run_tool(server, "godot_observe", {"action": "screenshot", "filename": escape})
    assert result["status"] == "error" and "outside the output directory" in result["error"]
    assert not addon.calls


@pytest.mark.parametrize("given, sent", [(100000, 4096), (5, 128), (-1, 128), (640, 640)])
async def test_screenshot_widths_stay_within_the_schemas_range(server, addon, given, sent):
    """The addon scales only for a positive cap below the picture: 0, a
    negative or a huge value returned the full resolution."""
    await run_tool(server, "godot_observe", {"action": "screenshot", "max_width": given})
    await run_tool(server, "godot_play", {"action": "input", "inputs": [{"action_name": "jump"}],
                                          "screenshot_at_ms": [10], "screenshot_max_width": given})
    assert addon.calls[0] == ("capture_game_screenshot", {"max_width": sent})
    assert addon.calls[1][1]["screenshot_max_width"] == sent


async def test_logs_carry_the_cursor_forward(server, addon):
    result, line = await run_tool(server, "godot_observe", {"action": "logs", "since": 3, "severity": "error"})
    assert addon.calls == [("get_log_messages", {"since": 3, "severity": "error"})]
    assert result["result"]["cursor"] == 7
    assert "cursor 7" in line and "invalid UID" in line, \
        "the text of an engine error sits in `type`, and the line must still carry it"


async def test_command_is_a_raw_passthrough(server, addon):
    result, line = await run_tool(server, "godot_command", {"command": "get_input_map", "params": {"x": 1}})
    assert result["result"] == {"actions": {"jump": ["Space"]}}
    assert addon.calls == [("get_input_map", {"x": 1})]
    assert "get_input_map" in line


async def test_an_addon_error_keeps_its_code(server, addon):
    result, line = await run_tool(server, "godot_command", {"command": "no_such"})
    assert result["status"] == "error" and result["code"] == "UNKNOWN_COMMAND"
    assert "Unknown command" in line


async def test_a_reply_with_a_foreign_id_is_refused(server, addon):
    addon.wrong_id = True
    result, _ = await run_tool(server, "godot_scene", {"action": "tree"})
    assert result["status"] == "error" and result["code"] == "PROTOCOL"


async def test_a_silent_addon_times_out_with_a_code(server, addon):
    addon.sleep["get_scene_tree"] = 5
    result, line = await run_tool(server, "godot_scene", {"action": "tree"})
    assert result["status"] == "error" and result["code"] == "TIMEOUT"
    assert "TIMEOUT" in line


@pytest.mark.parametrize("code,expected", [(4001, "BUSY"), (4002, "STALE"), (1001, "DISCONNECTED")])
async def test_a_close_during_the_command_is_a_coded_error_not_a_raw_exception(server, addon, code, expected):
    """The addon rejects a second client AFTER the handshake (4001) and drops
    an idle one (4002): both arrive on recv, not on connect."""
    addon.close_code = code
    result, line = await run_tool(server, "godot_scene", {"action": "tree"})
    assert result["status"] == "error" and result["code"] == expected
    assert expected in line


async def test_a_reply_that_is_not_json_is_a_protocol_error(server, addon):
    addon.raw_reply = "HTTP/1.1 200 OK"
    result, _ = await run_tool(server, "godot_scene", {"action": "tree"})
    assert result["status"] == "error" and result["code"] == "PROTOCOL"
    assert "not JSON" in result["error"]


async def test_status_survives_whatever_the_editor_does(server, addon):
    """The one tool the prompts say to call when others fail must itself
    never raise: a busy addon is an answer, not an exception."""
    addon.close_code = 4001
    result, line = await run_tool(server, "godot_status", {})
    assert result["status"] == "success" and result["editor"]["reachable"] is False
    assert "BUSY" in result["editor"]["hint"]
    assert "editor: none" in line


@pytest.mark.parametrize("action,params,command,expected_params", [
    ("stop", {}, "stop_project", {}),
    ("freeze", {}, "game_time_freeze", {}),
    ("thaw", {}, "game_time_thaw", {}),
    ("status", {}, "game_time_status", {}),
    ("step_until", {"until": "root.x > 1", "max_ms": 500}, "game_time_step_until",
     {"until": "root.x > 1", "max_ms": 500}),
    ("type_text", {"text": "hi", "submit": True, "delay_ms": 5}, "type_text",
     {"text": "hi", "submit": True, "delay_ms": 5}),
    ("exec", {"source": "return 1"}, "exec_run", {"source": "return 1"}),
])
async def test_every_play_action_reaches_the_addon_under_its_real_name(server, addon, action, params, command, expected_params):
    """The fake answers UNKNOWN_COMMAND to any name it does not implement,
    so a renamed command in the plugin would surface here -- the gap that
    let the state action ship with a parameter the addon never reads."""
    result, _ = await run_tool(server, "godot_play", {"action": action, **params})
    assert result["status"] == "success", result
    assert addon.calls == [(command, expected_params)]


@pytest.mark.parametrize("action,params,command,expected_params", [
    ("state", {"paths": ["/root/Main/Player"], "include": ["velocity"]}, "get_runtime_state",
     {"paths": ["/root/Main/Player"], "include": ["velocity"]}),
    ("state", {"type": "Area2D", "max_nodes": 5}, "get_runtime_state", {"type": "Area2D", "max_nodes": 5}),
    ("stack", {}, "get_stack_trace", {}),
    ("editor", {}, "get_editor_state", {}),
])
async def test_every_observe_action_sends_what_the_game_bridge_reads(server, addon, action, params, command, expected_params):
    result, _ = await run_tool(server, "godot_observe", {"action": action, **params})
    assert result["status"] == "success", result
    assert addon.calls == [(command, expected_params)]


async def test_play_run_with_frozen_null_still_starts_frozen(server, addon):
    await run_tool(server, "godot_play", {"action": "run", "frozen": None})
    assert addon.calls == [("run_project", {"frozen": True})]


async def test_screenshots_inside_an_input_sequence_become_files(server, addon, tmp_path):
    """The most useful thing execute_input_sequence does -- capture mid-
    sequence -- answers with base64 per capture. That belongs on disk."""
    result, _ = await run_tool(server, "godot_play", {
        "action": "input", "inputs": [{"action_name": "left"}], "screenshot_at_ms": [100, 400]})
    shots = result["result"]["screenshots"]
    assert [Path(s["path"]).name for s in shots] == ["play_input_0.png", "play_input_1.png"]
    assert all("image_base64" not in s for s in shots)
    assert (tmp_path / "out" / "play_input_1.png").read_bytes() == PNG
    assert addon.calls[0][1]["screenshot_at_ms"] == [100, 400]


async def test_an_image_through_the_raw_command_passthrough_is_spilled_too(server, addon, tmp_path):
    result, line = await run_tool(server, "godot_command", {"command": "capture_game_screenshot"})
    assert "image_base64" not in result["result"]
    assert Path(result["result"]["path"]).name == "command_capture_game_screenshot_0.png"
    assert "path=" in line


async def test_logs_say_when_the_limit_cut_the_list(server, addon, monkeypatch):
    reply = addon._reply
    monkeypatch.setattr(addon, "_reply", lambda c, p: (
        {"status": "success", "result": {"cursor": 9, "match_count": 17,
                                         "messages": [{"message": "one", "type": ""}]}}
        if c == "get_log_messages" else reply(c, p)))
    _, line = await run_tool(server, "godot_observe", {"action": "logs", "limit": 1})
    assert "1 of 17 log messages" in line


def test_clip_bounds_a_dict_with_no_list_to_halve():
    """A scene tree is a dict of dicts; a screenshot through the passthrough
    is a dict with one huge string. Neither has a list, and both came back
    whole with `truncated: True` before this guard."""
    from plugins.godot.server import GodotServer, MAX_RESULT_CHARS
    big_string, note = GodotServer._clip({"image_base64": "x" * 200_000, "width": 1})
    assert len(json.dumps(big_string)) <= MAX_RESULT_CHARS and "image_base64" in note
    assert big_string["width"] == 1
    # Wide, like a real scene: thousands of children keyed by name, no list.
    tree = {"name": "Main", "kids": {f"n{i}": {"name": f"n{i}", "type": "Node2D",
                                                 "position": {"x": i, "y": i}} for i in range(3000)}}
    nested, note = GodotServer._clip({"tree": tree})
    assert len(json.dumps(nested)) <= MAX_RESULT_CHARS and "tree" in note
    items, note = GodotServer._clip([{"i": i, "pad": "p" * 100} for i in range(2000)])
    assert len(json.dumps(items)) <= MAX_RESULT_CHARS and "entries omitted" in note
    small, note = GodotServer._clip({"a": [1, 2, 3]})
    assert small == {"a": [1, 2, 3]} and note == ""


def test_res_path_passes_a_file_system_path_through_only(tmp_path):
    """A leading "/" is the project's root (the schema asks for res:// or project-relative); a path that
    is there, or one with a drive or share, is the file system's -- on POSIX both are rooted at "/"."""
    from plugins.godot.server import GodotServer
    script = tmp_path / "tool.gd"
    script.write_text("extends Node\n", encoding="utf-8")
    assert GodotServer._res_path(str(script)) == script.as_posix()
    new_scene = tmp_path / "levels_dir_is_there" / "new_level.tscn"  # saved there next, not there yet
    new_scene.parent.mkdir()
    assert GodotServer._res_path(str(new_scene)) == new_scene.as_posix()
    assert GodotServer._res_path("C:/game/tool.gd") == "C:/game/tool.gd"
    assert GodotServer._res_path("/no_such_folder_here/tool.gd") == "res://no_such_folder_here/tool.gd"
    assert GodotServer._res_path("/main.tscn") == "res://main.tscn"  # its folder would be "/"


def test_res_path_keeps_a_leading_dot_in_a_name():
    from plugins.godot.server import GodotServer
    assert GodotServer._res_path("./.tools.gd") == "res://.tools.gd"
    assert GodotServer._res_path("/levels/one.tscn") == "res://levels/one.tscn"
    assert GodotServer._res_path("res://x.gd") == "res://x.gd"


async def test_an_unreachable_editor_says_what_to_do(server, monkeypatch):
    monkeypatch.setattr(server, "_port", 1)
    result, line = await run_tool(server, "godot_scene", {"action": "tree"})
    assert result["error_type"] == "GodotNotReachable"
    assert "Open the project in the Godot editor" in result["error"]
    assert "setup" in result["error"]
    assert len(line) <= 140


# ── the status lines (tests/plugins/test_status_end_lines.py's rule) ─────

async def test_every_tool_closes_with_a_line_that_carries_a_result(server, addon, project):
    cases = [
        ("godot_status", {}, "fake_project"),
        ("godot_setup", {"project": "shmup"}, "shmup"),
        ("godot_check", {"project": "shmup"}, "parse clean"),
        ("godot_run", {"project": "shmup", "frames": 3}, "ok"),
        ("godot_script", {"project": "shmup", "code": "func _init():\n\tprint('hi')\n"}, "hi"),
        ("godot_export", {"project": "shmup", "preset": "P", "filename": "p.zip"}, "p.zip"),
        ("godot_scene", {"action": "tree"}, "Main"),
        ("godot_node", {"action": "find", "type": "CharacterBody2D"}, "found 1"),
        ("godot_play", {"action": "run"}, "frozen=True"),
        ("godot_observe", {"action": "logs"}, "cursor"),
        ("godot_command", {"command": "get_input_map"}, "actions[1]"),
    ]
    for action, params, expected in cases:
        result, line = await run_tool(server, action, params)
        assert result["status"] == "success", (action, result)
        assert len(line) <= 140, f"{action}: {len(line)} chars — {line}"
        assert expected in line, f"{action}: {expected!r} missing from {line!r}"


@pytest.mark.parametrize("action,params", [
    ("godot_node", {"action": "get", "node_path": "/root/" + "/".join(["Deep"] * 40)}),
    ("godot_command", {"command": "x" * 300}),
    ("godot_observe", {"action": "screenshot", "filename": "../" + "s" * 150 + ".png"}),
    ("godot_check", {"project": "../" + "p" * 150}),
])
async def test_long_inputs_do_not_blow_the_status_row(server, addon, action, params):
    _, line = await run_tool(server, action, params)
    assert len(line) <= 140, f"{action}: {len(line)} chars — {line}"


# ── the vendored addon ───────────────────────────────────────────────────

def test_the_vendored_addon_is_what_setup_and_the_autoload_expect():
    cfg = (ADDON_SOURCE / "plugin.cfg").read_text(encoding="utf-8")
    assert 'version="4.1.11"' in cfg
    assert (ADDON_SOURCE / "game_bridge" / "mcp_game_bridge.gd").is_file(), \
        "the autoload path the addon writes into project.godot must exist"
    assert (ADDON_SOURCE.parent / "LICENSE").is_file()
    vendored = (ADDON_SOURCE.parent / "VENDORED.md").read_text(encoding="utf-8")
    assert "4.1.11" in vendored
