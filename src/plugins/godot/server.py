"""Control Godot 4 on this machine, over two channels.

**Headless (the binary as a subprocess).** ``check`` parses scripts, ``run``
plays a project or scene and reads what it printed, ``script`` runs a
SceneTree script the agent wrote, ``export`` builds with a preset, ``setup``
creates a project and installs the editor addon. All of it works with the
editor closed, and it is where the truth about "does it run" comes from: a
CI machine has nothing else.

**Editor (a WebSocket to the godot-mcp addon).** The addon that ships with
``satelliteoflove/godot-mcp`` (vendored under ``addon/``) listens on
``127.0.0.1:6550`` as soon as the project is open in the editor, and speaks
``{"id", "command", "params"}`` in, one JSON object out. Scene tree, node
edits, playing the game with a frozen clock, injecting input, screenshots of
the running game and its error log all go through it. The upstream MCP server
in front of that addon is a Node process that repackages 88 addon commands as
21 tools; talking to the socket directly costs one function and removes the
process, the same trade the ``blender`` plugin made.

Measured, and the reason the two channels are not one: ``push_error`` in a
running scene does NOT change the process exit code (0), so ``run`` parses
stderr for ``ERROR:`` blocks instead of trusting the exit status; a parse
error under ``--check-only`` does (1). And ``--headless --import`` loads the
addon, which registers its own autoload in ``project.godot`` -- that is what
lets ``setup`` finish without the editor.

**One connection per command.** The addon accepts a single client, so nothing
here holds a socket between calls. It also closes a connection 45 s after the
last INBOUND packet -- a clock that per-call connecting does not reset while
a command runs. The addon caps its own game-side commands below that (28-30
s); the one that can run past it is ``rescan_filesystem`` on a big project,
and that arrives as a ``STALE`` error rather than a raw disconnect.

**Paths are confined.** Projects must live below ``projects_root``, files the
tools write below ``output_directory``. A model-chosen path eventually points
somewhere it should not.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, TYPE_CHECKING

import psutil

from agent_system.paths import data_path
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.utils.path_sandbox import remote_outside

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

#: The vendored editor addon, copied into every project ``setup`` touches.
ADDON_SOURCE = Path(__file__).parent / "addon" / "godot_mcp"
ADDON_RES_PATH = "res://addons/godot_mcp/plugin.cfg"
#: Registered by the addon itself on first editor load (measured with
#: ``--headless --import``); ``setup`` reports whether it landed.
AUTOLOAD_KEY = "MCPGameBridge"
#: SceneTree script that loads every .gd in a project so parse errors surface.
CHECK_SCRIPT = Path(__file__).parent / "scripts" / "check_scripts.gd"

#: Same budget as the blender plugin: a scene tree or a log dump is unbounded.
MAX_RESULT_CHARS = 60_000
#: A game screenshot at max_width 900 is ~1 MB of base64; leave room.
MAX_WS_BYTES = 32 * 1024 * 1024
#: Screenshot width caps, as the schema states them.
MIN_SHOT_WIDTH, MAX_SHOT_WIDTH = 128, 4096
#: One status row (``tools/base.py`` and ``tests/plugins/test_status_end_lines.py``).
STATUS_LINE_LIMIT = 140
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: How Godot starts an error block on stderr. Everything indented after it
#: belongs to that block: an ``at:`` line and, for script errors, a backtrace.
#: Four logger types (error, warning, script, shader), each with a USER
#: variant for push_error/push_warning from GDScript.
_ERROR_HEAD = re.compile(
    r"^(USER SHADER ERROR|SHADER ERROR|USER SCRIPT ERROR|SCRIPT ERROR|"
    r"USER ERROR|ERROR|USER WARNING|WARNING): (.*)$")
#: ``(res://main.gd:4)`` at the end of an ``at:`` or backtrace line.
_LOCATION = re.compile(r"\(([^()]+):(\d+)\)\s*$")
#: Resource loader messages carry their own location up front:
#: ``res://enemy.tscn:4 - ext_resource, invalid UID: …``. That is the place
#: the agent needs, not the frame that called load().
_MESSAGE_LOCATION = re.compile(r"^((?:res|user)://[^\s:]+):(\d+) - ")
_BANNER = re.compile(r"^Godot Engine v\S+ - https://godotengine\.org\s*$")

#: Editor commands that may legitimately take long: a step of 50 s of game
#: time, a script run inside the game, an input sequence with screenshots.
_LONG_COMMANDS = {
    "game_time_step", "game_time_step_until", "exec_run",
    "execute_input_sequence", "type_text", "run_project",
    "capture_game_screenshot", "capture_editor_screenshot",
    # A scan walks the project and imports what it finds.
    "rescan_filesystem",
}


class GodotNotReachable(RuntimeError):
    """Nothing answers on the addon socket. Carries what to do about it."""


class GodotCommandError(RuntimeError):
    """The addon answered with ``status: error``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code


def parse_godot_stderr(text: str) -> list[dict[str, Any]]:
    """Godot's stderr as a list of error blocks (see ``split_godot_stderr``)."""
    return split_godot_stderr(text)[0]


def split_godot_stderr(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Godot's stderr as error blocks, plus every line that belongs to none.

    Each block is ``kind`` (ERROR / SCRIPT ERROR / SHADER ERROR / WARNING …),
    ``message``, and where it happened: ``file``/``line`` prefer the first
    *script* location in the backtrace over the C++ ``at:`` line, because
    ``push_error`` always reports ``variant_utility.cpp`` as its ``at:`` and
    the line the agent needs is ``res://main.gd:4`` one frame below.

    The second element is what the block parser did NOT consume: a crash
    handler's backtrace, ``printerr()`` output, anything unprefixed. A
    crashed game prints no ``ERROR:`` at all, so dropping these lines would
    turn a crash into "exit -1073741819, 0 errors" and nothing else.
    """
    blocks: list[dict[str, Any]] = []
    rest: list[str] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        head = _ERROR_HEAD.match(raw)
        if head:
            current = {"kind": head.group(1), "message": head.group(2).strip(),
                       "file": None, "line": None, "at": None}
            own = _MESSAGE_LOCATION.match(current["message"])
            if own:
                current["file"], current["line"] = own.group(1), int(own.group(2))
                current["_message_location"] = True
            blocks.append(current)
            continue
        if current is None or not raw[:1].isspace():
            # An unindented line ends the block it follows.
            current = None
            if raw.strip():
                rest.append(raw.rstrip())
            continue
        stripped = raw.strip()
        loc = _LOCATION.search(stripped)
        if stripped.startswith("at:") and current["at"] is None:
            current["at"] = stripped[3:].strip()
        if current.get("_message_location"):
            continue
        if loc and current["file"] is None and _is_script(loc.group(1)):
            current["file"], current["line"] = loc.group(1), int(loc.group(2))
        elif loc and current["file"] is None and stripped.startswith("at:"):
            # No script frame yet; keep the engine location so the block is
            # not silent, and let a later script frame replace it.
            current["file"], current["line"] = loc.group(1), int(loc.group(2))
            current["_engine_location"] = True
        elif loc and current.get("_engine_location") and _is_script(loc.group(1)):
            current["file"], current["line"] = loc.group(1), int(loc.group(2))
            current.pop("_engine_location")
    for block in blocks:
        block.pop("_engine_location", None)
        block.pop("_message_location", None)
    return blocks, rest


def _is_script(path: str) -> bool:
    return path.endswith((".gd", ".tscn", ".tres", ".cs"))


_LOAD_FAILED = re.compile(r'^Failed to load script "([^"]+)"')


def dedupe_load_failures(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One parse error, one block.

    Measured: a script that does not parse produces TWO blocks -- the
    ``SCRIPT ERROR`` with the line, and right after it an ``ERROR: Failed to
    load script "res://x.gd"`` whose backtrace points at whoever called
    ``load()``. The second carries nothing the first does not, and counting
    it reports "2 errors" for one mistake. Dropped when its file already has
    a SCRIPT ERROR block.
    """
    parse_failed = {b["file"] for b in blocks if b["kind"].endswith("SCRIPT ERROR") and b["file"]}
    kept = []
    for block in blocks:
        match = _LOAD_FAILED.match(block["message"]) if block["kind"] == "ERROR" else None
        if match and match.group(1) in parse_failed:
            continue
        kept.append(block)
    return kept


#: Editor-mode runs (--import, --export-*) print a progress bar per step,
#: with ANSI colour codes. Noise for the agent, dropped from ``output``.
_PROGRESS = re.compile(r"^\[\s*\d+% \]")


def _is_addon_noise(block: dict[str, Any]) -> bool:
    """Our own addon complaining that the port is taken.

    Every run that loads the project in EDITOR mode -- ``--import`` and
    ``--export-*`` -- starts the bundled EditorPlugin, which tries to bind
    the port a running editor already holds and pushes an error. It says
    nothing about the project, but it lands in the same stderr as real
    errors, so it failed exports outright and made clean imports look
    broken. Kept as a warning; nothing is dropped.
    """
    return block["message"].startswith("[godot-mcp] Failed to start server on ")


def _clean_stdout(stdout: str) -> str:
    lines = [ln for ln in stdout.splitlines()
             if not _BANNER.match(ln) and not _PROGRESS.match(ln)]
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).rstrip()


class GodotServer(SchemaBasedToolServer):
    """Headless Godot runs and a live editor, behind one tool surface."""

    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 server_config: "ToolServerConfig") -> None:
        super().__init__(name, system_config, server_config)
        binary = getattr(server_config, "godot_binary", "godot")
        # A list so a test can substitute an interpreter + stub script.
        self._godot: list[str] = [binary]
        self._host: str = getattr(server_config, "host", "127.0.0.1")
        self._port: int = int(getattr(server_config, "port", 6550))
        self._timeout: float = float(getattr(server_config, "timeout", 60))
        self._long_timeout: float = float(getattr(server_config, "long_timeout", 600))
        self._projects_root: Path = self._abs(
            getattr(server_config, "projects_root", None) or data_path("workspace"))
        self._out_dir: Path = self._abs(
            getattr(server_config, "output_directory", None) or data_path("workspace", "godot"))

    @staticmethod
    def _abs(value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else Path.cwd() / path

    # ── the headless channel ────────────────────────────────────────────

    def _run_binary(self, args: list[str], timeout: float,
                    started: list[subprocess.Popen | None] | None = None) -> dict[str, Any]:
        """The binary, blocking, in a thread. ``timed_out`` keeps partial output.

        A timeout kills the whole process tree, not just the binary: the
        Windows ``_console.exe`` is a wrapper around the real exe, and a game
        or script may start processes of its own. Killing only the direct
        child left those running, and the wait for the pipes they still held
        kept the call from answering until they ended. ``started`` receives
        the process so a cancelled call can kill it too; a ``None`` in it is
        that cancel, arrived before the process was there to kill.
        """
        cmd = [*self._godot, *args]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise GodotNotReachable(
                f"cannot start the Godot binary {self._godot[0]!r} ({exc}). "
                "Set `godot_binary` in the plugin config to the console executable."
            ) from exc
        if started is not None:
            started.append(proc)
            if None in started:
                _kill_tree(proc)
        # Own readers rather than communicate(): on Windows a communicate()
        # that times out raises without the output read so far, so an orphan
        # still holding the pipes after the kill took everything printed.
        out: list[bytes] = []
        err: list[bytes] = []
        readers = [threading.Thread(target=_drain, args=(stream, sink), daemon=True)
                   for stream, sink in ((proc.stdout, out), (proc.stderr, err))]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
        # Bounded, one deadline for both: a process outside the tree may hold
        # the pipes open for good.
        deadline = time.monotonic() + 5
        for reader in readers:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
        return {"exit": None if timed_out else proc.returncode, "timed_out": timed_out,
                "stdout": _text(out), "stderr": _text(err)}

    async def _run_raw(self, args: list[str], timeout: float) -> dict[str, Any]:
        """``_run_binary`` in a thread; a cancelled call kills the process."""
        started: list[subprocess.Popen | None] = []
        try:
            return await asyncio.to_thread(self._run_binary, args, timeout, started)
        except asyncio.CancelledError:
            # The thread cannot be cancelled; the process can. Without this a
            # stopped call left the run going until its timeout.
            # Not waited for: this runs on the event loop.
            started.append(None)
            for proc in [p for p in started if p is not None]:
                _kill_tree(proc, wait_s=0)
            raise

    async def _godot_run(self, args: list[str], timeout: float | None = None) -> dict[str, Any]:
        timeout = timeout or self._long_timeout
        raw = await self._run_raw(args, timeout)
        blocks, rest = split_godot_stderr(raw["stderr"])
        errors = dedupe_load_failures(blocks)
        return {
            "exit": raw["exit"],
            "timed_out": raw["timed_out"],
            "timeout": timeout,
            "output": _clean_stdout(raw["stdout"]),
            "errors": [e for e in errors
                       if "WARNING" not in e["kind"] and not _is_addon_noise(e)],
            "warnings": [e for e in errors
                         if "WARNING" in e["kind"] or _is_addon_noise(e)],
            # stderr the block parser did not consume: crash backtraces,
            # printerr(), anything unprefixed. Never silently dropped.
            "stderr": "\n".join(rest),
        }

    @staticmethod
    def _reason(result: dict[str, Any]) -> str:
        """Why a headless run is not a clean one, for the status row: the
        timeout, else the first error block, else the first unparsed stderr
        line, else the exit code. Empty for a clean run."""
        if result["timed_out"]:
            return f"timed out after {result['timeout']:.0f}s"
        if result["errors"]:
            return GodotServer._first_error(result["errors"])
        if result["stderr"]:
            return result["stderr"].splitlines()[0]
        if result["exit"] not in (0, None):
            return f"exit {result['exit']}"
        return ""

    @staticmethod
    def _clean(result: dict[str, Any]) -> bool:
        return not result["timed_out"] and result["exit"] == 0 and not result["errors"]

    # ── the editor channel ──────────────────────────────────────────────

    async def _call(self, command: str, params: dict[str, Any] | None = None,
                    timeout: float | None = None) -> Any:
        """One command over a fresh connection; the envelope unwrapped.

        The addon answers ``{"id", "status": "success", "result"}`` or
        ``{"id", "status": "error", "error": {"code", "message"}}`` -- one
        failure shape, unlike Blender's addon, but a reply whose id is not
        ours is still treated as a failure rather than handed on.
        """
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, WebSocketException

        rid = uuid.uuid4().hex[:12]
        url = f"ws://{self._host}:{self._port}"
        if timeout is None:
            timeout = self._long_timeout if command in _LONG_COMMANDS else self._timeout
        try:
            ws = await connect(url, open_timeout=5, max_size=MAX_WS_BYTES)
        except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
            raise GodotNotReachable(
                f"no Godot editor is listening on {self._host}:{self._port} "
                f"({exc.__class__.__name__}). Open the project in the Godot editor -- "
                "the godot_mcp addon starts listening when the editor loads it. "
                "If the project has no addon yet, run the setup tool first (editor "
                "closed), then open the project."
            ) from exc
        try:
            await ws.send(json.dumps({"id": rid, "command": command, "params": params or {}}))
            raw = await asyncio.wait_for(ws.recv(), timeout)
        except asyncio.TimeoutError as exc:
            raise GodotCommandError("TIMEOUT", f"{command} gave no answer within {timeout:.0f}s") from exc
        except ConnectionClosed as exc:
            # The addon accepts ONE client and closes a second with 4001 after
            # the handshake -- so that rejection shows up here, on recv, not
            # on connect. 4002 is its idle close: it counts 45 s since the
            # last INBOUND packet, and a command that runs longer than that
            # (rescan_filesystem on a big project) is cut off mid-flight.
            # Everything else means the editor or its game session ended.
            code = getattr(getattr(exc, "rcvd", None), "code", None)
            if code == 4001:
                raise GodotCommandError(
                    "BUSY", f"the addon refused {command}: another client is already "
                    "connected (close 4001). Only one agent can drive one editor.") from exc
            if code == 4002:
                raise GodotCommandError(
                    "STALE", f"the addon closed the connection during {command} as idle "
                    "(close 4002): the command ran past its 45 s activity window.") from exc
            raise GodotCommandError(
                "DISCONNECTED",
                f"the editor closed the connection during {command} ({exc}); "
                "the editor or its game session ended") from exc
        finally:
            await ws.close()

        try:
            reply = json.loads(raw)
        except ValueError as exc:
            raise GodotCommandError(
                "PROTOCOL", f"{command}: the reply is not JSON ({exc}); is something other "
                f"than the godot_mcp addon listening on {self._host}:{self._port}?") from exc
        if not isinstance(reply, dict):
            raise GodotCommandError("PROTOCOL", f"{command}: the reply is not an object")
        if reply.get("id") != rid:
            raise GodotCommandError("PROTOCOL", f"reply id {reply.get('id')!r} is not ours ({rid})")
        if reply.get("status") != "success":
            err = reply.get("error")
            if isinstance(err, dict):
                raise GodotCommandError(str(err.get("code") or ""), str(err.get("message") or err))
            raise GodotCommandError("", str(err or reply))
        return reply.get("result") if reply.get("result") is not None else {}

    # ── shared helpers ──────────────────────────────────────────────────

    def _fail(self, exc: Exception) -> dict[str, Any]:
        kind = type(exc).__name__
        out: dict[str, Any] = {"status": "error", "error": str(exc), "error_type": kind}
        if isinstance(exc, GodotCommandError) and exc.code:
            out["code"] = exc.code
        return out

    def _resolve_project(self, value: str | None, must_exist: bool = True) -> Path:
        """A project directory below ``projects_root``; by name or path."""
        value = (value or "").strip()
        if not value:
            raise ValueError("'project' is required: a name or path below "
                             f"{self._projects_root}")
        # A share or device path is refused on its text: resolving it would
        # already connect to the host (and sign in) before containment could say no.
        if remote_outside(value, self._projects_root, (self._projects_root,)):
            raise ValueError(f"'{value}' resolves outside the projects root ({self._projects_root})")
        candidate = (Path(value) if Path(value).is_absolute()
                     else self._projects_root / value).resolve()
        root = self._projects_root.resolve()
        # A drive or share (C:/..., \\server\share) is absolute wherever it is written: on
        # POSIX it read as a folder named "C:" below the root, and the answer was "no project".
        if (PureWindowsPath(value).drive and not Path(value).is_absolute()) or (
                candidate != root and root not in candidate.parents):
            raise ValueError(f"'{value}' resolves outside the projects root ({root})")
        if must_exist and not (candidate / "project.godot").is_file():
            raise ValueError(f"no project.godot in {candidate}; run the setup tool "
                             "to create a project there")
        return candidate

    def _resolve_output(self, filename: str) -> Path:
        if remote_outside(filename, self._out_dir, (self._out_dir,)):
            raise ValueError(f"'{filename}' resolves outside the output directory ({self._out_dir}); "
                             "pass a plain name or a path below it")
        candidate = (self._out_dir / filename).resolve()
        root = self._out_dir.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"'{filename}' resolves outside the output directory ({root}); "
                             "pass a plain name or a path below it")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    @staticmethod
    def _res_path(script: str) -> str:
        """``res://`` for a project-relative path; res:// and a file system path pass through.

        A file system path is one with a drive or share (C:/..., //server/share/...), or one
        rooted at "/" whose folder is there (a new scene is saved into one). Otherwise a leading
        "/" is the project's root, as the schema has it (res:// or project-relative): on Windows
        it never was absolute, and on POSIX "/levels/one.tscn" went to Godot as a path it did
        not know.
        """
        if remote_outside(script.strip(), Path.cwd(), ()):
            # Godot would open it -- load a scene or script from another host.
            raise ValueError(f"'{script}' is on a network share; give a res:// path")
        script = script.strip().replace("\\", "/")
        path = Path(script)
        if (script.startswith("res://") or PureWindowsPath(script).drive
                or (path.is_absolute() and (path.exists()
                                            or (path.parent != Path(path.anchor) and path.parent.is_dir())))):
            return script
        # Only a leading "./" or "/" is noise; a dot that starts a NAME
        # (".tools.gd") is part of it.
        while script.startswith("./"):
            script = script[2:]
        return "res://" + script.lstrip("/")

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    @staticmethod
    def _short(line: str) -> str:
        """One status row; cut the middle so the reason at the tail survives."""
        if len(line) <= STATUS_LINE_LIMIT:
            return line
        keep = STATUS_LINE_LIMIT - 5
        return line[: keep // 2] + " … " + line[-(keep - keep // 2):]

    @staticmethod
    def _clip(value: Any) -> tuple[Any, str]:
        """The value, and a note saying what was dropped ("" = nothing).

        A dict stays a dict: the longest list inside it is halved until the
        whole fits, then oversized STRING fields are cut -- a screenshot's
        base64 or a script's stdout inside a result would otherwise pass
        through untouched with the flag claiming the opposite (measured:
        ``{"image_base64": 60k chars}`` came back whole). A list is halved
        and the note carries the count, since a list cannot hold one.
        """
        marker = f"…[truncated at {MAX_RESULT_CHARS} characters]"
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if len(text) <= MAX_RESULT_CHARS:
            return value, ""
        if isinstance(value, str):
            return value[:MAX_RESULT_CHARS] + "\n" + marker, f"string cut to {MAX_RESULT_CHARS} characters"
        if isinstance(value, dict):
            trimmed = dict(value)
            notes = []
            lists = sorted((k for k, v in trimmed.items() if isinstance(v, list)),
                           key=lambda k: len(trimmed[k]), reverse=True)
            for key in lists:
                items = trimmed[key]
                # Halve rather than pop: `len // 2` without a floor is what
                # makes this terminate (4 -> 2 -> 1 -> 0).
                while items and len(json.dumps(trimmed, default=str)) > MAX_RESULT_CHARS:
                    items = items[: len(items) // 2]
                    trimmed[key] = items
                    trimmed[f"{key}_omitted"] = len(value[key]) - len(items)
                if f"{key}_omitted" in trimmed:
                    notes.append(f"{key}: {trimmed[f'{key}_omitted']} of {len(value[key])} entries omitted")
                if len(json.dumps(trimmed, default=str)) <= MAX_RESULT_CHARS:
                    return trimmed, "; ".join(notes)
            budget = MAX_RESULT_CHARS // 4
            for key, item in trimmed.items():
                if isinstance(item, str) and len(item) > budget:
                    trimmed[key] = item[:budget] + marker
                    notes.append(f"{key}: string cut to {budget} characters")
            # Still over: a nested structure (a scene tree is a dict of
            # dicts, no list anywhere). Cut the biggest values down to a
            # truncated JSON string, largest first, until the whole fits.
            # The type of that one field changes; the alternative is an
            # unbounded payload behind a flag that says it was bounded.
            by_size = sorted(trimmed, key=lambda k: len(json.dumps(trimmed[k], default=str)), reverse=True)
            for key in by_size:
                if len(json.dumps(trimmed, default=str)) <= MAX_RESULT_CHARS:
                    break
                if isinstance(trimmed[key], (dict, list)):
                    trimmed[key] = json.dumps(trimmed[key], default=str)[:budget] + marker
                    notes.append(f"{key}: serialised and cut to {budget} characters")
            return trimmed, "; ".join(notes) or "oversized scalars kept as they are"
        if isinstance(value, list):
            items = list(value)
            while items and len(json.dumps(items, default=str)) > MAX_RESULT_CHARS:
                items = items[: len(items) // 2]
            return items, f"{len(value) - len(items)} of {len(value)} entries omitted"
        return text[:MAX_RESULT_CHARS] + "\n" + marker, f"value cut to {MAX_RESULT_CHARS} characters"

    def _clipped(self, key: str, value: Any) -> dict[str, Any]:
        """``{key: clipped value, "truncated": bool[, "truncation": note]}``."""
        clipped, note = self._clip(value)
        out: dict[str, Any] = {key: clipped, "truncated": bool(note)}
        if note:
            out["truncation"] = note
        return out

    def _spill_images(self, result: Any, stem: str) -> Any:
        """Pictures inside a reply become files, in place.

        An input sequence with ``screenshot_at_ms`` answers with a
        ``screenshots`` list, and ``capture_*_screenshot`` through the raw
        command passthrough answers with a top-level ``image_base64`` --
        each ~1 MB of base64 that would otherwise land in the model context
        (or be cut to a useless stub by ``_clip``). Written under the output
        directory as ``<stem>_<n>.png``; the entry keeps width/height and
        gets ``path`` instead of the data. Undecodable data is reported in
        the entry, not raised: the rest of the reply is still the answer.
        """
        if not isinstance(result, dict):
            return result
        entries: list[dict[str, Any]] = []
        if isinstance(result.get("image_base64"), str):
            entries.append(result)
        entries += [s for s in (result.get("screenshots") or []) if isinstance(s, dict)
                    and isinstance(s.get("image_base64"), str)]
        for n, entry in enumerate(entries):
            data = entry.pop("image_base64")
            try:
                blob = base64.b64decode(data, validate=True)
                if not blob.startswith(_PNG_MAGIC):
                    raise ValueError("not a PNG")
                target = self._resolve_output(f"{stem}_{n}.png")
                target.write_bytes(blob)
                entry["path"] = str(target)
            except (binascii.Error, ValueError, OSError) as exc:
                entry["image_error"] = f"screenshot data unusable: {exc}"
        return result

    @staticmethod
    def _first_error(errors: list[dict[str, Any]]) -> str:
        """``res://main.gd:4: message`` -- or just the message when the only
        location is an engine source file, which tells the agent nothing."""
        if not errors:
            return ""
        e = errors[0]
        if e.get("file") and _is_script(e["file"]):
            return f"{e['file']}:{e['line']}: {e['message']}"
        return str(e["message"])

    # ── tools: headless ─────────────────────────────────────────────────

    async def status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Both channels: the binary's version, and whether an editor answers."""
        status = params["_status"]
        binary: dict[str, Any] = {"command": self._godot[0]}
        try:
            raw = await self._run_raw(["--version"], 30)
            binary["version"] = (raw["stdout"].strip().splitlines() or [""])[-1]
            binary["ok"] = raw["exit"] == 0 and bool(binary["version"])
        except GodotNotReachable as exc:
            binary.update({"ok": False, "error": str(exc)})

        editor: dict[str, Any] = {"host": self._host, "port": self._port, "reachable": False}
        try:
            info = await self._call("mcp_handshake", {"server_version": "agent_system"}, timeout=10)
            editor.update({"reachable": True, "addon_version": info.get("addon_version"),
                           "godot_version": info.get("godot_version"),
                           "project_name": info.get("project_name"),
                           "project_path": info.get("project_path")})
        except Exception as exc:  # a probe: whatever went wrong IS the answer
            editor["hint"] = str(exc)

        parts = [f"binary {binary.get('version') or 'MISSING'}"]
        parts.append(f"editor: '{editor['project_name']}' addon {editor['addon_version']}"
                     if editor["reachable"] else f"editor: none on {self._host}:{self._port}")
        line = ", ".join(parts)
        if not binary.get("ok"):
            await status.error(self._short(line))
        else:
            await status.end(self._short(line))
        return {"status": "success", "binary": binary, "editor": editor,
                "projects_root": str(self._projects_root),
                "output_directory": str(self._out_dir)}

    async def setup(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a project if needed, install and enable the editor addon."""
        status = params["_status"]
        try:
            project = self._resolve_project(params.get("project"), must_exist=False)
            if not ADDON_SOURCE.is_dir():
                raise RuntimeError(f"vendored addon missing at {ADDON_SOURCE}")
            project.mkdir(parents=True, exist_ok=True)
            cfg = project / "project.godot"
            created = False
            if not cfg.is_file():
                # The feature tag comes from the binary. A binary that does
                # not answer --version is a setup that cannot continue, not
                # one that guesses a version.
                raw = await self._run_raw(["--version"], 30)
                version = raw["stdout"].strip().splitlines()[-1] if raw["stdout"].strip() else ""
                if raw["exit"] != 0 or not version:
                    raise GodotNotReachable(
                        f"{self._godot[0]!r} did not answer --version (exit {raw['exit']}); "
                        "cannot write a project.godot without knowing the engine version")
                cfg.write_text(self._new_project_text(
                    params.get("name") or project.name, params.get("main_scene"), version),
                    encoding="utf-8")
                created = True

            addon_state = self._install_addon(project, bool(params.get("force")))
            enabled = self._enable_plugin(cfg)

            # The addon registers its autoload on first editor load, and
            # --import IS an editor load (measured): after it, project.godot
            # carries autoload/MCPGameBridge without the editor ever opening.
            imported = await self._godot_run(["--headless", "--path", str(project), "--import"])
            registered = f"{AUTOLOAD_KEY}=" in cfg.read_text(encoding="utf-8")
        except Exception as exc:
            await status.error(self._short(f"setup failed: {exc}"))
            return self._fail(exc)

        verdict = ("created" if created else "updated") + f", addon {addon_state}"
        reason = self._reason(imported)
        ok = registered and enabled and not imported["timed_out"]
        if not ok:
            why = ("the import " + reason) if imported["timed_out"] else \
                  f"the addon did not register its autoload ({reason or 'import printed nothing'})"
            await status.error(self._short(f"{project.name}: {verdict}, but {why}"))
        else:
            line = f"{project.name}: {verdict}, autoload registered"
            if imported["errors"]:
                line += f", import: {len(imported['errors'])} errors ({reason})"
            await status.end(self._short(line))
        return {"status": "success" if ok else "error",
                "project": str(project), "created": created, "addon": addon_state,
                "plugin_enabled": enabled, "autoload_registered": registered,
                "import": {"exit": imported["exit"], "timed_out": imported["timed_out"],
                           "errors": imported["errors"], "stderr": imported["stderr"]}}

    @staticmethod
    def _new_project_text(name: str, main_scene: str | None, version: str) -> str:
        # Both land between double quotes. A quote or a line break in them
        # would close the string and write lines of their own into the file
        # (an [autoload] entry, say); refused rather than escaped, because the
        # escape rules of Godot's parser were not measured here.
        for label, value in (("name", name), ("main_scene", main_scene or "")):
            if any(c in '"\\' or ord(c) < 32 for c in value):
                raise ValueError(
                    f"'{label}' must not contain a quote, a backslash or a line break: "
                    f"it is written into project.godot as a quoted string ({value!r})")
        feature = ".".join(version.split(".")[:2])
        lines = ["config_version=5", "", "[application]", "",
                 f'config/name="{name}"']
        if main_scene:
            lines.append(f'run/main_scene="{GodotServer._res_path(main_scene)}"')
        lines += [f'config/features=PackedStringArray("{feature}", "Forward Plus")', ""]
        return "\n".join(lines)

    @staticmethod
    def _addon_version(directory: Path) -> str | None:
        cfg = directory / "plugin.cfg"
        if not cfg.is_file():
            return None
        match = re.search(r'^version="([^"]+)"', cfg.read_text(encoding="utf-8"), re.M)
        return match.group(1) if match else None

    def _install_addon(self, project: Path, force: bool) -> str:
        target = project / "addons" / "godot_mcp"
        wanted = self._addon_version(ADDON_SOURCE)
        present = self._addon_version(target)
        if present == wanted and not force:
            return f"{wanted} present"
        if present is None:
            state = "installed"
        elif present == wanted:
            state = "reinstalled"
        else:
            state = f"updated {present} -> {wanted}"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(ADDON_SOURCE, target)
        return f"{wanted} {state}"

    @staticmethod
    def _enable_plugin(cfg: Path) -> bool:
        """Add the addon to ``[editor_plugins] enabled=...``; True if it is there now."""
        text = cfg.read_text(encoding="utf-8")
        if ADDON_RES_PATH in text:
            return True
        pattern = re.compile(r"^enabled=PackedStringArray\((.*)\)\s*$", re.M)
        section = text.find("[editor_plugins]")
        match = None
        if section >= 0:
            # Bounded to the section: an `enabled=` key in a later
            # third-party section must not become the plugin list.
            next_section = text.find("\n[", section + 1)
            end = len(text) if next_section < 0 else next_section
            match = pattern.search(text, section, end)
        if match:
            inner = match.group(1).strip()
            new_inner = f'{inner}, "{ADDON_RES_PATH}"' if inner else f'"{ADDON_RES_PATH}"'
            text = text[:match.start(1)] + new_inner + text[match.end(1):]
        elif section >= 0:
            # The section exists without the key: put the key INTO it. A
            # second [editor_plugins] at the end would be a duplicate section.
            header_end = text.index("\n", section) + 1 if "\n" in text[section:] else len(text)
            text = text[:header_end] + f'\nenabled=PackedStringArray("{ADDON_RES_PATH}")\n' + text[header_end:]
        else:
            text = text.rstrip("\n") + f'\n\n[editor_plugins]\n\nenabled=PackedStringArray("{ADDON_RES_PATH}")\n'
        cfg.write_text(text, encoding="utf-8")
        # Read back rather than assume: the value the caller reports is
        # what is on disk now, not what was intended.
        return ADDON_RES_PATH in cfg.read_text(encoding="utf-8")

    async def _refresh_editor(self, project: Path) -> tuple[bool, str | None]:
        """Make a running editor notice what the import wrote.

        Best effort by design: the import's verdict comes from the headless
        run, which is the one that knows which project it touched and which
        files failed. The addon's ``rescan_filesystem`` knows neither -- it
        scans whatever project the editor has open and answers ``scanned:
        true`` whether or not a file failed to import -- so it can refresh
        the editor's view and nothing more.
        """
        try:
            info = await self._call("mcp_handshake", {"server_version": "agent_system"}, timeout=10)
        except GodotNotReachable:
            return False, None  # no editor to refresh: not a defect
        except Exception as exc:
            return False, str(exc)
        open_path = str(info.get("project_path") or "").strip()
        try:
            same = bool(open_path) and Path(open_path).resolve() == project.resolve()
        except OSError:
            same = False
        if not same:
            return False, f"the editor has '{info.get('project_name') or open_path}' open"
        try:
            await self._call("rescan_filesystem", {})
        except Exception as exc:
            # STALE is the likely one: the addon drops a socket idle for 45 s
            # while its own scan may run to 60. The scan finishes regardless.
            return False, str(exc)
        return True, None

    async def import_assets(self, params: dict[str, Any]) -> dict[str, Any]:
        """Import everything new or changed in the project.

        A file copied into the project is not usable until it has been
        imported -- and the editor may be closed, or open on a scene that
        does not notice. The headless import is what decides: it takes the
        project as an argument and reports the files that failed.

        A running editor is refreshed afterwards so it sees the result, but
        that refresh cannot fail the import.
        """
        status = params["_status"]
        try:
            project = self._resolve_project(params.get("project"))
            result = await self._godot_run(["--headless", "--path", str(project), "--import"])
        except Exception as exc:
            await status.error(self._short(f"import failed: {exc}"))
            return self._fail(exc)
        errors = result["errors"]
        ok = self._clean(result)
        refreshed, why = await self._refresh_editor(project)
        line = f"{project.name}: import exit {result['exit']}, {len(errors)} errors"
        if refreshed:
            line += ", editor refreshed"
        elif why:
            line += f", editor not refreshed ({why})"
        if ok:
            await status.end(self._short(line))
        else:
            await status.error(self._short(f"{line}: {self._reason(result)}"))
        return {"status": "success", "ok": ok,
                "exit": result["exit"], "timed_out": result["timed_out"],
                "editor_refreshed": refreshed, "editor_note": why,
                "errors": errors, "warnings": result["warnings"], "stderr": result["stderr"]}

    async def check(self, params: dict[str, Any]) -> dict[str, Any]:
        """Parse errors for one script, or for every script in the project."""
        status = params["_status"]
        try:
            project = self._resolve_project(params.get("project"))
            script = (params.get("script") or "").strip()
            if script:
                result = await self._godot_run(
                    ["--headless", "--path", str(project), "--check-only", "-s", self._res_path(script)])
                checked = 1
            else:
                result = await self._godot_run(
                    ["--headless", "--path", str(project), "-s", str(CHECK_SCRIPT)])
                summary = re.search(r"CHECKED (\d+) FAILED (\d+)", result["output"])
                checked = int(summary.group(1)) if summary else 0
                if not summary and self._clean(result):
                    raise RuntimeError("the check script produced no summary: "
                                       + (result["output"] or "no output"))
                # The walker names every script that would not instantiate.
                # One that failed WITHOUT a SCRIPT ERROR block (nothing to
                # parse-locate) still has to show up as an error with its
                # path, or the tool says "1 failed" and cannot say which.
                located = {e["file"] for e in result["errors"] if e.get("file")}
                for failed in re.findall(r"^FAILED (\S+)$", result["output"], re.M):
                    if failed not in located:
                        result["errors"].append({
                            "kind": "CHECK", "file": failed, "line": None, "at": None,
                            "message": "script cannot be instantiated (no parse error "
                                       "printed; abstract class, or a dependency failed)"})
        except Exception as exc:
            await status.error(self._short(f"check failed: {exc}"))
            return self._fail(exc)

        errors = result["errors"]
        ok = self._clean(result)
        label = script or f"{checked} scripts"
        if ok:
            await status.end(self._short(f"{project.name}: {label} parse clean"))
        else:
            await status.error(self._short(
                f"{project.name}: {label}, {len(errors)} errors: {self._reason(result)}"))
        return {"status": "success", "ok": ok, "scripts_checked": checked,
                "errors": errors, "warnings": result["warnings"],
                "exit": result["exit"], "timed_out": result["timed_out"],
                "output": result["output"], "stderr": result["stderr"]}

    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        """Play the project or a scene headless and read stdout/stderr."""
        status = params["_status"]
        try:
            project = self._resolve_project(params.get("project"))
            args = ["--path", str(project)]
            if not params.get("windowed"):
                args.append("--headless")
            # The schema's default is 60 and nothing in the framework applies
            # schema defaults, so it has to be applied here. An explicit 0
            # keeps its meaning: run until the game quits or the timeout.
            frames = 60 if params.get("frames") is None else int(params["frames"])
            if frames > 0:
                args += ["--quit-after", str(frames)]
            scene = (params.get("scene") or "").strip()
            if scene:
                args.append(self._res_path(scene))
            user_args = params.get("args") or []
            if user_args:
                args += ["--", *[str(a) for a in user_args]]
            timeout = float(params.get("timeout") or self._long_timeout)
            result = await self._godot_run(args, timeout)
        except Exception as exc:
            await status.error(self._short(f"run failed: {exc}"))
            return self._fail(exc)

        errors = result["errors"]
        if result["timed_out"]:
            verdict = "timeout"
        elif errors:
            verdict = "errors"
        elif result["exit"] != 0:
            verdict = f"exit {result['exit']}"
        else:
            verdict = "ok"
        target = scene or "main scene"
        lines = len(result["output"].splitlines())
        line = (f"{project.name} {target}: {verdict}, {len(errors)} errors, "
                f"{len(result['warnings'])} warnings, {lines} lines out")
        if result["stderr"]:
            line += f", {len(result['stderr'].splitlines())} stderr lines"
        if verdict != "ok":
            line += f": {self._reason(result)}"
        if verdict == "ok":
            await status.end(self._short(line))
        else:
            await status.error(self._short(line))
        return {"status": "success", "verdict": verdict, "exit": result["exit"],
                "timed_out": result["timed_out"], "errors": errors,
                "warnings": result["warnings"], **self._clipped("output", result["output"]),
                "stderr": self._clip(result["stderr"])[0]}

    async def script(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run a SceneTree script against the project, editor closed or open."""
        status = params["_status"]
        code = params.get("code") or ""
        if not code.strip():
            await status.error("no code given")
            return {"status": "error", "error": "'code' is required", "error_type": "ValueError"}
        tmp: Path | None = None
        try:
            project = self._resolve_project(params.get("project"))
            if not re.search(r"^\s*extends\s", code, re.M):
                code = "extends SceneTree\n" + code
            frames = max(1, int(params.get("frames") or 1))
            with tempfile.NamedTemporaryFile("w", suffix=".gd", prefix="agent_script_",
                                             delete=False, encoding="utf-8") as fh:
                fh.write(code)
                tmp = Path(fh.name)
            result = await self._godot_run(
                ["--headless", "--path", str(project), "-s", str(tmp), "--quit-after", str(frames)],
                float(params.get("timeout") or self._long_timeout))
        except Exception as exc:
            await status.error(self._short(f"script failed: {exc}"))
            return self._fail(exc)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

        # A one-shot script that instantiates a scene and quits leaves nodes
        # alive, and Godot reports that at exit as an ERROR ("N resources
        # still in use at exit"). Measured on the first real script run. It
        # is a leak report for games, not a failure of the script; kept
        # separately so it neither hides nor counts.
        errors = [e for e in result["errors"] if "still in use at exit" not in e["message"]]
        exit_leaks = [e for e in result["errors"] if "still in use at exit" in e["message"]]
        result["errors"] = errors
        first = (result["output"].splitlines() or ["no output"])[0]
        if not self._clean(result):
            await status.error(self._short(
                f"{project.name} script: exit {result['exit']}, {len(errors)} errors: "
                f"{self._reason(result) or first}"))
        else:
            await status.end(self._short(f"{project.name} script: exit 0, {first}"))
        return {"status": "success", "exit": result["exit"], "timed_out": result["timed_out"],
                "errors": errors, "warnings": result["warnings"], "exit_leaks": exit_leaks,
                **self._clipped("output", result["output"]),
                "stderr": self._clip(result["stderr"])[0]}

    async def export(self, params: dict[str, Any]) -> dict[str, Any]:
        """Export with a preset into the output directory."""
        status = params["_status"]
        preset = (params.get("preset") or "").strip()
        if not preset:
            await status.error("no export preset given")
            return {"status": "error", "error": "'preset' is required", "error_type": "ValueError"}
        if preset.startswith("-"):
            # Godot's first argument pass reads the preset as an option of its own
            # (main.cpp: the export flag does not consume it), so "--path" would take over.
            error = f"'preset' must not start with '-' ({preset!r})"
            await status.error(self._short(error))
            return {"status": "error", "error": error, "error_type": "ValueError"}
        try:
            project = self._resolve_project(params.get("project"))
            target = self._resolve_output(params.get("filename") or f"{project.name}_{preset}.export")
            flag = "--export-debug" if params.get("debug") else "--export-release"
            before = self._fingerprint(target)
            result = await self._godot_run(
                ["--headless", "--path", str(project), flag, preset, str(target)])
        except Exception as exc:
            await status.error(self._short(f"export failed: {exc}"))
            return self._fail(exc)

        after = self._fingerprint(target)
        # A changed file is not proof of a finished export: Godot writes the
        # pack incrementally, so a run killed at the timeout or ending with
        # exit 1 leaves a file that is bigger than before and still garbage.
        if after is None or after == before or not self._clean(result):
            written = after is not None and after != before
            reason = self._reason(result) or "nothing written"
            what = "wrote nothing" if not written else "did not finish"
            await status.error(self._short(f"{project.name} export '{preset}' {what}: {reason}"))
            return {"status": "error",
                    "error": f"Godot did not {'write' if not written else 'finish'} {target}: {reason}",
                    "error_type": "MissingOutput" if not written else "ExportFailed",
                    "exit": result["exit"], "timed_out": result["timed_out"],
                    "errors": result["errors"], "output": result["output"],
                    "stderr": result["stderr"]}
        line = f"{project.name} '{preset}' -> {target.name}, {after[1] // 1024} KB"
        if result["warnings"]:
            line += f", {len(result['warnings'])} warnings"
        await status.end(self._short(line))
        return {"status": "success", "path": str(target), "bytes": after[1],
                "errors": result["errors"], "warnings": result["warnings"]}

    # ── tools: editor ───────────────────────────────────────────────────

    async def scene(self, params: dict[str, Any]) -> dict[str, Any]:
        """The scene open in the editor: tree, open, save, reload."""
        status = params["_status"]
        action = (params.get("action") or "tree").strip()
        path = (params.get("scene_path") or "").strip()
        try:
            if action == "tree":
                result = await self._call("get_scene_tree", {
                    "max_depth": int(params.get("max_depth") or 0),
                    "max_children": int(params.get("max_children") or 0)})
            elif action == "open":
                if not path:
                    raise ValueError("'scene_path' is required to open a scene")
                result = await self._call("open_scene", {"scene_path": self._res_path(path)})
            elif action == "save":
                result = await self._call("save_scene", {"path": self._res_path(path)} if path else {})
            elif action == "reload":
                result = await self._call("reload_scene",
                                          {"scene_path": self._res_path(path)} if path else {})
            else:
                raise ValueError(f"unknown action '{action}' (tree, open, save, reload)")
        except Exception as exc:
            await status.error(self._short(f"scene {action} failed: {exc}"))
            return self._fail(exc)

        if action == "tree":
            tree = result.get("tree") if isinstance(result, dict) else None
            root = tree.get("name") if isinstance(tree, dict) else "?"
            line = f"scene tree: root '{root}', {_count_nodes(tree)} nodes"
        else:
            line = f"scene {action}: {path or 'current scene'}"
        await status.end(self._short(line))
        return {"status": "success", "action": action, **self._clipped("result", result)}

    async def node(self, params: dict[str, Any]) -> dict[str, Any]:
        """One node: read its properties, find nodes, set properties, reparent."""
        status = params["_status"]
        action = (params.get("action") or "get").strip()
        node_path = (params.get("node_path") or "").strip()
        try:
            if action == "get":
                if not node_path:
                    raise ValueError("'node_path' is required")
                result = await self._call("get_node_properties", {"node_path": node_path})
            elif action == "find":
                query = {k: params[k] for k in ("name_pattern", "type", "root_path") if params.get(k)}
                result = await self._call("find_nodes", query)
            elif action == "update":
                props = params.get("properties")
                if not node_path or not isinstance(props, dict) or not props:
                    raise ValueError("'node_path' and a non-empty 'properties' object are required")
                result = await self._call("update_node", {"node_path": node_path, "properties": props})
            elif action == "reparent":
                new_parent = (params.get("new_parent_path") or "").strip()
                if not node_path or not new_parent:
                    raise ValueError("'node_path' and 'new_parent_path' are required")
                result = await self._call("reparent_node",
                                          {"node_path": node_path, "new_parent_path": new_parent})
            else:
                raise ValueError(f"unknown action '{action}' (get, find, update, reparent)")
        except Exception as exc:
            await status.error(self._short(f"node {action} {node_path or ''}: {exc}".strip()))
            return self._fail(exc)

        if action == "find":
            line = f"found {result.get('count', len(result.get('matches') or []))} nodes"
        elif action == "get":
            line = f"{node_path}: {len(result.get('properties') or {})} properties"
        elif action == "update":
            line = f"{node_path}: set {', '.join(list(params['properties'])[:4])}"
        else:
            line = f"{node_path} -> {result.get('new_path') or params.get('new_parent_path')}"
        await status.end(self._short(line))
        return {"status": "success", "action": action, **self._clipped("result", result)}

    async def play(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run the game from the editor and drive it: clock, input, scripts."""
        status = params["_status"]
        action = (params.get("action") or "").strip()
        forward: dict[str, Any]
        try:
            if action == "run":
                # None (key sent as null) means "not chosen", i.e. frozen.
                frozen = params.get("frozen")
                forward = {"frozen": True if frozen is None else bool(frozen)}
                if params.get("scene_path"):
                    forward["scene_path"] = self._res_path(params["scene_path"])
                result = await self._call("run_project", forward)
            elif action == "stop":
                result = await self._call("stop_project")
            elif action in ("freeze", "thaw", "status"):
                result = await self._call(f"game_time_{action}")
            elif action == "step":
                forward = _pick(params, "duration_ms", "frames", "inputs", "report")
                if not forward.get("duration_ms") and not forward.get("frames"):
                    raise ValueError("step needs 'duration_ms' or 'frames'")
                result = await self._call("game_time_step", forward)
            elif action == "step_until":
                forward = _pick(params, "until", "max_ms", "inputs", "report")
                if not forward.get("until"):
                    raise ValueError("step_until needs 'until' (a GDScript expression)")
                result = await self._call("game_time_step_until", forward)
            elif action == "input":
                forward = _pick(params, "inputs", "report", "screenshot_at_ms", "screenshot_max_width")
                if "screenshot_max_width" in forward:
                    forward["screenshot_max_width"] = _width(forward["screenshot_max_width"])
                if not forward.get("inputs"):
                    raise ValueError("input needs a non-empty 'inputs' list")
                result = await self._call("execute_input_sequence", forward)
            elif action == "type_text":
                forward = _pick(params, "text", "submit", "delay_ms")
                if not forward.get("text"):
                    raise ValueError("type_text needs 'text'")
                result = await self._call("type_text", forward)
            elif action == "exec":
                source = params.get("source") or ""
                if not source.strip():
                    raise ValueError("exec needs 'source' (GDScript run inside the game)")
                result = await self._call("exec_run", {"source": source})
            else:
                raise ValueError(f"unknown action '{action}' (run, stop, freeze, thaw, status, "
                                 "step, step_until, input, type_text, exec)")
        except Exception as exc:
            await status.error(self._short(f"play {action}: {exc}"))
            return self._fail(exc)

        result = self._spill_images(result, f"play_{action}")
        await status.end(self._short(f"play {action}: {_summary(result)}"))
        return {"status": "success", "action": action, **self._clipped("result", result)}

    async def observe(self, params: dict[str, Any]) -> dict[str, Any]:
        """Look at the game or the editor: screenshot, logs, state, stack."""
        status = params["_status"]
        action = (params.get("action") or "").strip()
        try:
            if action == "screenshot":
                return await self._screenshot(params, status)
            if action == "logs":
                forward = _pick(params, "since", "limit", "severity", "clear")
                result = await self._call("get_log_messages", forward)
                messages = result.get("messages") or []
                total = result.get("match_count")
                count = (f"{len(messages)} of {total}" if isinstance(total, int) and total > len(messages)
                         else str(len(messages)))
                line = (f"{count} log messages since {forward.get('since', 0)}, "
                        f"cursor {result.get('cursor')}")
                if messages:
                    # The addon puts the text of an engine error into `type`
                    # and leaves `message` empty (measured on the Shmup run);
                    # a script's push_error fills `message`.
                    first = messages[0]
                    text = first.get("message") or first.get("type") or first.get("text") or ""
                    line += f": {str(text)[:60]}"
            elif action == "state":
                # What the game bridge actually reads (mcp_game_bridge.gd,
                # get_runtime_state): explicit `paths`, or a selection by
                # group/name/type, plus `include` for extra fields. The
                # {path, fields} "specs" shape belongs to watch_start, not here.
                forward = _pick(params, "paths", "include", "select", "group",
                                "name", "type", "max_nodes")
                result = await self._call("get_runtime_state", forward)
                line = f"runtime state: {_summary(result)}"
            elif action == "stack":
                result = await self._call("get_stack_trace")
                line = f"stack trace: {_summary(result)}"
            elif action == "editor":
                result = await self._call("get_editor_state")
                line = f"editor state: {_summary(result)}"
            else:
                raise ValueError(f"unknown action '{action}' (screenshot, logs, state, stack, editor)")
        except Exception as exc:
            await status.error(self._short(f"observe {action}: {exc}"))
            return self._fail(exc)

        await status.end(self._short(line))
        return {"status": "success", "action": action, **self._clipped("result", result)}

    async def _screenshot(self, params: dict[str, Any], status: Any) -> dict[str, Any]:
        """Game or editor viewport -> PNG under the output directory.

        The addon hands back base64; this returns a PATH, and looking at it is
        ``media_ops_load``'s job -- a picture in every reply would land in the
        context whether the agent needed to look or not.
        """
        source = (params.get("source") or "game").strip()
        name = params.get("filename") or f"{source}.png"
        target = self._resolve_output(name)
        if target.suffix.lower() != ".png":
            target = target.with_suffix(".png")
        forward: dict[str, Any] = {"max_width": _width(params.get("max_width") or 900)}
        if source == "editor":
            if params.get("viewport"):
                forward["viewport"] = params["viewport"]
            result = await self._call("capture_editor_screenshot", forward)
        elif source == "game":
            result = await self._call("capture_game_screenshot", forward)
        else:
            raise ValueError("'source' must be game or editor")
        data = result.get("image_base64") if isinstance(result, dict) else None
        if not data:
            raise RuntimeError(f"the addon returned no image for the {source} screenshot")
        # Decode and check BEFORE touching the file. b64decode without
        # validation turns garbage into b"" -- and writing that would replace
        # the previous screenshot with an empty file, then report "not
        # written" about a file that was, in fact, destroyed.
        try:
            blob = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(f"the addon returned undecodable image data for the "
                               f"{source} screenshot ({exc})") from exc
        if not blob.startswith(_PNG_MAGIC):
            raise RuntimeError(f"the addon's {source} screenshot is not a PNG "
                               f"({len(blob)} bytes, starts with {blob[:8]!r})")
        target.write_bytes(blob)
        size_on_disk = target.stat().st_size
        if size_on_disk != len(blob):
            raise RuntimeError(f"{target} holds {size_on_disk} bytes, expected {len(blob)}")
        size = f"{result.get('width')}x{result.get('height')}"
        warnings = result.get("mesh_warnings") or []
        await status.end(self._short(
            f"{source} -> {target.name}, {size}, {len(blob) // 1024} KB"
            + (f", {len(warnings)} mesh warnings" if warnings else "")))
        return {"status": "success", "path": str(target), "bytes": len(blob),
                "width": result.get("width"), "height": result.get("height"),
                "mesh_warnings": warnings}

    async def command(self, params: dict[str, Any]) -> dict[str, Any]:
        """Any addon command by name. The escape hatch for the other 60."""
        status = params["_status"]
        name = (params.get("command") or "").strip()
        if not name:
            await status.error("no command name given")
            return {"status": "error", "error": "'command' is required", "error_type": "ValueError"}
        forward = params.get("params") or {}
        if not isinstance(forward, dict):
            await status.error(self._short(f"{name}: params must be an object"))
            return {"status": "error", "error": "'params' must be an object", "error_type": "ValueError"}
        try:
            result = await self._call(name, forward,
                                      timeout=self._long_timeout if params.get("long") else None)
        except Exception as exc:
            await status.error(self._short(f"{name}: {exc}"))
            return self._fail(exc)
        result = self._spill_images(result, f"command_{name}")
        await status.end(self._short(f"{name}: {_summary(result)}"))
        return {"status": "success", "command": name, **self._clipped("result", result)}


# ── module helpers ──────────────────────────────────────────────────────

def _kill_tree(proc: subprocess.Popen, wait_s: float = 5) -> None:
    """The process and everything it started, and back once they are gone
    (at most ``wait_s``). Only while the Popen is not reaped: after that its
    pid may already belong to another process."""
    if proc.returncode is not None:
        return
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        children = []
    for p in children:
        try:
            p.kill()
        except psutil.Error:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    # The root through the Popen, not wait_procs: psutil would reap it behind
    # the Popen's back (POSIX), returncode would stay None and the guard above
    # would no longer hold.
    try:
        proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        pass
    psutil.wait_procs(children, timeout=wait_s)


def _width(value: Any) -> int:
    """A screenshot width cap within the schema's 128..4096. The addon scales
    only when the cap is positive and below the picture: 0, a negative value
    or a huge one returned the full resolution."""
    return max(MIN_SHOT_WIDTH, min(MAX_SHOT_WIDTH, int(value)))


def _drain(stream: Any, sink: list[bytes]) -> None:
    # read1: what is there now, so a pipe that never closes still leaves its
    # output in the sink (a text read(n) holds it until n chars or EOF).
    for chunk in iter(lambda: stream.read1(65536), b""):
        sink.append(chunk)


def _text(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")


def _decode(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _pick(params: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: params[k] for k in keys if params.get(k) is not None}


def _count_nodes(tree: Any) -> int:
    if not isinstance(tree, dict):
        return 0
    return 1 + sum(_count_nodes(c) for c in (tree.get("children") or []))


def _summary(result: Any) -> str:
    """A few key=value pairs for the status row; never the whole payload."""
    if not isinstance(result, dict) or not result:
        return "ok" if not result else str(result)[:60]
    parts = []
    for key, value in list(result.items())[:4]:
        if isinstance(value, (dict, list)):
            parts.append(f"{key}[{len(value)}]")
        else:
            parts.append(f"{key}={str(value)[:24]}")
    return ", ".join(parts)
