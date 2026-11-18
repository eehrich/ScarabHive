"""A run as the viewer sees a sub-agent: its stream as run events and status lines.

A sub-agent reaches the chat as ``sub_run`` envelopes (relay_run_event) and the
terminal as status lines under its request id. Claude Code is no Agent, so its
stream-json is translated into the same events, under ids of the same shape:
``<call>_sub_<short>`` for the run and ``<run>_NNN`` for each of its tool calls --
the forms parse_request_id_hierarchy and the chat's copy of it know, which hang
the box under the call and the lines at their depth.

The same events are kept as a background run (agent_system.core.background_runs):
a sub-session of the session whose turn started the run, and a run in the run
events. The relay reaches the chat only while that turn streams; what is kept is
what the chat shows once the turn has ended -- a box under the call, its steps so
far, its state and a Stop.

Nothing here may cost the run: a failure is logged and the view is lost, the run
and its ring go on. A relay that fails costs the box, not the status lines: while
the view runs, the terminal has no other line for Claude Code's tool calls.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from agent_system.core.background_runs import BackgroundRun, Cancel
from agent_system.servers.agent.components.status_forwarding import (StatusEventForwarder,
                                                                     lines_stay_with_the_caller, relay_run_event)
from agent_system.tools.status import StatusPhase, publish_status
from agent_system.utils.id import short_id

from . import run as cli

logger = logging.getLogger(__name__)

#: The name on the box and on the status lines.
AGENT = "claude_code"
#: A tool's arguments or result as relayed. The chat shows a few hundred
#: characters of it; a Write of a whole file would otherwise travel whole.
CAP_PAYLOAD = 2000
#: One status row; the WebUI writes start and end into the same row.
CAP_LINE = 140


def _capped(value: Any) -> Any:
    """Long strings shortened, the shape kept. The chat pretty-prints the
    arguments and results it gets; handed one as a string, it showed a single
    escaped line -- where an agent's tool call shows its fields."""
    if isinstance(value, str):
        return value if len(value) <= CAP_PAYLOAD else f"{value[:CAP_PAYLOAD]}… ({len(value) - CAP_PAYLOAD} more characters)"
    if isinstance(value, dict):
        return {key: _capped(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_capped(item) for item in value]
    return value


def _result_text(content: Any) -> str:
    """A tool_result's content: a string, or a list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return ""


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


class LiveRun:
    """One run's view: opened when the call starts it, fed by the monitor, closed at the end."""

    def __init__(self, forwarder: StatusEventForwarder, root: Path,
                 background: Optional[BackgroundRun] = None) -> None:
        self._forwarder = forwarder
        self._root = root
        self.background = background
        self._step = 0
        self._message: Optional[str] = None
        self._names: list[str] = []
        self._calls = 0
        self._open: dict[str, tuple[str, str, str]] = {}   # tool_use id -> (request id, tool, line)
        self._closed = False
        self._relay_broken = False
        self.label = "Claude Code"   # the CLI that runs, as the end says it

    @property
    def request_id(self) -> str:
        return self._forwarder.request_id or ""

    @property
    def view(self) -> dict:
        """What a process that takes the run over needs to open its view again (reopen)."""
        background = self.background
        return {"run": self.request_id, "session": background.session_id if background else "",
                "parent": background.parent if background else "", "user": background.user_id if background else ""}

    @classmethod
    async def open(cls, call_request_id: Any, task: str, root: Path, *, system_config: Any = None,
                   parent: str = "", user_id: str = "", on_cancel: Optional[Cancel] = None) -> Optional["LiveRun"]:
        """The view of a run started by the call ``call_request_id`` in a turn of session ``parent``; None
        without a call."""
        if not isinstance(call_request_id, str) or not call_request_id:
            return None
        return await cls._open(f"{call_request_id}_sub_{short_id(6)}", task, root, system_config=system_config,
                               parent=parent, user_id=user_id, on_cancel=on_cancel)

    @classmethod
    async def reopen(cls, view: dict, task: str, root: Path, *, system_config: Any = None,
                     on_cancel: Optional[Cancel] = None) -> Optional["LiveRun"]:
        """The view of a run this process takes over (``view`` as the starting one kept it): no turn listens any
        more, its background run goes on in the same sub-session -- written anew, as the watch reads the run's
        stream again from its start."""
        if not view.get("run"):
            return None
        return await cls._open(str(view["run"]), task, root, system_config=system_config,
                               parent=str(view.get("parent") or ""), user_id=str(view.get("user") or ""),
                               on_cancel=on_cancel, session_id=str(view.get("session") or ""))

    @classmethod
    async def _open(cls, run_id: str, task: str, root: Path, *, system_config: Any, parent: str, user_id: str,
                    on_cancel: Optional[Cancel], session_id: str = "") -> Optional["LiveRun"]:
        try:
            forwarder = StatusEventForwarder()
            await forwarder.start_forwarding(run_id)
            # Who listens was settled just now, and relay_run_event reads only
            # that. Left registered, the forwarder would also collect this run's
            # status lines into a list nobody reads, for as long as it runs.
            await forwarder.stop_forwarding()
            background = await BackgroundRun.open(
                system_config=system_config, parent=parent, user_id=user_id, run_id=run_id, agent=AGENT, task=task,
                on_cancel=on_cancel, session_id=session_id) if parent and user_id else None
            if background is None:
                # kept nowhere (one run per model call of a CLI that answers as the model): its tool lines
                # are the calling run's to keep with its step, or a session read back shows none of them
                lines_stay_with_the_caller(run_id)
            live = cls(forwarder, root, background)
            # the box learns that it outlives the turn: once that turn has ended, its state comes from the run
            live._relay({"type": "start", "task": task,
                         **({"background": background.session_id, "background_parent": background.parent}
                            if background else {})})
            return live
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("coding_cli: the live view of a run could not be opened")
            return None

    async def _say(self, event: dict, label: str = "") -> None:
        """One event: kept first, then relayed -- what a reader of the kept run sees live is on disk already."""
        if self.background is not None:
            await self.background.event(event, label)
        self._relay(event)

    def _relay(self, event: dict) -> None:
        try:
            relay_run_event(self._forwarder, event, AGENT)
        except Exception:  # noqa: BLE001 - see the module docstring
            if not self._relay_broken:
                logger.exception("coding_cli: relaying a run's events failed")
            self._relay_broken = True

    async def feed(self, events: list[dict]) -> None:
        """Relay what the stream said since the last feed."""
        if self._closed:
            return
        for event in events:                         # one that breaks costs itself, not the rest
            try:
                kind = event.get("type")
                if kind == "assistant":
                    await self._assistant(cli.message(event), cli.content(event))
                elif kind == "user":
                    await self._results(cli.content(event))
            except Exception:  # noqa: BLE001 - see the module docstring
                logger.exception("coding_cli: relaying a run's stream failed")

    async def _assistant(self, message: dict, content: list) -> None:
        # Claude Code sends one event per content block of a model turn, all
        # with that turn's message id: a new id is a new step.
        message_id = message.get("id")
        if message_id is None or message_id != self._message:
            self._message, self._step, self._names = message_id, self._step + 1, []
            await self._say({"type": "thinking", "step": self._step})
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in ("text", "thinking"):
                text = str(block.get("text") or block.get("thinking") or "")
                if text.strip():
                    await self._say({"type": "reasoning_delta", "step": self._step, "delta": text.rstrip() + "\n"})
            elif kind == "tool_use":
                await self._tool_use(block)

    async def _tool_use(self, block: dict) -> None:
        self._calls += 1
        request_id = f"{self.request_id}_{self._calls:03d}"
        name = str(block.get("name") or "tool")
        line = cli.tool_line(name, block.get("input"), self._root)[:CAP_LINE]
        self._open[str(block.get("id"))] = (request_id, name, line)
        await publish_status(AGENT, line, request_id=request_id, phase=StatusPhase.START)
        args = block.get("input")
        # _capped recurses: arguments nested past what it takes would cost the rest of the batch.
        params = (_capped(args) if cli.nesting(args) <= cli.MAX_NESTING
                  else f"(arguments nested deeper than {cli.MAX_NESTING}, not shown)")
        await self._say({"type": "tool_call", "request_id": request_id, "action": name, "step": self._step,
                         "params": params}, line)
        self._names.append(name)
        # The step's header names what it called, as an agent's step does.
        self._relay({"type": "thinking", "step": self._step, "assistant": {
            "content": "", "tool_calls": [{"function": {"name": n}} for n in self._names]}})

    async def _results(self, content: list) -> None:
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            opened = self._open.pop(str(block.get("tool_use_id")), None)
            if opened is None:
                continue
            request_id, name, line = opened
            text = _result_text(block.get("content"))
            failed = bool(block.get("is_error"))
            await self._say({"type": "tool_result", "request_id": request_id, "action": name,
                             "result": {"is_error": True, "content": _capped(text)} if failed
                             else {"content": _capped(text)}})
            outcome = _first_line(text) or ("failed" if failed else "no output")
            await publish_status(AGENT, f"{line}: {outcome}"[:CAP_LINE], request_id=request_id,
                                 phase=StatusPhase.ERROR if failed else StatusPhase.END)

    def detach(self) -> None:
        """The watch stopped before the run ended: its kept run is left to whoever takes the run over."""
        if not self._closed and self.background is not None:
            self.background.detach()

    async def close(self, record: dict) -> bool:
        """The run ended: its answer or why not, and the end of its stream. False when its background run's end
        was not taken (BackgroundRun.close)."""
        if self._closed:
            return True
        self._closed = True
        said = True
        try:
            # A call the run never got an answer to (killed mid-call): its row
            # would spin for good.
            for request_id, _name, line in self._open.values():
                await publish_status(AGENT, f"{line}: no result, the run ended"[:CAP_LINE],
                                     request_id=request_id, phase=StatusPhase.ERROR)
            self._open.clear()
            if self.background is None:
                lines_stay_with_the_caller(self.request_id, False)   # its last lines are said
            state = record.get("state")
            answer = str(record.get("result") or "")
            why = record.get("note") or _first_line(answer)[:300] or "the run failed"
            if self.background is not None:
                said = await self.background.close(state if state in ("done", "cancelled") else "failed",
                                                   answer, f"{self.label}: {why}")
            if state == "done":
                self._relay({"type": "final", "summary": answer, "content_format": "text"})
            elif state == "cancelled":
                self._relay({"type": "cancelled"})
            else:
                self._relay({"type": "error", "message": f"{self.label}: {why}"})
            self._relay({"type": "end"})
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("coding_cli: closing a run's live view failed")
        return said
