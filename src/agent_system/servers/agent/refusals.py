"""The refusals a run, or a call of an agent as a tool, answers with: their ``error_type``s.

Compared against by every caller that must tell a refusal from a run that failed -- app.py, the
openai_api plugin, agent-cli, agent-run, the sub-agent manager, Agent.call. Kept apart from the
mixins that answer with them (mixins/access.py, mixins/tool_session.py, mixins/run.py), so that each
of them, and server.py, which re-exports them for those callers, imports one module and no mixin
imports another for a constant.
"""
from __future__ import annotations

from typing import Any

#: ``error_type`` of the error a run ends with when another request of this process holds its session's lock.
#: It ran nothing and has nothing to save -- its caller must not save the session either: what the tracker holds
#: is the other run's live state (see REFUSED_BEFORE_THE_RUN for who asks).
SESSION_LOCKED = "session_locked"
#: ``error_type`` of the error a run ends with when its caller may not run this agent (metadata.min_role).
AGENT_ROLE_GATE = "agent_role_gate"
#: ``error_type`` of the error a run ends with when its session is held for another user (_foreign_session).
FOREIGN_SESSION = "foreign_session"
#: The refusals a run ends with before it has started: it ran nothing and wrote nothing, and its caller must
#: not save the session after it either. Asked by app.py (/run, /events and their jobs), the openai_api turn
#: (its put back, its answer), agent-cli (the one-shot run, via collect_final_result's ``refused``), its chat,
#: agent-run and the sub-agent manager (create, continue, the background job); Agent.call answers with it.
REFUSED_BEFORE_THE_RUN = frozenset({SESSION_LOCKED, AGENT_ROLE_GATE, FOREIGN_SESSION})
#: ``error_type`` of the answer an agent called as a tool gives when it runs above the call already -- it
#: would call itself, directly or through other agents called as tools (Agent._open_tool_session). Not across
#: a sub-agent manager's or a stategraph run's hop: the chain above a call ends at such a session, and the
#: sub-agent nesting budget bounds that (a stategraph run only by a budget a manager above it set). An answer of the tool
#: call, not an event of a run: no run started and no session was opened, so it is none of
#: REFUSED_BEFORE_THE_RUN. When the agent ran on its caller's session, such a call waited at that session's
#: lock and ended with SESSION_LOCKED; on a session of its own below it, nothing is locked, and asking to
#: wait for the other request would be wrong -- that request waits for this call.
RECURSIVE_CALL = "recursive_call"
#: ``error_type`` of the answer an agent called as a tool gives when its session could not be made ready with the
#: caller's sub-agent budget (Agent._file_tool_session): the caller's budget could not be read, or the session's
#: record could not be written with it. Nothing ran -- run without it, a manager below counted from its own
#: maximum. An answer of the tool call as RECURSIVE_CALL is, none of REFUSED_BEFORE_THE_RUN.
TOOL_SESSION_UNAVAILABLE = "tool_session_unavailable"


def refused_before_the_run(event: dict[str, Any]) -> bool:
    """Whether a run event is such a refusal (REFUSED_BEFORE_THE_RUN): the run ran and wrote nothing."""
    return event.get("type") == "error" and event.get("error_type") in REFUSED_BEFORE_THE_RUN
