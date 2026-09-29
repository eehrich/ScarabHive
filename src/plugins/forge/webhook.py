"""The webhook route and where its events go (docs/konzept.md §7).

``POST /plugins/<instance>/webhook`` takes GitLab's project webhooks. The
route has no login -- the operator opens it in both auth layers -- and checks
``X-Gitlab-Token`` against the secrets of the configured hosts before it
reads a byte of the body (W1, W2). It answers at once and distributes after
(W3): each event goes into the inbox of the session bound to its target, and
that session is rung; new work gets a session of its own first (W4).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Mapping, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .events import Event, describe, gitlab_events

if TYPE_CHECKING:
    from .server import ForgeServer, Repo

logger = logging.getLogger(__name__)

MAX_BODY = 1_000_000


@dataclass(frozen=True)
class WebhookConfig:
    user: str               # whose sessions new work starts
    agent: str              # which agent works it
    llm: str                # its LLM profile; "" = the agent's default
    merge: bool             # may a webhook-started run merge (W8)
    max_new_per_hour: int   # new sessions (W9)
    max_wakes_per_hour: int  # rings of sessions that exist (W9)

    @classmethod
    def read(cls, raw: Any) -> Optional["WebhookConfig"]:
        if not isinstance(raw, dict) or not str(raw.get("user") or "").strip():
            return None
        def cap(key: str, default: int) -> int:
            try:
                return max(0, int(raw.get(key, default)))
            except (TypeError, ValueError):
                return default

        return cls(user=str(raw["user"]).strip(), agent=str(raw.get("agent") or "coder"),
                   llm=str(raw.get("llm") or ""), merge=raw.get("merge") is True,
                   max_new_per_hour=cap("max_new_per_hour", 6), max_wakes_per_hour=cap("max_wakes_per_hour", 20))


def _answer(status: int, **body: Any) -> JSONResponse:
    return JSONResponse(body, status_code=status)


class Intake:
    def __init__(self, server: "ForgeServer") -> None:
        self.server = server
        self._tasks: set[asyncio.Task] = set()
        self._targets: dict[tuple[str, str, str], asyncio.Lock] = {}

    def router(self) -> APIRouter:
        router = APIRouter(prefix=f"/plugins/{self.server.name}", tags=[self.server.name])

        @router.post("/webhook", include_in_schema=False)
        async def webhook(request: Request) -> JSONResponse:
            host = self.host_of(request.headers)
            if host is None:
                return _answer(404, detail="Not Found")      # as if there were no route
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_BODY:
                    return _answer(413, detail=f"at most {MAX_BODY} bytes")
            return self.take(host, request.headers, body)

        return router

    def host_of(self, headers: Mapping[str, str]) -> Optional[str]:
        """The configured host whose secret the request carries, compared in constant time."""
        given = (headers.get("x-gitlab-token") or "").encode()
        found = None
        for host, secret in self.server.webhook_secrets().items():
            if given and hmac.compare_digest(given, secret.encode()):
                found = host
        return found

    def take(self, host: str, headers: Mapping[str, str], body: bytes) -> JSONResponse:
        try:
            payload = json.loads(body)
        except ValueError:
            return _answer(400, detail="the body is no JSON")
        project = str(((payload if isinstance(payload, dict) else {}).get("project") or {})
                      .get("path_with_namespace") or "")
        repo = next((r for r in self.server.repos.values() if r.host == host and r.provider == "gitlab"
                     and r.project.lower() == project.lower()), None)
        if repo is None:
            return _answer(202, ignored=f"project {project!r} is not configured for this host")
        # GitLab's resend keeps the Idempotency-Key and renews the event UUID (F-GL15).
        key = headers.get("idempotency-key") or ""
        if key and not self.server.events.first_time(key):
            return _answer(202, ignored="delivered before")
        task = asyncio.get_running_loop().create_task(self.distribute(repo, payload, key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return _answer(202, accepted=True)

    async def distribute(self, repo: "Repo", payload: Any, key: str = "") -> None:
        try:
            bot = (await self.server._backend(repo).me())["login"]
            went = [await self.deliver(event) for event in gitlab_events(payload, repo.name, bot)]
            if key and None in went:
                # Not started (a cap, presence off, nobody works on it): a resend
                # from GitLab once that changed must be taken (review of the fix round).
                self.server.events.forget(key)
        except Exception:  # noqa: BLE001 - GitLab has its answer; the log is where this can go
            logger.exception("forge %s: a webhook event of %s could not be distributed -- resend it from GitLab's "
                             "webhook page", self.server.name, repo.name)
            if key:
                self.server.events.forget(key)

    async def deliver(self, event: Event) -> Optional[str]:
        """Into the inbox of the session that works on the event's target, and
        ring it. Returns the session id, or None when the event went nowhere."""
        store, config = self.server.events, self.server.webhook
        assert config is not None
        presence = self._presence()
        target = (event.repo, event.target, event.key)
        # One target at a time: an assignment and a mention arriving together
        # would otherwise start two sessions for one issue (review of the webhook).
        async with self._targets.setdefault(target, asyncio.Lock()):
            bound = store.bound(*target)
            if bound is not None and not self._exists(*bound):
                logger.info("forge %s: session %s working on %s %s is gone -- unbound", self.server.name, bound[1],
                            event.repo, event.ref)
                store.unbind(*target)
                bound = None
            fresh = bound is None
            if fresh:
                if not event.new_work:
                    logger.info("forge %s: %s on %s %s -- no session works on it", self.server.name, event.kind,
                                event.repo, event.ref)
                    return None
                if presence is None:
                    logger.warning("forge %s: %s on %s %s not started -- session presence is off (config.yaml "
                                   "session_presence), nothing would run a new session", self.server.name,
                                   event.kind, event.repo, event.ref)
                    return None
                if not store.may_ring("new", config.max_new_per_hour):
                    logger.warning("forge %s: %s on %s %s not started -- %d new sessions within the hour "
                                   "(webhook.max_new_per_hour)", self.server.name, event.kind, event.repo,
                                   event.ref, config.max_new_per_hour)
                    return None
                bound = config.user, await self.new_session(event)
                store.bind(*target, *bound)
            user, session = bound
            # A mention asks a new session for an answer; the session that works
            # on the target takes it like any comment (review of the webhook).
            shown = event if fresh or event.kind != "mention" else replace(event, kind="comment")
            store.put(session, user, describe(shown, self.server.name, merge=config.merge))
        if presence is None:
            logger.warning("forge %s: session presence is off (config.yaml session_presence) -- the event waits "
                           "for the next run of session %s", self.server.name, session)
            return session
        if not fresh and not store.may_ring("wake", config.max_wakes_per_hour):
            logger.warning("forge %s: %s on %s %s waits for the next run of session %s -- %d wakes within the "
                           "hour (webhook.max_wakes_per_hour)", self.server.name, event.kind, event.repo, event.ref,
                           session, config.max_wakes_per_hour)
            return session
        rung, note = await asyncio.to_thread(presence.notify, session, user)
        logger.info("forge %s: %s on %s %s -> session %s: %s %s", self.server.name, event.kind, event.repo,
                    event.ref, session, rung, note)
        return session

    async def new_session(self, event: Event) -> str:
        from agent_system.core.session_presence import sessions_dir
        from agent_system.services.session_manager import SessionManager

        config = self.server.webhook
        assert config is not None
        created = await SessionManager(storage_path=str(sessions_dir())).create_session(
            user_id=config.user, title=f"forge: {event.repo} {event.ref}", agent_name=config.agent,
            llm_profile=config.llm or self._default_profile(config.agent))
        return str(created["session_id"])

    def _exists(self, user: str, session: str) -> bool:
        """Stored, or held by a live run: a session in its first run has only
        its lock file (agent-cli saves at the end), and a lock a crashed run
        left behind is no session -- presence tells the two apart (reviews of
        the fix rounds). Without presence there are no locks to ask."""
        presence = self._presence()
        if presence is not None:
            return presence.get(session, user) is not None
        from agent_system.core.session_presence import sessions_dir

        return (sessions_dir() / user / f"{session}.json").exists()

    def _default_profile(self, agent: str) -> str:
        try:
            from agent_system.config.settings import get_tool_server_config

            agent_config = getattr(get_tool_server_config(agent, self.server.system_config), "agent_config", None)
            profile = (agent_config.get("default_llm_profile") if isinstance(agent_config, dict)
                       else getattr(agent_config, "default_llm_profile", None))
            return str(profile or "normal")
        except Exception:  # noqa: BLE001
            logger.exception("forge %s: the LLM profile of agent %s could not be read", self.server.name, agent)
            return "normal"

    def _presence(self) -> Any:
        from agent_system.core.session_presence import presence_for

        return presence_for(self.server.system_config)
