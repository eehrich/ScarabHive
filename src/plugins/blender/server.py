"""Control a locally running Blender over the BlenderMCP addon socket.

Architecture, and why this is not a wrapper around ``blender-mcp``:

The addon that ships with ``blender-mcp`` listens on a TCP port and speaks a
trivial protocol -- send ``{"type": <command>, "params": {...}}`` as JSON, read
one JSON object back. The ``blender-mcp`` package puts a second process in
front of that (a stdio MCP bridge in its own virtualenv, plus a telemetry
package that has to be muzzled through an env var) and exposes 28 tools, of
which 23 are asset-marketplace calls and 5 are actual Blender control.

Talking to the socket directly costs about forty lines, removes the second
process, and lets the tool surface be the five or six things an agent needs.
It also reaches two addon commands the bridge never exposed:
``get_world_state_snapshot`` (selection, frame range, fps) and
``drain_human_activity``.

**One connection per call.** The bridge keeps a persistent socket and documents
the failure that comes with it: two commands overlapping on one wire desync the
response stream until a timeout fires. An agent makes occasional calls, so the
cost of connecting each time is irrelevant next to being immune to that whole
class. Nothing here holds state between calls.

**The addon is the only writer.** Screenshots and exports are produced by
Blender's own process, so this plugin hands it an absolute path and Blender
writes it. Those paths are confined to ``output_directory`` -- Blender would
happily write anywhere, and a model that picks its own path eventually picks a
bad one.
"""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agent_system.tools.schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

#: How the addon spells "only the selected objects" per exporter. Measured
#: against Blender 5.2.0 LTS, because it is spelled five different ways and
#: guessing produces a full-scene export that looks like a success.
#: ``op`` is the operator path, ``selection`` the keyword, ``extra`` what the
#: format needs beyond a filepath.
_EXPORTERS: dict[str, dict[str, Any]] = {
    # glTF and FBX still live in the OLD export_scene namespace...
    "glb": {"op": "export_scene.gltf", "selection": "use_selection",
            "extra": {"export_format": "GLB"}},
    "gltf": {"op": "export_scene.gltf", "selection": "use_selection",
             "extra": {"export_format": "GLTF_SEPARATE"}},
    "fbx": {"op": "export_scene.fbx", "selection": "use_selection", "extra": {}},
    # ...everything else moved to wm.*_export.
    "obj": {"op": "wm.obj_export", "selection": "export_selected_objects", "extra": {}},
    "stl": {"op": "wm.stl_export", "selection": "export_selected_objects", "extra": {}},
    "ply": {"op": "wm.ply_export", "selection": "export_selected_objects", "extra": {}},
    "usd": {"op": "wm.usd_export", "selection": "selected_objects_only", "extra": {}},
    "abc": {"op": "wm.alembic_export", "selection": "selected", "extra": {}},
    # The .blend file itself has no selection concept: it is the whole session.
    #
    # copy=True is NOT optional. Without it, save_as_mainfile makes the file we
    # just wrote the session's CURRENT file: the user's next Ctrl+S in their own
    # Blender would silently write into our output directory instead of their
    # document. Measured -- bpy.data.filepath went from '' to the exported path.
    # With copy=True the file is written and the session keeps its identity.
    "blend": {"op": "wm.save_as_mainfile", "selection": None,
              "extra": {"copy": True}},
}

#: A scene dump of a large file is unbounded; the addon caps its object list at
#: 4000, which is ~21x this budget, so that cap does not protect us.
MAX_RESULT_CHARS = 60_000

#: One status row. Same number as ``tools/base.py`` and the fleet guard in
#: ``tests/plugins/test_status_end_lines.py`` — a Blender object name may be 63
#: characters and ``get_object_info`` returns unrounded floats, so an ordinary
#: asset name overruns this without help.
STATUS_LINE_LIMIT = 140


class BlenderNotReachable(RuntimeError):
    """Raised with an actionable message when nothing answers on the socket."""


class BlenderServer(SchemaBasedToolServer):
    """Scene inspection, bpy execution, screenshots and exports."""

    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 server_config: "ToolServerConfig") -> None:
        super().__init__(name, system_config, server_config)
        self._host: str = getattr(server_config, "host", "127.0.0.1")
        self._port: int = int(getattr(server_config, "port", 9876))
        self._timeout: float = float(getattr(server_config, "timeout", 60))
        # Renders and heavy scripts run for minutes; the plain queries do not.
        self._long_timeout: float = float(getattr(server_config, "long_timeout", 600))
        out = getattr(server_config, "output_directory", "data/workspace/blender")
        self._out_dir: Path = Path(out) if Path(out).is_absolute() else Path.cwd() / out

    # ── the wire ────────────────────────────────────────────────────────

    def _send(self, command: str, params: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        """One command, one connection. Blocking; callers use ``_call``."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            try:
                sock.connect((self._host, self._port))
            except OSError as exc:
                raise BlenderNotReachable(
                    f"nothing is listening on {self._host}:{self._port} ({exc.__class__.__name__}). "
                    "Start Blender, open the N-panel in the 3D viewport, tab "
                    "'BlenderMCP', and click 'Start MCP Server'."
                ) from exc

            sock.sendall(json.dumps({"type": command, "params": params or {}}).encode("utf-8"))

            # The addon does not frame its responses, so the only reliable end
            # marker is the payload parsing as JSON. Same approach the bridge
            # takes; a closed socket before that is a truncated answer.
            buf = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                try:
                    return json.loads(buf.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
            if not buf:
                raise BlenderNotReachable(
                    "Blender accepted the connection but closed it without answering — "
                    "the addon is loaded but its server is likely stopped."
                )
            return json.loads(buf.decode("utf-8"))
        finally:
            sock.close()

    async def _call(self, command: str, params: dict[str, Any] | None = None,
                    timeout: float | None = None) -> dict[str, Any]:
        """Run one command off the event loop and unwrap the addon envelope.

        TWO failure shapes, and the second one is the trap. The addon wraps
        *whatever a handler returns* in ``{"status": "success", "result": …}``
        and only reports ``status: error`` when the handler RAISED. Several
        handlers do not raise -- they return ``{"error": "…"}`` -- so
        ``get_viewport_screenshot`` with no 3D viewport open, or a failing
        ``get_world_state_snapshot``, arrive here looking like a success.

        Checking the envelope alone therefore reports a failed screenshot as a
        good one. Both shapes are unwrapped here, at the single point every
        tool passes through, rather than in six handlers that would each have
        to remember.
        """
        reply = await asyncio.to_thread(
            self._send, command, params, timeout or self._timeout)
        if reply.get("status") != "success":
            raise RuntimeError(str(reply.get("message") or reply.get("error") or reply))
        result = reply.get("result") or {}
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result

    def _fail(self, exc: Exception) -> dict[str, Any]:
        kind = "BlenderNotReachable" if isinstance(exc, BlenderNotReachable) else type(exc).__name__
        return {"status": "error", "error": str(exc), "error_type": kind}

    # ── paths ───────────────────────────────────────────────────────────

    def _resolve_output(self, filename: str) -> Path:
        """A path inside ``output_directory``. Rejects escapes, absolute or not.

        Blender writes wherever it is told, so this is the only thing standing
        between a model-chosen name and an overwritten file somewhere else.
        """
        candidate = (self._out_dir / filename).resolve()
        root = self._out_dir.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(
                f"'{filename}' resolves outside the output directory ({root}); "
                "pass a plain name or a path below it"
            )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int] | None:
        """(mtime_ns, size) if the file is there, else None."""
        try:
            st = path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    @staticmethod
    def _short(line: str) -> str:
        """One status row. The bus caps at 140 and an over-long line is cut
        wherever it happens to land, so the tail — which is where the reason
        lives — is what gets lost. Cut the middle instead."""
        if len(line) <= STATUS_LINE_LIMIT:
            return line
        keep = STATUS_LINE_LIMIT - 5
        return line[: keep // 2] + " … " + line[-(keep - keep // 2):]

    @staticmethod
    def _clip(value: Any) -> tuple[Any, bool]:
        """The value, and whether anything had to be dropped.

        A dict STAYS a dict. Serialising it to a truncated string would change
        the type of the same field between calls — an object on a small scene,
        a string on a large one — and the string would be cut mid-token, so it
        could not be parsed back either. Measured: the budget is reached at
        ~190 objects, which is an ordinary scene, not an exotic one.

        For a mapping the oversized part is a list (``objects``), so entries
        are dropped from the longest one until it fits and a ``…_omitted``
        count records how many. Strings are cut as before.
        """
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if len(text) <= MAX_RESULT_CHARS:
            return value, False

        if isinstance(value, str):
            return value[:MAX_RESULT_CHARS] + f"\n…[truncated at {MAX_RESULT_CHARS} characters]", True

        if isinstance(value, dict):
            trimmed = dict(value)
            lists = sorted(
                (k for k, v in trimmed.items() if isinstance(v, list)),
                key=lambda k: len(trimmed[k]), reverse=True)
            for key in lists:
                items = trimmed[key]
                # Halve rather than pop one at a time: a 4000-entry list would
                # otherwise re-serialise the whole mapping thousands of times.
                # `len // 2` without a floor is what makes this terminate --
                # 4 -> 2 -> 1 -> 0. A `max(1, ...)` floor never reaches empty
                # and spins forever whenever even one entry does not fit.
                while items and len(json.dumps(trimmed, default=str)) > MAX_RESULT_CHARS:
                    items = items[: len(items) // 2]
                    trimmed[key] = items
                    trimmed[f"{key}_omitted"] = len(value[key]) - len(items)
                if len(json.dumps(trimmed, default=str)) <= MAX_RESULT_CHARS:
                    return trimmed, True
            # Even with every list emptied the scalars alone are over budget.
            # Return the mapping anyway: a dict that is too big is still a
            # dict, and changing the type here is the thing this avoids.
            return trimmed, True

        return text[:MAX_RESULT_CHARS] + f"\n…[truncated at {MAX_RESULT_CHARS} characters]", True

    # ── tools ───────────────────────────────────────────────────────────

    async def status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Is Blender reachable, which addon, what can it do."""
        status = params["_status"]
        try:
            info = await self._call("get_addon_info")
        except Exception as exc:
            await status.error(self._short(f"Blender unreachable on {self._host}:{self._port}"))
            return self._fail(exc)

        version = ".".join(str(p) for p in (info.get("addon_version") or []))
        caps = info.get("capabilities") or []
        markets = {}
        for market in ("polyhaven", "sketchfab", "hyper3d", "polypizza", "hunyuan3d"):
            try:
                reply = await self._call(f"get_{market}_status", timeout=10)
                markets[market] = bool(reply.get("enabled"))
            except Exception:
                markets[market] = False

        on = [k for k, v in markets.items() if v]
        await status.end(self._short(
            f"Blender addon {version or '?'} on {self._host}:{self._port}, "
            f"{len(caps)} commands, assets: {', '.join(on) if on else 'none enabled'}"
        ))
        return {"status": "success", "addon_version": version,
                "protocol_version": info.get("protocol_version"),
                "capabilities": caps, "asset_providers": markets,
                "output_directory": str(self._out_dir)}

    async def scene(self, params: dict[str, Any]) -> dict[str, Any]:
        """Scene state: objects, selection, frame range, fps."""
        status = params["_status"]
        try:
            # The richer of the two the addon offers; the bridge only ever
            # exposed get_scene_info, which omits selection and frame range.
            result = await self._call("get_world_state_snapshot")
        except Exception as exc:
            await status.error(self._short(f"scene query failed: {exc}"))
            return self._fail(exc)

        count = result.get("object_count")
        selected = result.get("selected_count")
        await status.end(self._short(
            f"Scene '{result.get('name')}': {count} objects, {selected} selected, "
            f"frame {result.get('frame_current')}/{result.get('frame_end')}"
        ))
        scene, truncated = self._clip(result)
        return {"status": "success", "scene": scene, "truncated": truncated}

    async def object(self, params: dict[str, Any]) -> dict[str, Any]:
        """One object in detail."""
        status = params["_status"]
        name = (params.get("name") or "").strip()
        if not name:
            await status.error("no object name given")
            return {"status": "error", "error": "'name' is required",
                    "error_type": "ValueError"}
        try:
            # The addon's handler signature is get_object_info(name) -- not
            # object_name, which is what the tool parameter is called upstream.
            result = await self._call("get_object_info", {"name": name})
        except Exception as exc:
            await status.error(self._short(f"'{name}': {exc}"))
            return self._fail(exc)

        location = result.get("location")
        # Unrounded floats from the addon plus a 63-character object name
        # overrun the row on their own; round here so the line stays readable.
        if isinstance(location, list):
            location = [round(v, 3) if isinstance(v, (int, float)) else v
                        for v in location]
        await status.end(self._short(
            f"'{name}' is a {result.get('type')} at {location}, "
            f"{len(result.get('materials') or [])} materials"
        ))
        obj, truncated = self._clip(result)
        return {"status": "success", "object": obj, "truncated": truncated}

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run bpy code inside Blender. The escape hatch for everything else."""
        status = params["_status"]
        code = params.get("code") or ""
        if not code.strip():
            await status.error("no code given")
            return {"status": "error", "error": "'code' is required",
                    "error_type": "ValueError"}
        try:
            result = await self._call("execute_code", {"code": code},
                                      timeout=self._long_timeout)
        except Exception as exc:
            await status.error(self._short(f"bpy failed: {exc}"))
            return self._fail(exc)

        output = str(result.get("result") or "").strip()
        first = output.splitlines()[0] if output else "no output"
        await status.end(self._short(f"bpy ran, {len(output)} chars out: {first}"))
        clipped, truncated = self._clip(output)
        return {"status": "success", "output": clipped, "truncated": truncated}

    async def screenshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Render the viewport to a file in the output directory.

        Returns the PATH, not the image: loading it into the model context is
        ``media_ops_load``'s job, and doing it here would put a picture into
        every call whether the agent needed to look or not.
        """
        status = params["_status"]
        name = params.get("filename") or "viewport.png"
        try:
            target = self._resolve_output(name)
            # PNG regardless of what the name says, because that is what the
            # addon writes; a ".jpg" holding PNG bytes is later handed to the
            # model labelled image/jpeg by media_ops, which keys on the suffix.
            if target.suffix.lower() != ".png":
                target = target.with_suffix(".png")
            before = self._fingerprint(target)
            await self._call("get_viewport_screenshot",
                             {"max_size": int(params.get("max_size") or 1000),
                              "filepath": str(target), "format": "png"},
                             timeout=self._long_timeout)
        except Exception as exc:
            await status.error(self._short(f"screenshot failed: {exc}"))
            return self._fail(exc)

        after = self._fingerprint(target)
        # `exists()` alone would accept a leftover from an earlier call, and
        # the agent would reason about a pre-edit frame while everything
        # reported success. It must have CHANGED.
        if after is None or after == before:
            await status.error(self._short(f"{target.name} was not written"))
            return {"status": "error",
                    "error": f"Blender reported success but did not write {target}",
                    "error_type": "MissingOutput"}
        size = after[1]
        if not size:
            await status.error(self._short(f"{target.name} was written empty"))
            return {"status": "error", "error": f"{target} is empty",
                    "error_type": "EmptyRender"}
        await status.end(self._short(f"viewport -> {target.name}, {size // 1024} KB"))
        return {"status": "success", "path": str(target), "bytes": size}

    async def export(self, params: dict[str, Any]) -> dict[str, Any]:
        """Export the scene or the selection to a file in the output directory."""
        status = params["_status"]
        fmt = (params.get("format") or "").strip().lower()
        spec = _EXPORTERS.get(fmt)
        if spec is None:
            await status.error(self._short(f"unknown format '{fmt}'"))
            return {"status": "error",
                    "error": f"format '{fmt}' is not supported",
                    "supported": sorted(_EXPORTERS), "error_type": "ValueError"}

        name = params.get("filename") or f"export.{fmt}"
        selected_only = bool(params.get("selected_only", False))
        try:
            target = self._resolve_output(name)
            kwargs: dict[str, Any] = {"filepath": str(target), **spec["extra"]}
            if spec["selection"] and selected_only:
                kwargs[spec["selection"]] = True
            code = (
                "import bpy\n"
                f"bpy.ops.{spec['op']}(**{kwargs!r})\n"
                "print('exported')\n"
            )
            before = self._fingerprint(target)
            await self._call("execute_code", {"code": code}, timeout=self._long_timeout)
        except Exception as exc:
            await status.error(self._short(f"{fmt} export failed: {exc}"))
            return self._fail(exc)

        after = self._fingerprint(target)
        # Not `exists()`: an operator returning CANCELLED without raising,
        # next to a same-named file from an earlier export, would otherwise
        # report a success that wrote nothing.
        if after is None or after == before:
            await status.error(self._short(f"{fmt} export produced no {target.name}"))
            return {"status": "error",
                    "error": f"Blender reported success but did not write {target}",
                    "error_type": "MissingOutput"}
        size = after[1]
        scope = "selection" if selected_only else "whole scene"
        await status.end(self._short(f"{scope} -> {target.name} ({fmt}), {size // 1024} KB"))
        return {"status": "success", "path": str(target), "bytes": size, "format": fmt}
