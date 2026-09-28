"""A machine as an agent -- the agent facade of docs/stategraph_design.md §10.

``type: stategraph_machine`` (the plugin folder ``src/plugins/stategraph_machine``) builds a
``MachineAgent``. Everything that addresses agents -- a SAM spawn, AgentCaller, writer_jobs'
``/events`` -- keeps doing so; the agent runs ONE configured machine through the
StateGraphServer instance and answers with the run's output as a JSON object.

Config (flat keys next to ``type``; ``agent_config`` forbids unknown keys)::

    v6_story_machine:
      type: stategraph_machine
      enabled: true
      machine: v6_story          # the machine id -- never taken from the message
      stategraph: stategraph     # the StateGraphServer instance whose RunManager runs it
      input: text                # text: the message goes into params[task_param]; json: it IS the params
      task_param: task
      params: {}                 # literal params, under the ones from the message
      promote: [story_id]        # output keys copied onto the final event (writer_jobs reads them there)

Request ids: the run key is ``<agent>:<request id>`` (and belongs to the user who started the
run), the run id ``<request id>_sg<n>``. So a re-dispatch of the same request attaches to its
run, resumes it after a crash, or -- once it ended -- answers its outcome again: the output, the
failure, the cancel; only a transient failure starts a new run (service.start_run); cancel, status
lines and cost accounting stay under the caller; and a cancel of the caller reaches the run's token,
which terminates the run (its ``finally`` activities run). A bare task cancel -- the process
stops, the SSE client left -- leaves the run alone: it ends ``interrupted`` and the next
dispatch resumes it. Which run a session belongs to is kept in runs.db, so a continue in any
process finds it. As another agent's tool, every call is a request of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional, Union

from agent_system.core.cancellation import get_cancellation_manager
from agent_system.core.request_context import get_request_user, register_request_user
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder
from agent_system.servers.agent.server import SESSION_LOCKED, Agent
from agent_system.tools.status import current_request_id, status_bus, status_scope
from agent_system.utils.id import short_id

logger = logging.getLogger(__name__)

#: A run another process still holds (its lease is live after a crash): how long to wait for it.
ATTACH_PATIENCE = 90.0
ENDED = ("succeeded", "failed", "cancelled", "interrupted")


class MachineAgent(Agent):
    """A plain Agent whose ``run_events`` runs a machine instead of an LLM loop."""

    def __init__(self, name: str, system_config: Any, server_config: Any, registry: Any):
        super().__init__(name, system_config, server_config, registry)
        # A lazy type must build without its own keys (tests/bootstrap/test_runtime_lazy_contract.py):
        # a config mistake is the answer of every request, not a failed start.
        self.machine_id = str(getattr(server_config, "machine", None) or "")
        self.stategraph_name = str(getattr(server_config, "stategraph", None) or "stategraph")
        self.input_mode = str(getattr(server_config, "input", None) or "text")
        self.task_param = str(getattr(server_config, "task_param", None) or "task")
        params = getattr(server_config, "params", None)
        promote = getattr(server_config, "promote", None)
        self.fixed_params = dict(params) if isinstance(params, dict) else {}
        self.promote = [str(key) for key in promote] if isinstance(promote, list) else []
        problems = [
            "no machine configured (machine: <machine id>)" if not self.machine_id else "",
            f"input must be text or json, not {self.input_mode!r}" if self.input_mode not in ("text", "json") else "",
            f"params must be a mapping, not {type(params).__name__}" if params is not None and not isinstance(params, dict)
            else "",
            f"promote must be a list of output keys, not {type(promote).__name__}"
            if promote is not None and not isinstance(promote, list) else ""]
        self.config_error = f"{name}: " + "; ".join(p for p in problems if p) if any(problems) else None
        if self.config_error:
            logger.warning("%s", self.config_error)

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """As another agent's tool every call is a request of its own: the caller's session is not ours to
        continue, and its request id gets a suffix -- else a second task would get the first run's answer.
        The suffix keeps the run under the caller's prefix (cost, status lines, cancel)."""
        caller = params.get("request_id") or params.get("_request_id")
        own = {key: value for key, value in params.items()
               if key not in ("session_id", "_session_id", "request_id", "_request_id")}
        if caller:
            own["_request_id"] = f"{caller}_{short_id(6)}"
            user = params.get("_user_id") or get_request_user(str(caller), default="")
            if user:  # the run belongs to the caller's user (released with the caller's request tree)
                register_request_user(own["_request_id"], str(user))
        return await super().call(tool, own)

    # ------------------------------------------------------------ the one method every caller uses
    async def run_events(self, task: Union[str, ChatMessage], request_id: Optional[str] = None,
                         session_id: Optional[str] = None, llm_override: Any = None,
                         llm_profile_info_override: Optional[str] = None,
                         use_advanced_model: bool = False) -> AsyncIterator[dict[str, Any]]:
        request_id = request_id or short_id()
        # The role gate Agent.run_events asks first -- this override does not call it.
        refusal = self._refusal_event(request_id, session_id)
        if refusal:
            yield refusal
            yield {"type": "end"}
            return
        session_id = session_id or short_id()
        text = task if isinstance(task, str) else task.get_text_content()
        entry: dict[str, Any] = {"cancel": asyncio.Event(), "message_event": asyncio.Event(), "appended": []}
        self._request_manager.register_active_request(request_id, entry)
        self._session_tracker.register_request(request_id, session_id, entry)
        if not await self._session_tracker.acquire_session_lock(session_id, request_id, timeout=5.0):
            self._request_manager.unregister_active_request(request_id)
            self._session_tracker.unregister_request(request_id)
            yield {"type": "error", "request_id": request_id, "error_type": SESSION_LOCKED,
                   "message": f"session {session_id} is busy with another request; send again when it is done"}
            yield {"type": "end"}
            return
        cancellation = get_cancellation_manager()
        token = cancellation.create_token(request_id)  # a cancel of the caller's prefix reaches us here
        reset = current_request_id.set(request_id)
        forwarder = StatusEventForwarder()
        outcome: dict[str, Any] = {"type": "error", "request_id": request_id,
                                   "message": f"{self.name}: ended without an answer"}
        try:
            await forwarder.start_forwarding(request_id)
            yield {"type": "start", "task": text, "request_id": request_id, "session_id": session_id}
            self._presence_hold(session_id, request_id)
            async with status_scope(status_bus, self.name, request_id) as status:
                async for event in self._drive(text, request_id, session_id, token, entry, status, forwarder):
                    if event.get("type") == "_outcome":
                        outcome = event["event"]
                    else:
                        yield event
            for event in forwarder.get_pending_events():  # our own end line, and the run's last lines
                yield event
        finally:
            try:
                await forwarder.stop_forwarding()
            except Exception:
                logger.debug("status forwarder of %s did not stop cleanly", request_id, exc_info=True)
            cancellation.unregister_request(request_id)
            self._request_manager.unregister_active_request(request_id)
            self._session_tracker.unregister_request(request_id)
            self._presence_release(request_id)
            try:
                current_request_id.reset(reset)
            except ValueError:  # closed from another task (a consumer that stopped early): nothing to restore
                pass
        yield outcome
        yield {"type": "end"}

    # ------------------------------------------------------------ one request
    async def _drive(self, text: str, request_id: str, session_id: str, token: Any, entry: dict[str, Any],
                     status: Any, forwarder: StatusEventForwarder) -> AsyncIterator[dict[str, Any]]:
        async def refuse(message: str, kind: str = "error") -> dict[str, Any]:
            await status.error(message[:140])
            self._remember(session_id, text, message)
            event = {"type": kind, "request_id": request_id}
            event["reason" if kind == "cancelled" else "message"] = message
            return {"type": "_outcome", "event": event}

        if self.config_error:
            yield await refuse(self.config_error)
            return
        server = self._stategraph()
        if server is None:
            yield await refuse(f"{self.name}: no stategraph instance {self.stategraph_name!r} is running")
            return
        user_id = self._user_id(session_id, request_id)
        caller = f"{self.name}:{session_id}"
        stopped = lambda: token.is_cancelled or entry["cancel"].is_set()  # noqa: E731
        run_id = server.run_store.run_of_caller(caller)
        if run_id is None:  # a create -- or the same request again, which start_run answers with its run
            if stopped():
                yield await refuse(f"{self.name}: cancelled before the run started", "cancelled")
                return
            try:
                run_id = await self._start(server, self._params(text), f"{self.name}:{request_id}", request_id,
                                           user_id, session_id, token, entry)
            except _Refused as refused:
                yield await refuse(f"{self.name}: {refused}")
                return
            server.run_store.set_caller(caller, run_id)
        else:  # a continue: never a second run
            await self._load_transcript(session_id, user_id)
            row = server.run_store.get_run(run_id)
            state = (row or {}).get("status")
            if state == "interrupted":
                if stopped():
                    yield await refuse(f"{self.name}: cancelled before run {run_id} resumed", "cancelled")
                    return
                try:
                    await server.service.control_run(run_id, "resume", user_id=user_id)
                except Exception as exc:  # ServiceError: another process holds it, or it no longer loads
                    yield await refuse(f"{self.name}: run {run_id} cannot resume: {exc}")
                    return
            elif state in ("failed", "cancelled") or state is None:
                yield await refuse(f"{self.name}: run {run_id} is {state or 'gone'}; a continue does not start "
                                   "a new run")
                return

        stopping = False
        last_state = None
        while True:
            row, ended_here = await self._tick(server, run_id)
            for event in forwarder.get_pending_events():
                yield event
            if row is None:
                yield await refuse(f"{self.name}: run {run_id} disappeared")
                return
            if row["status"] in ENDED and ended_here:
                break
            if not stopping and stopped():
                stopping = True  # an explicit cancel of this request: the run ends terminated, finally included
                try:  # the panel's path: a run no process runs is resumed into its termination (§3.10)
                    await server.service.control_run(run_id, "terminate", user_id=user_id)
                except Exception:  # idempotent where live; another process's run is refused (409)
                    logger.debug("terminate of %s from the facade not applied", run_id, exc_info=True)
            state = _root_state(row)
            if state and state != last_state:
                last_state = state
                await status.progress(f"{self.machine_id} {run_id}: {state}"[:140])
        yield {"type": "_outcome", "event": {"request_id": request_id,
                                             **await self._answer(row, text, session_id, status)}}

    async def _answer(self, row: dict[str, Any], text: str, session_id: str, status: Any) -> dict[str, Any]:
        run_id, state = row["id"], row.get("final_state") or _root_state(row) or "-"
        if row["status"] == "succeeded":
            output = row.get("output")
            answer = json.dumps(output if isinstance(output, dict) else {"output": output}, ensure_ascii=False)
            self._remember(session_id, text, answer)
            await status.end(f"{self.machine_id} {run_id}: succeeded in {state}, {len(answer)} chars"[:140])
            promoted = {key: output[key] for key in self.promote if isinstance(output, dict) and key in output}
            return {"type": "final", "summary": answer, "run_id": run_id, **promoted}
        error = row.get("error") or {}
        if row["status"] == "cancelled":
            message = f"run {run_id} was cancelled in {state}"
            await status.error(f"{self.machine_id} {run_id}: cancelled in {state}"[:140])
            self._remember(session_id, text, message)
            return {"type": "cancelled", "reason": message, "run_id": run_id}
        if row["status"] == "interrupted":
            message = (f"{self.name}: run {run_id} was interrupted in {state}; send the same request again to "
                       "resume it")
            await status.error(f"{self.machine_id} {run_id}: interrupted in {state}"[:140])
            self._remember(session_id, text, message)
            return {"type": "error", "run_id": run_id, "message": message}
        detail = f"{error.get('type') or 'failed'}: {error.get('message') or ''}".strip()
        output = row.get("output")
        if output not in (None, "", {}, []):  # a failed final's own output says why (e.g. {reason: ...})
            detail += f" -- {json.dumps(output, ensure_ascii=False)[:500]}"
        message = f"{self.name}: run {run_id} failed in {state}: {detail}"
        await status.error(f"{self.machine_id} {run_id}: failed in {state}: {detail}"[:140])
        self._remember(session_id, text, message)
        return {"type": "error", "run_id": run_id, "message": message}

    # ------------------------------------------------------------ helpers
    async def _start(self, server: Any, params: dict[str, Any], run_key: str, request_id: str,
                     user_id: Optional[str], session_id: str, token: Any, entry: dict[str, Any]) -> str:
        """The run of this request: started, attached to, resumed, or the one that ended (its outcome is the answer
        again; start_run decides, also after another process's run we waited for ended meanwhile); wait out
        another process's live lease."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ATTACH_PATIENCE
        while True:
            try:
                started = await server.service.start_run(self.machine_id, params=params, user_id=user_id,
                                                         run_key=run_key, run_id=f"{request_id}_sg{short_id(6)}",
                                                         caller_session=session_id)
            except Exception as exc:  # ServiceError (unknown machine, invalid params, config, key of another user)
                raise _Refused(f"the machine {self.machine_id} did not start: {exc}") from exc
            if started.get("attached") is not False:
                return str(started["run_id"])
            if token.is_cancelled or entry["cancel"].is_set() or loop.time() > deadline:
                raise _Refused(f"run {started['run_id']} of this request runs in {started.get('owner')}; "
                               "send the request there or again later")
            await asyncio.sleep(5.0)

    async def _tick(self, server: Any, run_id: str) -> tuple[Optional[dict[str, Any]], bool]:
        """At most a second of waiting for the run; then its row, and whether it really ended: a run that
        is live here ended when its task did -- a sweep that marked it interrupted meanwhile (the owner's
        loop stalled past its lease) is undone by its heartbeat and is not its end."""
        live = server.run_manager.live.get(run_id)
        if live is not None:
            try:
                await asyncio.wait_for(asyncio.shield(live.task), 1.0)
            except asyncio.TimeoutError:
                pass
        else:  # ended, or another process holds it: read the row, do not spin
            await asyncio.sleep(1.0)
        return server.run_store.get_run(run_id), live is None or live.task.done()

    def _params(self, text: str) -> dict[str, Any]:
        if self.input_mode == "json":
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise _Refused(f"the message must be a JSON object of params: {exc}") from None
            if not isinstance(data, dict):
                raise _Refused(f"the message must be a JSON object of params, not {type(data).__name__}")
            return {**self.fixed_params, **data}
        return {**self.fixed_params, self.task_param: text}

    def _stategraph(self) -> Any:
        """The StateGraphServer instance (its RunManager, so the panel sees and controls these runs)."""
        try:
            server = self.registry.get(self.stategraph_name) if self.registry is not None else None
        except KeyError:
            return None
        if server is None or not hasattr(server, "run_manager"):
            return None
        server.use_registry(self.registry)  # runs resolve their runner agent here
        return server

    def _user_id(self, session_id: str, request_id: str) -> Optional[str]:
        """The run's user: its registered request owner, else the session's stored user.

        The owner first, as Agent._run_denial and the tool user ask: the framework
        writes it. Where one is registered, the stored user is the same one when
        the run starts (Agent._foreign_session refuses another); asked first, the
        owner stays the run's user whatever the session's metadata says later:
        the metadata is state of the session id, shared by every run of it, and
        the registered owner is this run's.
        """
        owner = get_request_user(request_id, default=None)
        if owner:
            return owner
        metadata = self._session_tracker.get_session_metadata(session_id) or {}
        if metadata.get("user_id"):
            return str(metadata["user_id"])
        return get_request_user(request_id)

    async def _load_transcript(self, session_id: str, user_id: Optional[str]) -> None:
        """A continue in a fresh process: the caller saves the tracker's list afterwards, which REPLACES the
        stored transcript -- load it first, or the create's exchange is lost."""
        service = getattr(self, "_session_service", None)
        if service is None or not user_id or self._session_tracker.get_session_messages(session_id):
            return
        try:
            await service.load_and_restore_session(self, user_id, session_id)
        except Exception:
            logger.debug("transcript of %s not loaded", session_id, exc_info=True)

    def _remember(self, session_id: str, text: str, answer: str) -> None:
        """The exchange in the session's transcript -- whatever the outcome: the caller saves the session only
        when it holds messages (an empty list saves nothing)."""
        try:
            messages = list(self._session_tracker.get_session_messages(session_id))
            messages += [ChatMessage(role="user", content=text), ChatMessage(role="assistant", content=answer)]
            self._session_tracker.set_session_messages(session_id, messages)
        except Exception:  # bookkeeping must never fail the answer
            logger.debug("transcript of %s not recorded", session_id, exc_info=True)


class _Refused(Exception):
    """The request cannot run: its message is the error event's."""


def _root_state(row: dict[str, Any]) -> Optional[str]:
    frames = (row.get("view") or {}).get("frames") or []
    return frames[0].get("state") if frames else None


__all__ = ["ATTACH_PATIENCE", "MachineAgent"]
