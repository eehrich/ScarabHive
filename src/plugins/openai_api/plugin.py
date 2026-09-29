"""openai_api: the agents behind the OpenAI Responses and Chat Completions API.

Base URL for a client: ``http(s)://<host>/plugins/<instance>/v1`` with the user's
API key (or an access token) as the key. The model is the agent's name.

- ``GET  /v1/models``            the agents offered (by default: the ones the web UI lists)
- ``POST /v1/responses``         one turn; ``previous_response_id`` continues the stored conversation,
                                 ``store: false`` runs on a throwaway session
- ``POST /v1/chat/completions``  stateless, as at OpenAI: the history comes with every call and no
                                 session is kept

Both stream (``stream: true``) as Server-Sent Events in the respective format. A
client that leaves stops the agent: a stream hears it from Starlette, a JSON
answer asks the connection every second (``DISCONNECT_POLL``). The answer is the
agent's final message; a stream's deltas are the work as it happens (every LLM
call of the turn, set apart), and a Responses stream ends on the final message.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator, AsyncIterator, Optional

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.background import BackgroundTasks

from agent_system.plugins.web_base import SchemaBasedPluginWebInterface

from agent_system.llm.structured_output import STRUCTURED_OUTPUT_INVALID, STRUCTURED_OUTPUT_UNAVAILABLE

from .protocol import (ApiError, chat_chunk, chat_completion, chat_response_format, chat_usage, model_list, new_id, now,
                       output_message, prepare_format, refuse_client_tools, response_object, responses_text_format,
                       sse, turn_from_messages)
from .store import ResponseStore
from .turns import AgentTurn, ConversationBusy, ConversationGone, TurnError

#: Seconds a structured stream may stay silent before it sends an SSE comment: its answer comes whole at the
#: end, and a proxy drops a connection that says nothing for long.
KEEPALIVE_SECONDS = 15.0
_KEEPALIVE: Any = object()  # what _whole yields meanwhile; the routes write ": keep-alive"

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

_STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

#: Seconds between two looks at the connection while a JSON answer is being worked on.
DISCONNECT_POLL = 1.0


class _ClientGone(Exception):
    """The client closed the connection before its JSON answer was ready."""


def _gone() -> ApiError:
    """A continued conversation that is not stored any more (deleted in the web UI, archived, unreadable)."""
    return ApiError(404, "the conversation of this response is no longer stored", param="previous_response_id",
                    code="previous_response_not_found")


def _plugin_stopped() -> ApiError:
    return ApiError(503, "the openai_api plugin is stopped", type_="server_error")


def _streamed(stream: AsyncGenerator[str, None], turn: AgentTurn) -> StreamingResponse:
    """A stream whose generator -- and turn -- are closed once its response is over, as the app's
    ``_sse_response`` closes its generator.

    Starlette cancels a response whose client left but never closes its body generator. Caught at a yield --
    in the middle of a send (a client that stopped reading, uvicorn waiting to write) -- the generator waited for
    the garbage collector, and the turn with it: its run went on, its conversation stayed busy and held. And a
    generator that never started (its client gone before the first chunk) never reaches its own close of the
    turn, which was opened before the response.
    """
    async def close() -> None:
        # A coroutine function: handed `stream.aclose` itself, Starlette takes it for a plain callable and calls
        # it in a thread, and the awaitable it returns is never awaited.
        await stream.aclose()  # its frames now, not at the garbage collector's pace; for the turn, see below
        await turn.close()  # once only: a stream that ran has closed it already

    background = BackgroundTasks()  # fastapi's, a Starlette BackgroundTask: the plugin declares no starlette
    background.add_task(close)
    return StreamingResponse(stream, media_type="text/event-stream", headers=_STREAM_HEADERS, background=background)


def _failed(exc: BaseException) -> JSONResponse:
    """The JSON error answer for what a route raised -- OpenAI-shaped whatever it was, so a client can read it."""
    if isinstance(exc, ApiError):
        return JSONResponse(exc.body(), status_code=exc.status)
    if isinstance(exc, ConversationBusy):  # found by the turn after its opening (AgentTurn.events)
        return JSONResponse(ApiError(409, str(exc), type_="conflict").body(), status_code=409)
    if isinstance(exc, TurnError):
        if exc.code == STRUCTURED_OUTPUT_UNAVAILABLE:
            # The checker was busy or broke: no verdict on the answer -- a retry may go through (no x-should-retry).
            return JSONResponse(ApiError(503, str(exc), type_="server_error", code=exc.code).body(), status_code=503)
        if exc.code == STRUCTURED_OUTPUT_INVALID:
            # The run already had its correction round: a client's automatic retry (the openai SDK retries
            # every 5xx twice) would buy two more whole agent runs for the same verdict. The SDK reads the
            # header; the code tells the case apart for everyone else.
            return JSONResponse(ApiError(500, str(exc), type_="server_error", code=exc.code).body(), status_code=500,
                                headers={"x-should-retry": "false"})
        return JSONResponse(ApiError(500, str(exc), type_="server_error").body(), status_code=500)
    if isinstance(exc, _ClientGone):  # nobody reads it; for the access log
        return JSONResponse(ApiError(499, "the client closed the request").body(), status_code=499)
    logger.exception("openai_api: the request failed", exc_info=exc)
    return JSONResponse(ApiError(500, f"internal error: {type(exc).__name__}", type_="server_error").body(),
                        status_code=500)


class OpenAIApiPlugin(SchemaBasedPluginWebInterface):
    def __init__(self, name: str, system_config: "AgentSystemConfig", server_config: "ToolServerConfig"):
        super().__init__(name, system_config, server_config)
        self.agent_patterns = [str(p) for p in (getattr(server_config, "agents", None) or [])]
        self.blocked_patterns = [str(p) for p in (getattr(server_config, "blocked_agents", None) or [])]
        self.responses_db = str(getattr(server_config, "responses_db", None) or "data/openai_api/responses.db")
        self._store: Optional[ResponseStore] = None
        self._busy: set[str] = set()  # sessions with a turn in flight: a conversation takes one turn at a time
        self._turns: set[AgentTurn] = set()  # the turns not settled yet (stop_plugin waits for them)
        self._stopped = False  # stop_plugin ran: the response store is closed, and stays closed until a start

    async def call(self, tool: Optional[str] = None, params: Optional[dict] = None, *args: Any, **kwargs: Any):
        return {"status": "ok", "name": self.name, "type": "web_only", "active": True}

    def get_security_config(self) -> dict[str, Any]:
        return {"accept_api_keys": True}  # OpenAI clients send the user's API key as the Bearer value

    async def start_plugin(self) -> None:
        """Started (again, after a stop): the response store opens when it is next needed."""
        self._stopped = False

    async def stop_plugin(self) -> None:
        """At shutdown, before the loop cancels what is left: a turn whose run is still stopping gets STOP_GRACE
        to stop and be settled. Cancelled unsettled, its conversation would keep the stopped turn (a new one
        would stay on disk). Then the response store is closed."""
        from .turns import STOP_GRACE

        turns = list(self._turns)
        if turns:
            waits = [asyncio.ensure_future(turn.settled()) for turn in turns]
            _, left = await asyncio.wait(waits, timeout=STOP_GRACE)
            for wait in left:
                wait.cancel()
            if left:
                logger.warning("openai_api: %d turn(s) not settled at shutdown: their runs did not stop within "
                               "%.0fs, their conversations keep what those runs saved", len(left), STOP_GRACE)
        self._stopped = True  # a record after this -- a run that outlasted the wait, routes of a plugin
        if self._store is not None:  # unregistered at runtime -- fails instead of opening a store nobody closes
            self._store.close()
            self._store = None
        # The schema workers of this loop (structured output): ended with the plugin, not left to their idle time.
        from agent_system.llm.structured_output import close_schema_workers

        try:
            await close_schema_workers()
        except Exception:
            logger.warning("openai_api: the schema workers did not end cleanly", exc_info=True)

    @property
    def store(self) -> ResponseStore:
        if self._stopped:
            raise _plugin_stopped()
        if self._store is None:
            self._store = ResponseStore(self.responses_db)
        return self._store

    # ------------------------------------------------------------ routes
    def get_web_router(self) -> APIRouter:
        router = APIRouter(prefix=f"/plugins/{self.name}/v1")
        router.add_api_route("/models", self.list_models, methods=["GET"])
        router.add_api_route("/models/{model}", self.get_model, methods=["GET"])
        router.add_api_route("/responses", self.create_response, methods=["POST"])
        router.add_api_route("/chat/completions", self.create_chat_completion, methods=["POST"])
        return router

    async def list_models(self, request: Request) -> JSONResponse:
        return await self._answer(request, lambda: model_list(self._agent_names(request)))

    async def get_model(self, request: Request, model: str) -> JSONResponse:
        def one() -> dict[str, Any]:
            if model not in self._agent_names(request):
                raise ApiError(404, f"The model {model!r} does not exist", param="model", code="model_not_found")
            return model_list([model])["data"][0]
        return await self._answer(request, one)

    async def create_response(self, request: Request) -> Any:
        try:
            body = await self._body(request)
            user = await self._user(request)
            refuse_client_tools(body)
            if body.get("background"):
                raise ApiError(400, "background: not supported", param="background")
            store = body.get("store", True) is not False
            if store and self._stopped:  # refused before a run, not after it: a stopped plugin records nothing
                raise _plugin_stopped()
            previous = body.get("previous_response_id")
            instructions = body.get("instructions")
            if instructions is not None and not isinstance(instructions, str):
                raise ApiError(400, "instructions: only a string is supported", param="instructions")
            raw_input = body.get("input")
            if raw_input is None or raw_input == "":
                raise ApiError(400, "input is required", param="input")
            items = [{"role": "user", "content": raw_input}] if isinstance(raw_input, str) else raw_input
            if previous is not None:
                found = self.store.find(str(previous), user)
                if found is None:
                    raise ApiError(404, f"Previous response with id {previous!r} not found",
                                   param="previous_response_id", code="previous_response_not_found")
                if self.store.latest(found["session_id"]) != previous:
                    raise ApiError(409, "previous_response_id is not the latest response of its conversation: "
                                        "a conversation continues from its last response only",
                                   param="previous_response_id")
                if not store:
                    raise ApiError(400, "store: false cannot continue a stored conversation", param="store")
                model = str(body.get("model") or found["agent"])
                if model != found["agent"]:
                    raise ApiError(400, f"the conversation belongs to the model {found['agent']!r}", param="model")
                self._offered(request, model)
                session_id = found["session_id"]
                # Instructions become part of the conversation (they reach the agent in front of the turn, and
                # the turn is stored); sent again unchanged, they are there already.
                repeated = instructions is not None and instructions == self.store.instructions(session_id)
            else:
                model = self._model(request, body)
                session_id = self._new_session_id(store)
                repeated = False
            turn = turn_from_messages(items, "input", instructions=None if repeated else instructions)
            structured = responses_text_format(body)
            agent, service = self._agent(request, model)
            _refuse_format_it_cannot_take(agent, model, structured, "text.format")
            # In the schema worker, not on this loop (the subset, under a deadline): a costly schema holds
            # nobody else up.
            structured = await prepare_format(structured, "text.format", user)
        except Exception as exc:
            return _failed(exc)
        response_id, message_id, created = new_id("resp"), new_id("msg"), now()
        common = dict(created=created, store=store, previous=previous)
        # A new conversation is named as SessionService names one (the first user text, 50 characters) -- from the
        # user's own text: the instructions stand in front of it in the turn.
        opening = dict(persist=store, history=turn.history, continues=previous is not None, title=turn.title[:50],
                       response_format=structured)

        async def finish(turn_run: AgentTurn, text: str) -> dict[str, Any]:
            def record() -> None:
                self.store.add(response_id, user, session_id, model, instructions=instructions)

            # Kept only as delivered: saved, then its id recorded -- the id must not lead to a conversation that
            # was not stored, and a conversation must not keep a turn nobody has the id of.
            await turn_run.close(deliver=record if store else None)
            return response_object(response_id, model, status="completed", output=[output_message(message_id, text)],
                                   usage=turn_run.usage, **common)

        if not body.get("stream"):
            try:
                async with self._turn(agent, service, user, session_id, **opening) as turn_run:
                    await _run_to_end(request, turn_run, turn.message)
                    if await request.is_disconnected():
                        # Left in the last moments of the run: an answer nobody reads keeps no turn. (A client
                        # that leaves while the answer is sent is not seen at all.)
                        raise _ClientGone()
                    return JSONResponse(await finish(turn_run, turn_run.answer()))
            except Exception as exc:
                return _failed(exc)

        # Opened before the stream starts: a refusal (busy, running elsewhere, gone) is a status, not a
        # response.failed after a 200 -- which a client takes for a server error to retry.
        try:
            turn_run = await self._open_turn(agent, service, user, session_id, **opening)
        except Exception as exc:
            return _failed(exc)

        async def stream() -> AsyncGenerator[str, None]:
            sequence = 0

            def event(kind: str, **data: Any) -> str:
                nonlocal sequence
                sequence += 1
                return sse({"type": kind, "sequence_number": sequence, **data}, event=kind)

            try:
                started = response_object(response_id, model, status="in_progress", output=[], **common)
                yield event("response.created", response=started)
                yield event("response.in_progress", response=started)
                part = {"type": "output_text", "text": "", "annotations": []}
                where = {"item_id": message_id, "output_index": 0, "content_index": 0}
                try:
                    yield event("response.output_item.added", output_index=0,
                                item=output_message(message_id, "", status="in_progress"))
                    yield event("response.content_part.added", part=part, **where)
                    async for delta in _stream_of(turn_run, turn.message):
                        if delta is _KEEPALIVE:
                            yield ": keep-alive\n\n"
                            continue
                        yield event("response.output_text.delta", delta=delta, logprobs=[], **where)
                    text = turn_run.answer()  # the final message, as the JSON answer has it
                    yield event("response.output_text.done", text=text, logprobs=[], **where)
                    yield event("response.content_part.done", part={**part, "text": text}, **where)
                    yield event("response.output_item.done", output_index=0, item=output_message(message_id, text))
                    yield event("response.completed", response=await finish(turn_run, text))
                except Exception as exc:
                    await turn_run.close()  # settled -- put back -- before the failure goes out
                    if isinstance(exc, ConversationBusy):
                        # Refused after the stream began (AgentTurn.events): the Responses "error" event, which names
                        # its own code -- response.failed takes only OpenAI's codes, and "server_error" invites a retry.
                        yield event("error", code="conflict", message=str(exc), param=None)
                        return
                    if not isinstance(exc, (ApiError, TurnError)):
                        logger.exception("openai_api: the streamed response failed")
                    message = exc.message if isinstance(exc, ApiError) else str(exc) or type(exc).__name__
                    failed = response_object(response_id, model, status="failed", output=[],
                                             error={"code": "server_error", "message": message}, **common)
                    yield event("response.failed", response=failed)
            finally:
                await turn_run.close()

        return _streamed(stream(), turn_run)

    async def create_chat_completion(self, request: Request) -> Any:
        try:
            body = await self._body(request)
            user = await self._user(request)
            refuse_client_tools(body)
            n = body.get("n")
            if n is not None and (not isinstance(n, int) or isinstance(n, bool) or n != 1):
                raise ApiError(400, "n: only one choice is supported", param="n")
            options = body.get("stream_options")
            if options is not None and not isinstance(options, dict):
                raise ApiError(400, "stream_options must be an object", param="stream_options")
            turn = turn_from_messages(body.get("messages"), "messages")
            structured = chat_response_format(body)
            model = self._model(request, body)
            agent, service = self._agent(request, model)
            _refuse_format_it_cannot_take(agent, model, structured, "response_format")
            structured = await prepare_format(structured, "response_format.json_schema", user)
        except Exception as exc:
            return _failed(exc)
        completion_id, session_id = new_id("chatcmpl"), self._new_session_id(False)

        if not body.get("stream"):
            try:
                async with self._turn(agent, service, user, session_id, persist=False,
                                      history=turn.history, response_format=structured) as turn_run:
                    await _run_to_end(request, turn_run, turn.message)
                    text = turn_run.answer()  # before close: a throwaway session is gone after it
                    await turn_run.close()
                    return JSONResponse(chat_completion(completion_id, model, text, turn_run.usage))
            except Exception as exc:
                return _failed(exc)

        with_usage = bool((options or {}).get("include_usage"))
        try:  # opened before the stream starts: a refusal is a status (see create_response)
            turn_run = await self._open_turn(agent, service, user, session_id, persist=False, history=turn.history,
                                             response_format=structured)
        except Exception as exc:
            return _failed(exc)

        async def stream() -> AsyncGenerator[str, None]:
            try:
                try:
                    yield sse(chat_chunk(completion_id, model, {"role": "assistant", "content": ""}))
                    # The deltas are all a client gets here -- a chunk stream has no final text.
                    async for delta in _stream_of(turn_run, turn.message):
                        if delta is _KEEPALIVE:
                            yield ": keep-alive\n\n"
                            continue
                        yield sse(chat_chunk(completion_id, model, {"content": delta}))
                    yield sse(chat_chunk(completion_id, model, {}, finish="stop"))
                    if with_usage:
                        yield sse({**chat_chunk(completion_id, model, {}), "choices": [],
                                   "usage": chat_usage(turn_run.usage)})
                except Exception as exc:
                    await turn_run.close()
                    if not isinstance(exc, (ApiError, TurnError, ConversationBusy)):
                        logger.exception("openai_api: the streamed chat completion failed")
                    message = exc.message if isinstance(exc, ApiError) else str(exc) or type(exc).__name__
                    # The code as the JSON answer has it (_failed): after the 200 no header can stop a retry,
                    # but a client can still tell a format verdict from a failed run.
                    code = (exc.code if isinstance(exc, TurnError)
                            and exc.code in (STRUCTURED_OUTPUT_INVALID, STRUCTURED_OUTPUT_UNAVAILABLE) else None)
                    yield sse(ApiError(500, message, type_="server_error", code=code).body())
                yield "data: [DONE]\n\n"
            finally:
                await turn_run.close()

        return _streamed(stream(), turn_run)

    # ------------------------------------------------------------ plumbing
    async def _open_turn(self, agent: Any, service: Any, user: str, session_id: str, *, persist: bool,
                         history: list[Any], continues: bool = False, title: Optional[str] = None,
                         response_format: Any = None) -> AgentTurn:
        """A turn, opened: one at a time per conversation, the session held and opened. A refusal comes as the
        OpenAI error a client reads, before anything ran. The conversation stays busy until the turn is settled --
        after its run has stopped, not when the client left."""
        from agent_system.services.session_manager import SessionPermissionError

        turn = AgentTurn(agent, service, user=user, session_id=session_id, request_id=f"oai_{new_id('r')[2:14]}",
                         persist=persist, continues=continues, title=title, response_format=response_format)
        if session_id in self._busy:
            raise ApiError(409, "this conversation is answering another request right now", type_="conflict")
        self._busy.add(session_id)
        self._turns.add(turn)

        def settled() -> None:
            self._busy.discard(session_id)
            self._turns.discard(turn)

        turn.on_settled(settled)
        try:
            await turn.open(history)
        except BaseException as exc:
            await turn.close()
            if isinstance(exc, SessionPermissionError):
                raise ApiError(403, "the conversation belongs to another user", type_="permission_error") from None
            if isinstance(exc, ConversationGone):
                raise _gone() from None
            if isinstance(exc, ConversationBusy):
                raise ApiError(409, str(exc), type_="conflict") from None
            raise
        return turn

    @asynccontextmanager
    async def _turn(self, *args: Any, **kwargs: Any) -> AsyncIterator[AgentTurn]:
        """``async with``: the turn opened (_open_turn), and closed however it ends."""
        turn = await self._open_turn(*args, **kwargs)
        try:
            yield turn
        finally:
            await turn.close()

    async def _answer(self, request: Request, build: Any) -> JSONResponse:
        try:
            await self._user(request)
            return JSONResponse(build())
        except Exception as exc:
            return _failed(exc)

    async def _body(self, request: Request) -> dict[str, Any]:
        # JSON only: a form or text/plain POST is what a page of another origin can send with the user's cookie
        # and without asking first.
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        try:
            body = await request.json()
        except ValueError:
            raise ApiError(400, "The body is not valid JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "The body must be a JSON object")
        return body

    async def _user(self, request: Request) -> str:
        """The caller's username ("anonymous" when the app runs without auth)."""
        from agent_system.auth.database import get_db
        from agent_system.auth.dependencies import bearer_scheme, get_optional_user

        auth = getattr(getattr(request.app.state, "config", None), "auth", None)
        if auth is None or not auth.enabled:
            return "anonymous"
        user = await get_optional_user(request, await bearer_scheme(request), request.headers.get("X-API-Key"),
                                       get_db())
        if user is None or not user.is_active:
            raise ApiError(401, "Incorrect API key provided", type_="invalid_request_error", code="invalid_api_key")
        return user.username

    def _registry(self, request: Request) -> Any:
        registry = getattr(request.app.state, "tool_registry", None)
        if registry is None:
            raise ApiError(503, "no agent registry: the app is still starting", type_="server_error")
        return registry

    def _agent_names(self, request: Request) -> list[str]:
        """The agents offered as models: the ones the web UI lists (``/agents``), narrowed by
        ``agents``/``blocked_agents``."""
        from agent_system.runtime import ServerView
        from agent_system.servers.agent.server import Agent

        registry = self._registry(request)
        names = []
        for name in registry.list():
            view = registry.describe(name) if callable(getattr(registry, "describe", None)) else None
            if isinstance(view, ServerView):
                listed = view.is_agent and view.tool_public
            else:
                try:
                    server = registry.get(name)
                except Exception:
                    continue
                listed = isinstance(server, Agent) and bool(getattr(server, "_tool_public", True))
            if not listed:
                continue
            if self.agent_patterns and not any(fnmatch.fnmatchcase(name, p) for p in self.agent_patterns):
                continue
            if any(fnmatch.fnmatchcase(name, p) for p in self.blocked_patterns):
                continue
            names.append(name)
        return sorted(names)

    def _model(self, request: Request, body: dict[str, Any]) -> str:
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise ApiError(400, "model: the agent's name is required", param="model")
        self._offered(request, model)
        return model

    def _offered(self, request: Request, model: str) -> None:
        """404 for an agent not offered -- never was, or not any more (a continued conversation's too)."""
        if model not in self._agent_names(request):
            raise ApiError(404, f"The model {model!r} does not exist or you do not have access to it",
                           param="model", code="model_not_found")

    def _agent(self, request: Request, model: str) -> tuple[Any, Any]:
        try:
            agent = self._registry(request).get(model)
        except KeyError:
            raise ApiError(404, f"The model {model!r} does not exist or you do not have access to it",
                           param="model", code="model_not_found") from None
        service = getattr(agent, "_session_service", None)
        if service is None or getattr(service, "session_manager", None) is None:
            raise ApiError(503, "no session service: the app is still starting", type_="server_error")
        return agent, service

    @staticmethod
    def _new_session_id(store: bool) -> str:
        from agent_system.utils.id import short_id

        from agent_system.services.session_service import EPHEMERAL_SESSION_PREFIX

        return short_id() if store else f"{EPHEMERAL_SESSION_PREFIX}oai-{new_id('s')[2:18]}"

    def get_static_assets(self) -> Optional[Path]:
        return None


async def _run_to_end(request: Request, turn: AgentTurn, message: str) -> None:
    """The turn's run to its end, for a JSON answer. Nothing tells such a route that its client left (only a
    stream hears it), so the connection is asked every ``DISCONNECT_POLL`` seconds: a client that left stops
    the run -- or an SDK's retry after its timeout would start the next one next to it."""

    async def drain() -> None:
        async for _ in turn.events(message):
            pass

    work = asyncio.ensure_future(drain())
    try:
        while True:
            done, _ = await asyncio.wait({work}, timeout=DISCONNECT_POLL)
            if done:
                return work.result()
            if await request.is_disconnected():
                raise _ClientGone()
    finally:
        if not work.done():
            work.cancel()  # events() stops the run (its token first) and waits for it, STOP_GRACE at most
            with anyio.CancelScope(shield=True):
                await asyncio.wait({work})


def _refuse_format_it_cannot_take(agent: Any, model: str, structured: Any, param: str) -> None:
    """An agent whose ``run_events`` has no ``response_format`` -- a state machine behind the stategraph
    facade, say -- cannot hold its answer to a format: refused before the run, as OpenAI refuses a parameter
    a model does not support, instead of the TypeError its run would end on."""
    if structured is None:
        return
    try:
        parameters = inspect.signature(agent.run_events).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "response_format" in parameters or any(p.kind is p.VAR_KEYWORD for p in parameters.values()):
        return
    raise ApiError(400, f"Unsupported parameter: '{param}' is not supported with the model {model!r}.",
                   param=param, code="unsupported_parameter")


def _stream_of(turn: AgentTurn, message: str) -> AsyncIterator[str]:
    """What a stream carries: the work as it happens, or -- for a structured turn -- its answer whole."""
    return _whole(turn, message) if turn.response_format is not None else _deltas(turn, message)


async def _whole(turn: AgentTurn, message: str) -> AsyncIterator[str]:
    """A structured turn's answer as ONE delta, once the run has checked it against the format.

    Nothing of the run goes out before: its steps' notes, an answer the run sent back for
    correction -- the joined deltas of a stream have to BE the JSON the client asked for. Every
    KEEPALIVE_SECONDS of that silence it yields _KEEPALIVE, which the routes write as an SSE comment.
    The run is a task of its own for that; a consumer that leaves stops it, as it stops _deltas' run.
    """

    async def drain() -> None:
        async for _ in turn.events(message):
            pass

    work = asyncio.ensure_future(drain())
    try:
        while True:
            done, _ = await asyncio.wait({work}, timeout=KEEPALIVE_SECONDS)
            if done:
                break
            yield _KEEPALIVE
        work.result()  # the run's own failure (TurnError, ConversationBusy)
    finally:
        if not work.done():
            work.cancel()  # events() stops the run (its token first) and waits for it, STOP_GRACE at most
            with anyio.CancelScope(shield=True):
                await asyncio.wait({work})
    yield turn.answer()


async def _deltas(turn: AgentTurn, message: str) -> AsyncIterator[str]:
    """The work as the agent writes it: every LLM call's deltas, a blank line between two calls.

    A new call starts after a ``thinking_complete`` (the next step of a multi-step run, a call asked again
    after an empty answer) -- or without one, when a call that broke off mid-stream by an exception (a rate
    limit, a dropped connection) is asked again (_starts_over).

    A completed call whose content did not all go out in its deltas: the rest follows at once when the content
    goes on from what it streamed (a client that salvages a rest of the stream without a delta). And when the
    turn's last call did not stream its content at all -- a model that does not stream, or a call asked again on
    a fallback that does not -- the final message comes in one piece at the end.
    """
    between, wrote = False, False
    seen, streamed = "", ""  # the current call's accumulated text / its deltas
    call_wrote = last_streamed = False  # the current call sent deltas / the last completed call's content went out
    async for event in turn.events(message):
        kind = event.get("type")
        if kind == "thinking_complete":
            rest = _rest(event, seen, streamed) if call_wrote else None
            if rest:
                yield rest
            last_streamed = rest is not None
            between, seen, streamed, call_wrote = True, "", "", False
        elif kind == "thinking_delta" and event.get("delta"):
            delta, accumulated = str(event["delta"]), event.get("accumulated")
            restarted = isinstance(accumulated, str) and _starts_over(accumulated, seen, delta)
            if (between or restarted) and wrote:
                yield "\n\n"
            if restarted:
                streamed = ""
            between, wrote, call_wrote = False, True, True
            if isinstance(accumulated, str) and accumulated:
                seen = accumulated
            streamed += delta
            yield delta
    if not last_streamed and (text := turn.answer()):
        yield ("\n\n" if wrote else "") + text


def _starts_over(accumulated: str, seen: str, delta: str) -> bool:
    """Whether a delta starts a new call: its ``accumulated`` (the call's text so far, as its client keeps it)
    does not go on from the one before.

    It goes on when it grew by the delta -- also with thinking grown in front of the text (Anthropic's client
    keeps it there, Gemini's its thoughts): what follows the common start then still ends it, with the delta
    after it, or without, when the delta was a thought put in front. It starts over when it grew by less than
    the delta (a new call has only its own text), or when the text before does not end it any more (a new call
    that thought longer before it wrote)."""
    if not seen:
        return False
    if len(accumulated) < len(seen) + len(delta):
        return True
    common = 0
    for new, old in zip(accumulated, seen, strict=False):  # up to the shorter one
        if new != old:
            break
        common += 1
    tail = seen[common:]
    return not (accumulated.endswith(tail + delta) or accumulated.endswith(tail))


def _rest(event: dict[str, Any], accumulated: str, streamed: str) -> Optional[str]:
    """What of a completed call's content did not go out in its deltas: "" when all of it did (its client's
    accumulated text ends with it -- thinking or thoughts stand in front of it there, never behind), the rest
    when the content goes on from what the call streamed, None when it is not what the call streamed at all.
    A call without content has nothing to miss; a client that does not accumulate leaves its deltas as all
    there is to go by."""
    content = (event.get("assistant") or {}).get("content")
    if not isinstance(content, str) or not content.strip() or not accumulated:
        return ""
    if accumulated.rstrip().endswith(content.strip()):
        return ""
    if streamed and content.startswith(streamed):
        return content[len(streamed):]
    return None


PLUGIN_FACTORY = OpenAIApiPlugin
