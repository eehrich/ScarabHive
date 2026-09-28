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
      on_wait: block             # ask: a wait state asks in the conversation (as a tool it blocks)

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
import re
from typing import Any, AsyncIterator, Optional, Union

from agent_system.core.cancellation import get_cancellation_manager
from agent_system.core.request_context import get_request_user, register_request_user
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder
from agent_system.servers.agent.server import Agent
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
        # ask: a run that waits for an event ends the turn with the question; the next message answers it.
        # block (the default): the request waits until the run ends -- a caller such as writer_jobs takes any
        # answer for the result
        self.on_wait = str(getattr(server_config, "on_wait", None) or "block")
        params = getattr(server_config, "params", None)
        promote = getattr(server_config, "promote", None)
        self.fixed_params = dict(params) if isinstance(params, dict) else {}
        self.promote = [str(key) for key in promote] if isinstance(promote, list) else []
        self._as_tool: set[str] = set()  # the requests of call(): another agent's tool has no conversation to ask in
        problems = config_problems(server_config)
        self.config_error = f"{name}: " + "; ".join(problems) if problems else None
        if self.config_error:
            logger.warning("%s", self.config_error)

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """As another agent's tool every call is a request of its own: the caller's session is not ours to
        continue, and its request id gets a suffix -- else a second task would get the first run's answer.
        The suffix keeps the run under the caller's prefix (cost, status lines, cancel)."""
        caller = params.get("request_id") or params.get("_request_id")
        own = {key: value for key, value in params.items()
               if key not in ("session_id", "_session_id", "request_id", "_request_id")}
        own["_request_id"] = f"{caller}_{short_id(6)}" if caller else short_id()
        if caller:
            user = params.get("_user_id") or get_request_user(str(caller), default="")
            if user:  # the run belongs to the caller's user (released with the caller's request tree)
                register_request_user(own["_request_id"], str(user))
        # A question would end the call, and the reply would come as a new call -- a new run: a wait blocks
        self._as_tool.add(own["_request_id"])
        try:
            return await super().call(tool, own)
        finally:
            self._as_tool.discard(own["_request_id"])

    # ------------------------------------------------------------ the one method every caller uses
    async def run_events(self, task: Union[str, ChatMessage], request_id: Optional[str] = None,
                         session_id: Optional[str] = None, llm_override: Any = None,
                         llm_profile_info_override: Optional[str] = None,
                         use_advanced_model: bool = False) -> AsyncIterator[dict[str, Any]]:
        request_id = request_id or short_id()
        session_id = session_id or short_id()
        text = task if isinstance(task, str) else task.get_text_content()
        entry: dict[str, Any] = {"cancel": asyncio.Event(), "message_event": asyncio.Event(), "appended": []}
        self._request_manager.register_active_request(request_id, entry)
        self._session_tracker.register_request(request_id, session_id, entry)
        if not await self._session_tracker.acquire_session_lock(session_id, request_id, timeout=5.0):
            self._request_manager.unregister_active_request(request_id)
            self._session_tracker.unregister_request(request_id)
            yield {"type": "error", "request_id": request_id,
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
        ask = self.on_wait == "ask" and request_id not in self._as_tool
        run_id = server.run_store.run_of_caller(caller)
        row = server.run_store.get_run(run_id) if run_id is not None else None
        if row is not None and not server.sees_run(user_id, row.get("user_id")):
            yield await refuse(f"{self.name}: session {session_id} holds another user's run")
            return
        asked: Optional[frozenset[tuple[str, int]]] = None  # the waits a reply of this request answered
        since: Optional[int] = None  # the journal's last row before the reply: what the run made of it comes after
        if run_id is None or (row or {}).get("run_key") == f"{self.name}:{request_id}":
            # a create -- or the same request again, which start_run answers with its run (or a new one after a
            # transient failure): never the continue path's "no new run"
            if row is not None:  # the session has an exchange already: keep it
                await self._load_transcript(session_id, user_id)
            if stopped():
                yield await refuse(f"{self.name}: cancelled before the run started", "cancelled")
                return
            try:
                run_id = await self._start(server, self._params(text, server), f"{self.name}:{request_id}", request_id,
                                           user_id, session_id, token, entry)
            except _Refused as refused:
                yield await refuse(f"{self.name}: {refused}")
                return
            server.run_store.set_caller(caller, run_id)
        else:  # a continue: never a second run
            await self._load_transcript(session_id, user_id)
            state = (row or {}).get("status")
            if ask and state == "waiting" and run_id not in server.run_manager.live:
                # a wait no process here runs -- agent-cli is a process per message, or the API restarted before
                # the sweep: resume it (refused while another process's lease holds it), then answer its wait
                state = "interrupted"
            if state == "interrupted":
                if stopped():
                    yield await refuse(f"{self.name}: cancelled before run {run_id} resumed", "cancelled")
                    return
                try:
                    await server.service.control_run(run_id, "resume", user_id=user_id)
                except Exception as exc:  # ServiceError: another process holds it, or it no longer loads
                    yield await refuse(f"{self.name}: run {run_id} cannot resume: {exc}")
                    return
                if ask:  # the replay stands in the wait again: the message answers it, not a question anew
                    row = await self._settled(server, run_id, stopped)
                    state = row["status"]
            elif state in ("failed", "cancelled") or state is None:
                yield await refuse(f"{self.name}: run {run_id} is {state or 'gone'}; a continue does not start "
                                   "a new run")
                return
            # the message answers the question the run asked -- a timer that takes no event is waited out below
            if ask and state == "waiting" and _waits(row):
                since = _last_seq(server, run_id)
                problem = self._reply(server, row, text, user_id)
                if problem is not None:
                    yield {"type": "_outcome", "event": {"request_id": request_id,
                                                         **await self._ask(server, row, text, session_id, status,
                                                                           problem)}}
                    return
                asked = _waits(row)  # the wait answered: the next question is a new wait, not this one again

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
            # a wait with events to answer -- a timer state (after) that takes none waits out its time
            if ask and row["status"] == "waiting" and _waits(row) and _waits(row) != asked:
                yield {"type": "_outcome", "event": {"request_id": request_id,
                                                     **await self._ask(server, row, text, session_id, status,
                                                                       since=since)}}
                return
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

    async def _ask(self, server: Any, row: dict[str, Any], text: str, session_id: str, status: Any,
                   problem: Optional[str] = None, since: Optional[int] = None) -> dict[str, Any]:
        """The turn's answer while the run waits: what for, which events it takes, and how to reply -- and, after
        a reply, why it did not move the run (``since``: the journal's last row before it)."""
        waits = [frame for frame in (row.get("view") or {}).get("frames") or [] if frame.get("accepts")]
        names = sorted({name for frame in waits for name in frame["accepts"]})
        events, states = _declared(row, [(frame.get("machine"), frame.get("state")) for frame in waits])
        lines = [f"Not sent: {problem}." if problem else "", *_discarded(server, row["id"], since),
                 f"{self.machine_id} waits for an answer in {', '.join(f'{name!r}' for name, _ in states) or 'a wait state'}."]
        lines += [f"{name}: {description}" for name, description in states if description]
        lines.append("It takes:")
        shared = False
        for name in names:
            spec = events.get(name) or {}
            data = f" (data: {json.dumps(spec['data'], ensure_ascii=False)[:300]})" if spec.get("data") else ""
            frames = [f"{frame.get('prefix', '')!r} in {frame.get('state')!r}" for frame in waits
                      if name in frame["accepts"]]
            shared = shared or len(frames) > 1
            where = f" (in frames {', '.join(frames)})" if len(frames) > 1 else ""
            lines.append(f"- {name}{': ' + spec['description'] if spec.get('description') else ''}{data}{where}")
        lines.append('Reply with the event\'s name, or with JSON {"event": "<name>", "data": ...} to send data along'
                     + ('; name the frame of an event several frames take: {"event": ..., "frame": "<frame>"}.'
                        if shared else '.'))
        question = "\n".join(line for line in lines if line)
        self._remember(session_id, text, question)
        await status.end(f"{self.machine_id} {row['id']}: waits for {', '.join(names)}"[:140])
        return {"type": "final", "summary": question, "run_id": row["id"],
                "waiting": {"states": [name for name, _ in states], "events": names}}

    def _reply(self, server: Any, row: dict[str, Any], text: str, user_id: Optional[str]) -> Optional[str]:
        """Send the event a reply names; why not, when it names none the run takes or the run refuses it."""
        accepts = sorted({name for frame in (row.get("view") or {}).get("frames") or []
                          for name in frame.get("accepts") or []})
        found = _event_of(text, accepts)
        if isinstance(found, str):
            return found
        name, data, frame = found
        try:
            answer = server.service.send_event(row["id"], name, data, frame, user_id=user_id)
        except Exception as exc:  # ServiceError: the run is gone, another user's, ...
            return str(exc)
        return None if answer.get("accepted") else str(answer.get("reason") or "the run did not take it")

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
                takes = self._takes(server) if getattr(exc, "status", None) == 422 else ""  # the request's fault
                raise _Refused(f"the machine {self.machine_id} did not start: {exc}{takes}") from exc
            if started.get("attached") is not False:
                return str(started["run_id"])
            if token.is_cancelled or entry["cancel"].is_set() or loop.time() > deadline:
                raise _Refused(f"run {started['run_id']} of this request runs in {started.get('owner')}; "
                               "send the request there or again later")
            await asyncio.sleep(5.0)

    @staticmethod
    async def _settled(server: Any, run_id: str, stopped: Any) -> dict[str, Any]:
        """The run's row once it no longer runs (it waits, pauses or ended) -- or when the request stops."""
        while True:
            row = await server.run_manager.wait(run_id, timeout=1.0)
            if row["status"] != "running" or stopped():
                return row

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

    def _params(self, text: str, server: Any = None) -> dict[str, Any]:
        if self.input_mode == "json":
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise _Refused(f"the message must be a JSON object of params: {exc}{self._takes(server)}") from None
            if not isinstance(data, dict):
                raise _Refused(f"the message must be a JSON object of params, not {type(data).__name__}"
                               f"{self._takes(server)}")
            return {**self.fixed_params, **data}
        return {**self.fixed_params, self.task_param: text}

    def _takes(self, server: Any) -> str:
        """'; it takes: ...' -- the machine's params, what a caller has to send (empty when they cannot be read)."""
        try:
            tree = server.machines.load(self.machine_id)
            declared = tree.files[tree.root].spec.params
        except Exception:
            return ""
        given = set(self.fixed_params) | ({self.task_param} if self.input_mode == "text" else set())
        described = [f"{name} ({spec.type}{', required' if spec.required else ''}"
                     f"{', set by the agent' if name in given else ''})"
                     f"{' -- ' + spec.description if spec.description else ''}" for name, spec in declared.items()]
        return f"; it takes: {'; '.join(described)}" if described else "; it takes no params"

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


def config_problems(server_config: Any) -> list[str]:
    """What in a machine agent's own keys keeps it from running: the answer of its every request, and what the
    State Graph panel and the start's log say about it."""
    input_mode = str(getattr(server_config, "input", None) or "text")
    on_wait = str(getattr(server_config, "on_wait", None) or "block")
    params = getattr(server_config, "params", None)
    promote = getattr(server_config, "promote", None)
    problems = [
        "no machine configured (machine: <machine id>)" if not getattr(server_config, "machine", None) else "",
        f"input must be text or json, not {input_mode!r}" if input_mode not in ("text", "json") else "",
        f"on_wait must be ask or block, not {on_wait!r}" if on_wait not in ("ask", "block") else "",
        f"params must be a mapping, not {type(params).__name__}" if params is not None and not isinstance(params, dict)
        else "",
        f"promote must be a list of output keys, not {type(promote).__name__}"
        if promote is not None and not isinstance(promote, list) else ""]
    return [problem for problem in problems if problem]


def _waits(row: dict[str, Any]) -> frozenset[tuple[str, int]]:
    """The waits a run stands in (frame, step): a reply answers these; a new one is another question."""
    return frozenset((frame.get("prefix", ""), int(frame.get("step") or 0))
                     for frame in (row.get("view") or {}).get("frames") or [] if frame.get("accepts"))


#: A reply's event name, then its data: ``reject: {...}``, ``reject {...}``, the data on the next line.
_REPLY = re.compile(r"^(\S+?)(?::\s*|\s+|$)(.*)$", re.S)


def _event_of(text: str, accepts: list[str]) -> Union[tuple[str, Any, Optional[str]], str]:
    """``(name, data, frame)`` a reply sends -- ``{"event": name, "data": ..., "frame": ...}``, or the event's name
    with the data after it -- or why it names none the run takes now."""
    text = text.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    frame = None
    if isinstance(parsed, dict) and isinstance(parsed.get("event"), str):
        name, data = parsed["event"], parsed.get("data")
        frame = parsed["frame"] if isinstance(parsed.get("frame"), str) else None
    else:
        found = _REPLY.match(text)
        name, rest = (found.group(1), found.group(2).strip()) if found else ("", "")
        try:
            data = json.loads(rest) if rest else None
        except ValueError:
            data = rest
    match = next((event for event in accepts if event.lower() == name.lower()), None)
    if match is None:
        return f"{name[:60]!r} is none of the events it takes now ({', '.join(accepts) or 'none'})"
    return match, data, frame


def _last_seq(server: Any, run_id: str) -> int:
    rows = server.run_store.tail(run_id, 1)
    return int(rows[-1]["seq"]) if rows else 0


def _discarded(server: Any, run_id: str, since: Optional[int]) -> list[str]:
    """Why a reply did not move the run: the events a dispatch discarded after ``since``, with its guards."""
    if since is None:
        return []
    lines = []
    for row in server.run_store.page(run_id, after=since, kinds=["trace"]):
        data = row.get("data") or {}
        if row.get("status") != "event_discarded":
            continue
        guards = "; ".join(f"{guard.get('guard')} -> {guard['error'] if 'error' in guard else guard.get('result')}"
                           for guard in data.get("guards") or [])
        lines.append(f"Not taken: {data.get('event')!r} in {row.get('state')!r} -- no transition took it"
                     + (f" (guards: {guards})" if guards else "") + ".")
    return lines


def _declared(row: dict[str, Any], waits: list[tuple[Any, Any]]) -> tuple[dict[str, dict[str, Any]],
                                                                          list[tuple[str, str]]]:
    """The events every machine of the run declares ({name: {description, data}}), and the waiting states with
    their descriptions -- from the run's own definition."""
    from .model.loader import load_snapshot

    events: dict[str, dict[str, Any]] = {}
    specs: dict[str, Any] = {}
    try:
        tree = load_snapshot(row.get("definition") or {}, execute_python=False)  # descriptions only
        for loaded in tree.files.values():
            spec = loaded.spec
            if spec is None:
                continue
            specs[spec.id] = spec
            for name, event in spec.events.items():
                events.setdefault(name, {"description": event.description, "data": event.data})
    except Exception:  # a definition that no longer loads: the question names the events without their text
        logger.debug("definition of run %s not read", row.get("id"), exc_info=True)

    def described(states: dict[str, Any], name: str) -> Optional[str]:
        for state_name, state in (states or {}).items():
            if state_name == name:
                return state.description or ""
            found = described(state.states, name)
            if found is not None:
                return found
        return None

    states = [(str(state), described(specs[machine].states, state) or "" if machine in specs else "")
              for machine, state in waits if state]
    return events, states


def _root_state(row: dict[str, Any]) -> Optional[str]:
    frames = (row.get("view") or {}).get("frames") or []
    return frames[0].get("state") if frames else None


__all__ = ["ATTACH_PATIENCE", "MachineAgent"]
