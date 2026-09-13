"""Agent watchdog — stage 1: a passive judge at the step boundary.

Concept: ``docs/agent_watchdog_konzept.md``. This is Etappe 1 and nothing more:
every *n* steps a separate, configurable model reads a bounded excerpt of the
run and says whether the recent stretch moved the task forward. The verdict is
logged and shown as a status line. **It is not acted on** — no message is
injected, nothing is aborted. Stage 1 exists to find out whether the verdicts
are worth acting on.

Why the judge runs in the BACKGROUND
------------------------------------
The agent loop awaits every ``post_llm_call`` hook before it continues
(``server.py``, the hook task is awaited while status events are streamed). A
judge call inside the hook would add its full latency to every observed step.
A passive judge has no reason to hold the agent up, so the hook only snapshots
the excerpt and returns; the call happens in a task of its own.

Per-request state is exactly one thing: the running judge task, which removes
itself when it finishes. Nothing is left behind when a run is cancelled — the
leak ``agent_continuation`` has with its counters cannot happen here.

The judge's calls do not pass through the hook pipeline, so they appear in no
``llm_requests`` row and do not distort the observed agent's numbers. Their
usage is written to this plugin's own log instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from agent_system.hooks import HookContext, HookResult, SchemaBasedPluginHook
from agent_system.llm.models import ChatMessage
from agent_system.mcp.status import StatusScope, status_bus

from .window import build_excerpt, parse_verdict

logger = logging.getLogger(__name__)

#: Status lines are cut on the right at this width — result first, subject last.
_STATUS_WIDTH = 140

_INT_SETTINGS = {
    "first_check_step": 10,
    "every_n_steps": 10,
    "task_chars": 4000,
    "spec_chars": 3000,
    "reasoning_chars": 8000,
    "max_tool_calls": 20,
    "judge_timeout_seconds": 120,
}


def _positive_int(raw: Any) -> Optional[int]:
    """``raw`` as an integer >= 1, else None. YAML ``true`` is not a number."""
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


def _model_of(llm: Any) -> Optional[str]:
    """Model name of an LLM client; the clients call the attribute ``model``."""
    return getattr(llm, "model", None) or getattr(llm, "model_name", None)


class AgentWatchdogPlugin(SchemaBasedPluginHook):
    """Hook plugin: judge a running agent every n steps, log the verdict."""

    def __init__(self, plugin_dir: Path | str, mcp_config: Any = None,
                 project_root: Optional[Path] = None) -> None:
        super().__init__(plugin_dir)
        config = {key: spec.get("default") if isinstance(spec, dict) else spec
                  for key, spec in (self.get_config() or {}).items()}
        if mcp_config is not None and getattr(mcp_config, "config", None):
            config.update(mcp_config.config)
        self._config = config
        self._llm_profile = str(config.get("llm_profile") or "turbo")

        root = Path(project_root) if project_root else Path.cwd()
        self._root = root
        log_path = Path(config.get("log_path") or "data/agent_watchdog/verdicts.jsonl")
        self._log_path = log_path if log_path.is_absolute() else root / log_path

        # Each prompt file is read once, not per check: saving it mid-run must
        # not change the verdicts of a run that is already being observed.
        self._prompt = (Path(plugin_dir) / "prompts" / "judge.md").read_text(
            encoding="utf-8")
        self._prompts: Dict[str, str] = {}

        self._running: Dict[str, asyncio.Task] = {}
        self._judge_llm: Any = None

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    def _setting(self, context: HookContext, key: str) -> int:
        """Per-agent value from hooks.overrides, else the plugin value.

        A value that is not a positive integer falls back with a warning: a
        typo must not silently switch the check off or make it fire every step.
        """
        # The plugin value is validated too: an invalid one used to become the
        # fallback itself (0), which is exactly what the fallback is there to stop.
        default = _positive_int(self._config.get(key)) or _INT_SETTINGS[key]
        raw = (context.hook_config or {}).get(key, default)
        value = _positive_int(raw)
        if value is None:
            logger.warning("[AgentWatchdog] '%s': %s=%r is not a positive integer "
                           "— using %d", context.agent_name, key, raw, default)
            return default
        return value

    def _judge_prompt(self, context: HookContext) -> tuple[Dict[str, Any], str]:
        """(log fields, prompt text) — per agent, else plugin, else built-in.

        A path that cannot be read falls back to the built-in prompt with a
        warning, and the log line says so: a judge that ran on the wrong
        criteria must be visible as such.
        """
        configured = (context.hook_config or {}).get("judge_prompt") \
            or self._config.get("judge_prompt")
        if not configured:
            return {}, self._prompt
        configured = str(configured)
        if configured not in self._prompts:
            path = Path(configured)
            path = path if path.is_absolute() else self._root / path
            try:
                self._prompts[configured] = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("[AgentWatchdog] '%s': judge_prompt %s unreadable (%s) "
                               "— using the built-in prompt", context.agent_name, path, exc)
                return {"judge_prompt": configured, "judge_prompt_error": str(exc)[:300]}, \
                    self._prompt
        return {"judge_prompt": configured}, self._prompts[configured]

    def _is_due(self, context: HookContext) -> bool:
        step = context.step or 0
        first = self._setting(context, "first_check_step")
        every = self._setting(context, "every_n_steps")
        return step >= first and (step - first) % every == 0

    # ------------------------------------------------------------------
    # hook
    # ------------------------------------------------------------------

    async def observe_step(self, context: HookContext) -> HookResult:
        """POST_LLM_CALL: snapshot the excerpt when a check is due, judge later."""
        if not self._is_due(context):
            return HookResult(success=True, modified=False)

        request_id = context.request_id
        if request_id in self._running:
            # The previous check of this run is still thinking. Queuing a second
            # one would judge a window that overlaps the first.
            logger.debug("[AgentWatchdog] check for %s step %s skipped: previous "
                         "check still running", request_id, context.step)
            return HookResult(success=True, modified=False)

        assistant = (context.llm_response or {}).get("assistant") or {}
        excerpt = build_excerpt(
            context.messages or [], assistant,
            task_chars=self._setting(context, "task_chars"),
            spec_chars=self._setting(context, "spec_chars"),
            reasoning_chars=self._setting(context, "reasoning_chars"),
            max_tool_calls=self._setting(context, "max_tool_calls"),
        )
        base = {
            "request_id": request_id,
            "session_id": context.session_id,
            "agent": context.agent_name,
            "observed_model": _model_of(context.llm),
            "step": context.step,
        }
        if excerpt is None:
            self._write({**base, "verdict": "continue", "fail_open": "task_not_visible"})
            return HookResult(success=True, modified=False)

        # Which assignment the judge was shown — without it a verdict cannot be
        # checked against the run afterwards.
        base["task_preview"] = excerpt["user_messages"][-1][:200]
        prompt_fields, prompt = self._judge_prompt(context)
        base.update(prompt_fields)
        task = asyncio.create_task(self._judge(context.agent, base, excerpt, prompt,
                                               self._setting(context, "judge_timeout_seconds")))
        self._running[request_id] = task
        task.add_done_callback(lambda _t, rid=request_id: self._running.pop(rid, None))
        return HookResult(success=True, modified=False)

    # ------------------------------------------------------------------
    # judge
    # ------------------------------------------------------------------

    async def _judge(self, agent: Any, base: Dict[str, Any],
                     excerpt: Dict[str, Any], prompt: str, timeout: int) -> None:
        status_id = base["request_id"]
        if agent is not None and hasattr(agent, "next_internal_tool_request_id"):
            # A child id, never the raw one: with the raw id this status line
            # would overwrite the line of the agent it is watching.
            status_id = await agent.next_internal_tool_request_id(base["request_id"])

        record: Dict[str, Any] = dict(base)
        started = time.monotonic()
        async with StatusScope(status_bus, "agent_watchdog", status_id,
                               start_msg=f"checking step {base['step']}") as scope:
            try:
                llm = self._get_judge_llm(agent)
                if llm is None:
                    record.update(verdict="continue", fail_open="no_judge_llm")
                else:
                    record["judge_model"] = _model_of(llm)
                    response = await asyncio.wait_for(llm.chat_tools(
                        messages=[
                            ChatMessage(role="system", content=prompt),
                            ChatMessage(role="user", content=json.dumps(
                                excerpt, ensure_ascii=False, indent=1)),
                        ],
                        tools=[],
                    ), timeout=timeout)
                    response = response if isinstance(response, dict) else {}
                    raw = ((response.get("assistant") or {}).get("content") or "")
                    verdict, fail_open = parse_verdict(raw, excerpt)
                    record.update(verdict)
                    record["usage"] = response.get("usage")
                    record["finish_reason"] = response.get("finish_reason")
                    if fail_open:
                        record["fail_open"] = fail_open
                        record["raw"] = raw[:2000]
            except asyncio.TimeoutError:
                record.update(verdict="continue", fail_open="timeout")
            except asyncio.CancelledError:
                # Only stop_plugin cancels a judge. The line is still written:
                # a check that vanished without a trace reads like one that
                # was never due.
                record.update(verdict="continue", fail_open="cancelled_at_shutdown",
                              latency_ms=int((time.monotonic() - started) * 1000))
                self._write(record)
                await scope.end(f"continue (cancelled_at_shutdown) — step {base['step']}"[:_STATUS_WIDTH])
                raise
            except Exception as exc:  # a dead judge is silent, never fatal
                # Provider refusals land here too; the log line carries them, so
                # a judge that never fires on some content is visible as such.
                record.update(verdict="continue", fail_open="judge_error",
                              error=f"{type(exc).__name__}: {exc}"[:500])
            record["latency_ms"] = int((time.monotonic() - started) * 1000)
            self._write(record)
            # An intervention verdict is not an error: end, never error().
            subject = base["agent"] or "agent"
            note = f" ({record['fail_open']})" if record.get("fail_open") else ""
            line = f"{record['verdict']}{note} — step {base['step']} of {subject}"
            if record.get("reason"):
                line = f"{record['verdict']}{note}: {record['reason']} — {subject}"
            await scope.end(line[:_STATUS_WIDTH])

    async def stop_plugin(self) -> None:
        """Let running judges finish before the process goes away.

        Measured in the first live run: the CLI answered at step 2, the check
        of step 2 started, and the process stopped 0.35 s later — the judge
        was killed and left no line at all. A short-lived process (CLI, job
        worker) would lose exactly its last check that way. So shutdown waits,
        bounded by the judge timeout, and whatever is still running then is
        cancelled and logged as such.
        """
        pending = [task for task in self._running.values() if not task.done()]
        if not pending:
            return
        timeout = float(self._config.get("judge_timeout_seconds",
                                       _INT_SETTINGS["judge_timeout_seconds"]))
        _done, still_running = await asyncio.wait(pending, timeout=timeout)
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)

    def _get_judge_llm(self, agent: Any) -> Any:
        if self._judge_llm is not None:
            return self._judge_llm
        system_config = getattr(agent, "system_config", None)
        if system_config is None:
            return None
        from agent_system.llm.factory import create_llm_from_profile

        ssl_verify = None
        try:
            ssl_verify = system_config.network.ssl_verify
        except Exception:
            pass
        self._judge_llm = create_llm_from_profile(
            system_config, self._llm_profile, ssl_verify=ssl_verify)
        return self._judge_llm

    def _write(self, record: Dict[str, Any]) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.warning("[AgentWatchdog] could not write verdict log %s: %s",
                           self._log_path, exc)
        logger.info("[AgentWatchdog] %s step %s: %s%s", record.get("agent"),
                    record.get("step"), record.get("verdict"),
                    f" ({record['fail_open']})" if record.get("fail_open") else "")
