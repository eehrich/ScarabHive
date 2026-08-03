"""Tool Script Plugin - scripted tool chains without LLM round-trips.

Agents that chain tools pay a token tax on every hop: each result enters the
context and is re-typed as the next call's arguments (slow, expensive, lossy).
This plugin lets the agent submit ONE small Python script that orchestrates
tool calls server-side: intermediate results stay out of the LLM context;
only the script and the final ``result`` cost tokens.

Design: docs/tool_script_plugin_design.md (concept v2, panel-reviewed).

Building blocks:
- Sandbox: ``script_interpreter``'s SafeExecutor, embedded as a library
  (fresh executor per call — chains are self-contained).
- Dispatch: ``Agent.dispatch_tool_call`` (core helper) — same server
  resolution, same allowed/blocked semantics as schema build, same runtime
  param injection as the LLM tool path. What the LLM cannot see, the script
  cannot call.
- ``call_tool(name, **params)`` is seeded into the sandbox: validates params
  against the target tool's JSON schema, dispatches via the agent, sanitizes
  the result through a JSON round-trip, converts ``{"status": "error"}``
  results into raised ``ToolCallError``s (catchable in-script).

Failure contract: the error report lists every executed call (side effects are
never rolled back silently), the failing line, and a size-capped variables
snapshot — so the agent can send a continuation script instead of redoing the
chain.
"""

from __future__ import annotations

import asyncio
import ast
import concurrent.futures
import json
import logging
import fnmatch
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from plugins.script_interpreter.config import ScriptInterpreterConfig
from plugins.script_interpreter.executor import ScriptExecutor

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)

# Callables seeded into the sandbox — excluded from variable snapshots/results.
_SEEDED_NAMES = ("call_tool", "log", "ToolCallError", "parse_json")


def _parse_json(text: Any) -> Any:
    """Sandbox-seeded ``parse_json(text)`` — parse a JSON string to data
    (tools often return JSON as text; the sandbox has no json module)."""
    if not isinstance(text, str):
        raise ToolCallError(
            f"parse_json expects a string, got {type(text).__name__}.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ToolCallError(
            f"parse_json: invalid JSON at line {e.lineno} column {e.colno}: "
            f"{e.msg}") from e

# Small scalar values are reported verbatim in the failure snapshot; anything
# larger only as type + size (the snapshot must not dump the payloads the
# design keeps out of the LLM context).
_SNAPSHOT_VALUE_LIMIT = 200


class ToolCallError(Exception):
    """A tool call inside a script failed (dispatch error, tool status=error,
    timeout, cap exceeded). Seeded into the sandbox so scripts can
    ``except ToolCallError:``."""


class _ScriptAbort(Exception):
    """Internal: abort the script for reasons a script must not catch
    (cancellation, script deadline, max_tool_calls). Deliberately NOT seeded
    into the sandbox namespace — ``except ToolCallError`` won't swallow it."""


class _ScriptContext:
    """Per-run state shared between the worker thread and result shaping."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.log_lines: List[str] = []
        self.n_calls = 0


class ToolScriptServer(SchemaBasedMCPServer):
    """Executes agent-authored Python scripts that chain tool calls in-process."""

    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 mcp_config: "MCPConfig") -> None:
        super().__init__(name, system_config, mcp_config)

        config_dict = getattr(mcp_config, "config", None) or {}
        self._timeout: float = float(config_dict.get("timeout", 120))
        self._per_call_timeout: float = float(config_dict.get("per_call_timeout", 60))
        self._max_tool_calls: int = int(config_dict.get("max_tool_calls", 20))
        self._max_call_result_bytes: int = int(
            config_dict.get("max_call_result_bytes", 512 * 1024))
        self._max_result_chars: int = int(config_dict.get("max_result_chars", 20000))
        self._max_output_length: int = int(config_dict.get("max_output_length", 4000))
        # Optional plugin-level narrowing (fnmatch on the flat tool name).
        # NEVER widens: the agent's allowed/blocked check runs in
        # dispatch_tool_call regardless.
        self._allowed_tools: List[str] = list(config_dict.get("allowed_tools") or [])
        self._blocked_tools: List[str] = list(config_dict.get("blocked_tools") or [])
        # Server-seitige Param-Injection: {tool-pattern: {param: value}}.
        # Für Secrets (write_key, ...), die NIE durchs LLM fließen sollen —
        # LLM-getippte Werte sind transpositions-anfällig (v6-Befund: der
        # Coordinator vertippte den write_key als WC_x9K_mP statt ***REMOVED***).
        # Config gewinnt IMMER über Script-Werte (ein vertippter Key wird
        # ersetzt, nicht nur ergänzt); Injection VOR der Schema-Validierung,
        # damit Scripts den Param komplett weglassen dürfen. Werte kommen aus
        # der Server-Config → trusted; Patterns wie bei allowed_tools (fnmatch).
        raw_inject = config_dict.get("inject_params") or {}
        if not isinstance(raw_inject, dict):
            logger.warning(
                "tool_script '%s': inject_params ignored — expected a "
                "mapping {tool-pattern: {param: value}}, got %s",
                name, type(raw_inject).__name__,
            )
            raw_inject = {}
        self._inject_params: Dict[str, Dict[str, Any]] = {
            str(pattern): dict(extra)
            for pattern, extra in raw_inject.items()
            if isinstance(extra, dict)
        }
        # Review-Befund: still verworfene Einträge machen die Injection
        # lautlos wirkungslos — Scripts lassen den Param bewusst weg und
        # scheitern dann erst zur Laufzeit an der Ziel-Tool-Validierung.
        _dropped = sorted(
            str(p) for p, e in raw_inject.items() if not isinstance(e, dict)
        )
        if _dropped:
            logger.warning(
                "tool_script '%s': inject_params entries dropped (value "
                "must be a mapping {param: value}): %s", name, _dropped,
            )
        if self._inject_params:
            logger.info(
                "tool_script '%s': param injection configured for %d tool "
                "pattern(s): %s", name, len(self._inject_params),
                sorted(self._inject_params),
            )

        # One running script per session — scripts hold a worker thread and
        # per-session serialization bounds pool pressure and state races.
        self._session_locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Tool entrypoint
    # ------------------------------------------------------------------

    async def run_script(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a Python script that chains tool calls via call_tool()."""
        script = params.get("script")
        if not isinstance(script, str) or not script.strip():
            return {"status": "error", "error": "'script' (Python source) is required"}

        if params.get("dry_run"):
            return self._dry_run(script)

        agent = params.get("_agent")
        if agent is None or not hasattr(agent, "dispatch_tool_call"):
            return {"status": "error",
                    "error": "tool_script requires the agent runtime context "
                             "(_agent) — it cannot run standalone."}

        session_id = params.get("_session_id")
        user_id = params.get("_user_id")
        request_id = params.get("_request_id") or params.get("request_id")
        cancellation_token = params.get("_cancellation_token")
        status = params.get("_status")

        timeout = min(float(params.get("timeout") or self._timeout), self._timeout)

        # Bound the lock map in long-running server processes: idle (unlocked)
        # entries carry no state and can be dropped once the map grows.
        if len(self._session_locks) > 500:
            for key in [k for k, v in self._session_locks.items()
                        if not v.locked()][:250]:
                self._session_locks.pop(key, None)
        lock = self._session_locks.setdefault(str(session_id or "global"),
                                              asyncio.Lock())
        if lock.locked():
            return {"status": "error",
                    "error": "Another script is already running in this session — "
                             "wait for it to finish (one script per session)."}

        async with lock:
            loop = asyncio.get_running_loop()
            ctx = _ScriptContext()
            deadline = time.monotonic() + timeout

            call_tool = self._make_call_tool(
                ctx=ctx, agent=agent, loop=loop, deadline=deadline,
                session_id=session_id, user_id=user_id, request_id=request_id,
                cancellation_token=cancellation_token, status=status)

            def log(msg: Any) -> None:
                line = str(msg)
                ctx.log_lines.append(line)
                if status is not None:
                    # Fire-and-forget progress from the worker thread.
                    try:
                        asyncio.run_coroutine_threadsafe(
                            status.progress(f"script: {line}"), loop)
                    except Exception:  # pragma: no cover - progress is best-effort
                        pass

            executor = ScriptExecutor(ScriptInterpreterConfig(
                max_execution_time=timeout,
                max_output_length=self._max_output_length,
            ))
            executor.safe_executor.variables["call_tool"] = call_tool
            executor.safe_executor.variables["log"] = log
            executor.safe_executor.variables["ToolCallError"] = ToolCallError
            # No `import json` in the sandbox — but tools return JSON text all
            # the time (json_store read -> {"json": "..."}). Observed in the
            # first live run: the model immediately reached for json.loads.
            executor.safe_executor.variables["parse_json"] = _parse_json

            try:
                exec_result = await asyncio.to_thread(executor.execute, script)
            except _ScriptAbort as e:
                return self._failure(ctx, executor, str(e), line=None)

        return self._shape_result(ctx, executor, exec_result, status)

    # ------------------------------------------------------------------
    # call_tool bridge (runs in the worker thread)
    # ------------------------------------------------------------------

    def _make_call_tool(self, *, ctx: _ScriptContext, agent: Any,
                        loop: asyncio.AbstractEventLoop, deadline: float,
                        session_id: Optional[str], user_id: Optional[str],
                        request_id: Optional[str], cancellation_token: Any,
                        status: Any):
        def call_tool(name: Any, **tool_params: Any) -> Any:
            # --- pre-flight guards (between hops) ---------------------------
            if cancellation_token is not None and getattr(
                    cancellation_token, "is_cancelled", False):
                raise _ScriptAbort("Request cancelled — script aborted between "
                                   "tool calls.")
            if time.monotonic() > deadline:
                raise _ScriptAbort(
                    f"Script timeout ({self._timeout:.0f}s) exceeded between "
                    f"tool calls.")
            ctx.n_calls += 1
            if ctx.n_calls > self._max_tool_calls:
                raise _ScriptAbort(
                    f"max_tool_calls ({self._max_tool_calls}) exceeded — split "
                    f"the work into multiple scripts.")

            if not isinstance(name, str) or not name.strip():
                raise ToolCallError("call_tool: first argument must be the tool "
                                    "name (string) from your tool list.")
            name_s = name.strip()

            # tool_script instances are never callable from scripts (their only
            # tool is <instance>_run_script — the suffix identifies them all).
            if name_s.endswith("_run_script"):
                raise ToolCallError("call_tool: scripts cannot start other "
                                    "scripts (no recursion).")
            if self._allowed_tools and not any(
                    fnmatch.fnmatch(name_s, p) for p in self._allowed_tools):
                raise ToolCallError(
                    f"call_tool: '{name_s}' is outside this script tool's "
                    f"allowed_tools config.")
            if any(fnmatch.fnmatch(name_s, p) for p in self._blocked_tools):
                raise ToolCallError(
                    f"call_tool: '{name_s}' is blocked by this script tool's "
                    f"config.")

            self._ensure_plain_data(tool_params)
            # Param-Injection (Secrets): NACH dem plain-data-Check der Script-
            # Params, VOR der Schema-Validierung (Scripts dürfen injizierte
            # Pflicht-Params weglassen). Config überschreibt Script-Werte.
            for _pattern, _extra in self._inject_params.items():
                if fnmatch.fnmatch(name_s, _pattern):
                    tool_params.update(_extra)
            self._validate_against_tool_schema(agent, name_s, tool_params)

            child_rid = (f"{request_id}_ts{ctx.n_calls:02d}"
                         if request_id else None)
            entry: Dict[str, Any] = {"tool": name_s, "ok": False}
            ctx.calls.append(entry)
            if status is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        status.progress(
                            f"script: call {ctx.n_calls} — {name_s}"), loop)
                except Exception:  # pragma: no cover
                    pass

            logger.info("Invoking tool %s via tool_script (call %d, rid=%s)",
                        name_s, ctx.n_calls, child_rid)
            t0 = time.monotonic()
            future = asyncio.run_coroutine_threadsafe(
                agent.dispatch_tool_call(
                    name_s, dict(tool_params), session_id=session_id,
                    user_id=user_id, request_id=child_rid),
                loop)
            try:
                raw = future.result(timeout=self._per_call_timeout)
            except concurrent.futures.TimeoutError:
                future.cancel()
                entry["ms"] = int((time.monotonic() - t0) * 1000)
                entry["error"] = f"timed out after {self._per_call_timeout:.0f}s"
                raise ToolCallError(
                    f"{name_s} timed out after {self._per_call_timeout:.0f}s.")
            except Exception as e:
                entry["ms"] = int((time.monotonic() - t0) * 1000)
                entry["error"] = str(e)
                # ToolDispatchError and transport errors arrive here — the
                # message is already agent-actionable.
                raise ToolCallError(f"{name_s} -> {e}") from e
            entry["ms"] = int((time.monotonic() - t0) * 1000)

            # Sanitize to plain data (drops live objects a tool might return)
            # and measure for the per-result cap in one pass.
            try:
                text = json.dumps(raw, ensure_ascii=False, default=str)
            except (TypeError, ValueError) as e:  # pragma: no cover - default=str covers most
                entry["error"] = f"unserializable result: {e}"
                raise ToolCallError(f"{name_s} returned an unserializable "
                                    f"result: {e}")
            if len(text) > self._max_call_result_bytes:
                entry["error"] = f"result too large ({len(text)} chars)"
                raise ToolCallError(
                    f"{name_s} result is too large ({len(text)} chars > "
                    f"{self._max_call_result_bytes}). Have the tool store the "
                    f"payload (e.g. a json_store doc) and work with the "
                    f"reference instead.")
            clean = json.loads(text)

            # Tools signal failure by RETURN VALUE, in two shapes:
            #   {"status": "error", "error": ...}   (json_store, SAM, ...)
            #   {"error": ...}                      (debate_forum, ...)
            # Both become a raised, catchable ToolCallError — a failed call must
            # never look like success to a script.
            if isinstance(clean, dict):
                status_val = clean.get("status")
                error_val = clean.get("error")
                if status_val == "error" or (error_val and status_val is None):
                    entry["error"] = str(error_val)
                    raise ToolCallError(f"{name_s} -> {error_val}")

            entry["ok"] = True
            logger.info("Tool %s returned via tool_script (%d chars)",
                        name_s, len(text))
            return clean

        return call_tool

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @classmethod
    def _ensure_plain_data(cls, value: Any, path: str = "params") -> None:
        """Reject anything that is not plain JSON-compatible data — scripts
        must not smuggle objects into tool params."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return
        if isinstance(value, list):
            for i, v in enumerate(value):
                cls._ensure_plain_data(v, f"{path}[{i}]")
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if not isinstance(k, str):
                    raise ToolCallError(
                        f"call_tool: non-string key in {path}: {k!r}")
                cls._ensure_plain_data(v, f"{path}.{k}")
            return
        raise ToolCallError(
            f"call_tool: {path} contains a non-JSON value "
            f"({type(value).__name__}) — pass only str/int/float/bool/None/"
            f"list/dict.")

    def _validate_against_tool_schema(self, agent: Any, tool_name: str,
                                      tool_params: Dict[str, Any]) -> None:
        """Validate params against the target tool's JSON schema (unknown and
        missing-required params fail HERE with the schema echoed — not three
        hops later with a confusing tool error). Best effort: tools without a
        resolvable schema skip validation (dispatch still authorizes)."""
        schema = self._find_tool_schema(agent, tool_name)
        if not schema:
            return
        properties = schema.get("properties") or {}
        if properties:
            unknown = [k for k in tool_params if k not in properties]
            if unknown:
                raise ToolCallError(
                    f"{tool_name}: unknown parameter(s) {unknown}. Valid "
                    f"parameters: {sorted(properties.keys())}")
        missing = [k for k in (schema.get("required") or [])
                   if k not in tool_params]
        if missing:
            raise ToolCallError(
                f"{tool_name}: missing required parameter(s) {missing}. Valid "
                f"parameters: {sorted(properties.keys())}")
        try:
            import jsonschema
            jsonschema.validate(instance=tool_params, schema=schema)
        except ImportError:  # pragma: no cover - jsonschema is a core dependency
            pass
        except jsonschema.ValidationError as e:
            raise ToolCallError(
                f"{tool_name}: invalid parameters — {e.message} "
                f"(at {'/'.join(str(p) for p in e.absolute_path) or 'root'})")

    @staticmethod
    def _find_tool_schema(agent: Any, tool_name: str) -> Optional[Dict[str, Any]]:
        """The target tool's parameters schema, via the agent's resolution.

        Primary source is the agent's LIVE tool schema -- the exact list the
        LLM runs with, request-scoped, sync to read, and covering every tool
        interface. The old get_tools()-only lookup returned None for hybrid
        plugins (all sub_agent_manager instances), which silently skipped the
        jsonschema validation for exactly those calls.
        """
        try:
            live_schema = getattr(agent, "_current_tools_schema", None) or []
            for tool in live_schema:
                fn = (tool.get("function") or {}) if isinstance(tool, dict) else {}
                if fn.get("name") == tool_name:
                    return fn.get("parameters") or None

            # Fallback for tools outside the live schema (e.g. internal-only
            # dispatch targets): the legacy per-server lookup.
            resolver = getattr(agent, "_resolve_flat_tool_name", None)
            if resolver is None:
                return None
            server, _ = resolver(tool_name)
            get_tools = getattr(server, "get_tools", None)
            if server is None or get_tools is None:
                return None
            for tool in get_tools():
                fn = tool.get("function") or {}
                if fn.get("name") == tool_name:
                    return fn.get("parameters") or None
        except Exception as e:  # pragma: no cover - validation is best effort
            logger.debug("tool_script: schema lookup failed for %s: %s",
                         tool_name, e)
        return None

    # ------------------------------------------------------------------
    # Result shaping
    # ------------------------------------------------------------------

    def _dry_run(self, script: str) -> Dict[str, Any]:
        """Syntax check + the call_tool targets found in the script."""
        try:
            tree = ast.parse(script)
        except SyntaxError as e:
            return {"status": "error",
                    "error": f"Syntax error at line {e.lineno}: {e.msg}"}
        targets = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "call_tool" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                targets.append(str(node.args[0].value))
        return {"status": "ok", "dry_run": True,
                "call_tool_targets": targets,
                "note": "Syntax OK. Sandbox restrictions (no imports, no "
                        "attribute tricks) are enforced at run time."}

    def _snapshot_variables(self, executor: ScriptExecutor) -> Dict[str, Any]:
        """Size-capped variables snapshot for the failure report."""
        snapshot: Dict[str, Any] = {}
        for key, value in executor.safe_executor.variables.items():
            if key in _SEEDED_NAMES or callable(value):
                continue
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                snapshot[key] = f"<{type(value).__name__} — not serializable>"
                continue
            if len(text) <= _SNAPSHOT_VALUE_LIMIT:
                snapshot[key] = value
            else:
                snapshot[key] = (f"<{type(value).__name__}, {len(text)} chars "
                                 f"— omitted>")
        return snapshot

    def _committed_summary(self, ctx: _ScriptContext) -> str:
        ok = sum(1 for c in ctx.calls if c.get("ok"))
        failed = len(ctx.calls) - ok
        return (f"{ok} tool call(s) committed, {failed} failed; nothing after "
                f"the failure was executed. Committed side effects are NOT "
                f"rolled back — do not repeat calls marked ok:true.")

    def _failure(self, ctx: _ScriptContext, executor: ScriptExecutor,
                 error: str, line: Optional[int]) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "status": "error",
            "error": error,
            "calls": ctx.calls,
            "committed_side_effects": self._committed_summary(ctx),
            "variables": self._snapshot_variables(executor),
        }
        if line is not None:
            result["line"] = line
        if ctx.log_lines:
            result["log"] = ctx.log_lines
        return result

    def _shape_result(self, ctx: _ScriptContext, executor: ScriptExecutor,
                      exec_result: Dict[str, Any], status: Any) -> Dict[str, Any]:
        if not exec_result.get("success"):
            err = exec_result.get("error") or {}
            if isinstance(err, dict):
                message = err.get("message") or str(err)
                # A bare KeyError message is just "'teile'" — the type prefix
                # is what makes it diagnosable for the model.
                err_type = err.get("type")
                if err_type and err_type not in message:
                    message = f"{err_type}: {message}"
                line = err.get("line_number")
            else:  # pragma: no cover - executor always returns a dict here
                message, line = str(err), None
            return self._failure(ctx, executor, message, line)

        result_value = executor.safe_executor.variables.get("result")
        if callable(result_value):
            result_value = None
        try:
            result_text = json.dumps(result_value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return self._failure(
                ctx, executor,
                "The script's `result` value is not JSON-serializable.", None)
        if len(result_text) > self._max_result_chars:
            return self._failure(
                ctx, executor,
                f"`result` is too large ({len(result_text)} chars > "
                f"{self._max_result_chars}). Store big payloads (e.g. a "
                f"json_store doc) and return the reference instead.", None)
        # Round-trip so seeded/sandbox values can never leak upstream.
        result_value = json.loads(result_text)

        shaped: Dict[str, Any] = {
            "status": "ok",
            "result": result_value,
            "calls": ctx.calls,
        }
        output = exec_result.get("output")
        if output:
            shaped["output"] = output[:self._max_output_length]
        if ctx.log_lines:
            shaped["log"] = ctx.log_lines
        return shaped
