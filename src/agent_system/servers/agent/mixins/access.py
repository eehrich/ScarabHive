"""Who may run the agent: its role gate (``metadata.min_role``) and the user a session is held for.

What a run, a call of the agent as a tool and the tools a run dispatches ask before they act for
anybody: whether the caller may run this agent (_run_denial, _tool_call_denial), which user a run's
tool calls run for (tool_user), and whether a session is held for another user (_foreign_session).
Its own module: these are the agent's access checks, asked from run_events, Agent.call, the tool
session and SchemaBasedToolMixin.call -- one place to read them all.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from ..refusals import AGENT_ROLE_GATE, FOREIGN_SESSION

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


def _role_gate_refusal(agent: Agent, caller: Callable[[], Optional[str]]
                       ) -> Optional[tuple[Optional[str], str]]:
    """(who, why the agent's role gate refuses them) for the caller *caller* names, or None.

    The check AccessMixin._run_denial and _tool_call_denial share; they differ in
    who the caller is and in how they say no. *caller* is asked only when the
    agent has a gate, and the agent is read for nothing but its gate and the
    auth config -- what those two methods read themselves.
    """
    min_role = agent.min_role
    if min_role is None:
        return None
    from ....auth.agent_access import agent_run_denial

    who = caller()
    reason = agent_run_denial(min_role, who, getattr(agent.system_config, "auth", None))
    return None if reason is None else (who, reason)


class AccessMixin:
    """The role gate and the session's user (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``system_config``, ``min_role`` and ``_session_tracker``
    -- the tracker read with getattr: an agent built without __init__ still answers.
    """

    #: The lowest account role that may run this agent (``metadata.min_role``,
    #: auth/agent_access.py), None for no gate. Set per instance in Agent.__init__
    #: and by reload_config; the class default answers for an instance built
    #: without __init__.
    min_role: Optional[str] = None

    @staticmethod
    def _declared_min_role(server_config: Any) -> Optional[str]:
        """``metadata.min_role`` of a server config; None when it declares none."""
        metadata = getattr(server_config, "metadata", None)
        return metadata.min_role if metadata is not None else None

    def _run_denial(self: Agent, request_id: Optional[str], session_id: Optional[str]) -> Optional[str]:
        """Why this run may not start under the agent's role gate, or None.

        The caller is the owner of the request id when one is registered, else
        the user the SESSION names when it already has metadata, else "anonymous".

        The request owner first: only framework code writes it -- the API for
        its caller, tool execution and the SAM for the calling run's user, the
        stategraph backend for the run's -- and a caller cannot pick the id it
        runs under (tool execution strips a model's request ids). A session id,
        in contrast, can reach a run from where the caller chose it, and this
        agent's tracker keeps the metadata of every session it ran, other users'
        sub-sessions included; asked first, it let a user's run pass as the
        admin whose session it named. On every trusted path the two agree, and
        where they do not, _foreign_session refuses the run as well -- the order
        then only decides which reason it is refused with.

        The session answers where no request is registered: agent-cli and
        agent-run (SessionService.open_for_run), so a woken run answers to its
        session's user, not to the local operator it runs as.
        """
        def who_runs() -> str:
            from ....core.request_context import get_request_user

            who: Optional[str] = get_request_user(request_id, default=None) if request_id else None
            tracker = getattr(self, "_session_tracker", None)
            if who is None and session_id and tracker is not None:
                stored = tracker.get_session_metadata(session_id)
                if stored:
                    who = stored.get("user_id") or None
            if who is None:
                who = "anonymous"
            return who

        refused = _role_gate_refusal(self, who_runs)
        if refused is None:
            return None
        who, reason = refused
        logger.warning("[%s] run refused for %r (request %s, session %s): %s",
                       self.name, who, request_id, session_id, reason)
        return f"Agent '{self.name}' may not be run by '{who}': {reason}"

    def _tool_call_denial(self: Agent, params: Dict[str, Any]) -> Optional[str]:
        """Why the run calling one of this agent's tools may not use it under the role gate, or None.

        For the tools a schema-based agent serves beside its runs
        (SchemaBasedToolMixin.call). The caller is the one the framework names:
        the registered owner of the call's request id, else the injected
        ``_user_id`` -- never a session's stored user -- and without either the
        call is unidentified and refused.
        """
        def who_calls() -> Optional[str]:
            from ....core.request_context import get_request_user

            request_id = params.get("_request_id") or params.get("request_id")
            injected = params.get("_user_id")
            return ((get_request_user(str(request_id), default=None) if request_id else None)
                    or (injected.strip() if isinstance(injected, str) and injected.strip() else None))

        refused = _role_gate_refusal(self, who_calls)
        if refused is None:
            return None
        who, reason = refused
        caller = f"'{who}'" if who else "an unidentified caller"
        logger.warning("[%s] tool call refused to %s: %s", self.name, caller, reason)
        return f"Agent '{self.name}' may not be used by {caller}: {reason}"

    def tool_user(self: Agent, request_id: Optional[str], session_id: Optional[str]) -> Optional[str]:
        """The user this run's tool calls run for -- injected as ``_user_id``, and the
        owner their request ids are registered under (tool_call_contract.inject_runtime_params). THE one
        answer for every way a run dispatches a tool: the LLM's calls, and calls
        made for the run beside the model (tool_preload).

        The run's registered owner first: only framework code registers it (the
        API, tool execution, the SAM, the stategraph backend). The session's
        stored user answers where nothing is registered (agent-cli). Where an
        owner is registered the stored user is the same one at the start of the
        run (_foreign_session refuses another); asked first, the owner stays the
        tools' user whatever the session's metadata says later in the run: the
        metadata is state of the session id, shared by every run of it, and the
        registered owner is this run's.
        """
        from ....core.request_context import get_request_user

        user_id: Optional[str] = get_request_user(request_id, default=None) if request_id else None
        if user_id is not None:
            logger.debug(f"[TOOL_EXEC] user_id='{user_id}' from the owner of request {request_id}")
            return user_id
        tracker = getattr(self, "_session_tracker", None)
        if not tracker:
            logger.warning("[TOOL_EXEC] No _session_tracker available")
            return None
        session_meta = tracker.get_session_metadata(session_id) if session_id else None
        if session_meta:
            user_id = session_meta.get("user_id")
            logger.debug(f"[TOOL_EXEC] Extracted user_id='{user_id}' from session_metadata for session {session_id}")
        else:
            logger.warning(f"[TOOL_EXEC] No session_metadata found for session {session_id}")
        return user_id

    def _refusal_event(self: Agent, request_id: Optional[str], session_id: Optional[str]) -> Optional[dict[str, Any]]:
        """The error a run refused before it starts ends with, or None: the role gate (AGENT_ROLE_GATE) or a
        session held for another user (FOREIGN_SESSION), each with its ``error_type`` for the callers that
        must tell a refusal from a run that failed (REFUSED_BEFORE_THE_RUN)."""
        denial, error_type = self._run_denial(request_id, session_id), AGENT_ROLE_GATE
        if not denial:
            denial, error_type = self._foreign_session(request_id, session_id), FOREIGN_SESSION
        if not denial:
            return None
        return {"type": "error", "message": denial, "request_id": request_id, "error_type": error_type}

    def _foreign_session(self: Agent, request_id: Optional[str], session_id: Optional[str]) -> Optional[str]:
        """Why this run may not go on in *session_id*: this agent holds it for another user.

        The metadata below is written only where none is, so a session this agent
        already holds keeps its user -- and its conversation. An agent called as a
        tool runs on a sub-session derived from its caller's (tool_session_id); a second
        user's run that reaches
        the same id would continue the first user's conversation, and its lock,
        checkpoints and saves go to the first user's session (they read the stored
        user, and a session already on disk is saved into the folder it is in).
        Refused whenever the run's registered owner and the stored user differ --
        an admin's run too, and "anonymous" as the stored user too: POST /run
        refuses another user's session the same way, whether auth is on or off
        (SessionManager.load_session). The API, the SAM and the stategraph
        backend write the owner into the metadata right before the run; an agent
        called as a tool writes it here from its request, which Agent.call
        registers for the injected ``_user_id`` where the caller registered none
        (a plugin command) -- without that, such a call stored
        "anonymous" and the same user's next call was refused as another user.
        So this refuses a session id that reached the run for somebody else.
        """
        if not request_id or not session_id:
            return None
        from ....core.request_context import get_request_user
        owner = get_request_user(request_id, default=None)
        tracker = getattr(self, "_session_tracker", None)
        stored = (tracker.get_session_metadata(session_id) or {}) if tracker is not None else {}
        holder = stored.get("user_id")
        if not owner or not holder or holder == owner:
            return None
        logger.warning("[%s] run of %r refused: session %s is held for %r", self.name, owner, session_id, holder)
        return f"Session {session_id} belongs to another user"
