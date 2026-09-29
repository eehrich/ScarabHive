"""
Shared agent runner functionality for both agent-run and agent-cli.

This module provides centralized logic for:
- Agent selection and creation
- Agent execution with status monitoring
- Error handling and reporting
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..config.settings import AgentSystemConfig
from ..core.session_presence import WAKE_TASK
from ..llm.message_roles import DEVELOPER
from ..llm.models import ChatMessage
from ..tools.base import ToolServerRegistry
from ..servers.agent.server import Agent


def wake_message() -> ChatMessage:
    """The task of a woken run, as the RUN speaking rather than a person typing.

    A woken run's only input is this sentence, and it used to arrive as a plain
    ``user`` turn. Nothing then told it apart from something somebody typed:
    not in the stored transcript, and not for the model, which has to report
    whether it was woken or asked. ``developer`` says who is speaking and keeps
    the note where it happened in the history; every wire format takes some
    rung of that ladder (``llm/message_roles.py``).

    Deliberately WITHOUT ``injected_by``: that marker means "bound to the one
    call it was built for", and ``set_session_messages`` drops those. This one
    is the reason the turn exists at all and has to stay in the transcript. The
    two searches that walk back for the last thing a person wrote stop at the
    role instead (tool_preload, context_engineer: a note, then ``role ==
    "user"``), so the marker is not what keeps them right here. They therefore
    do nothing on a woken turn, which is the intent: every preload rule is a
    regex over what a person asked, and a wake carries no such text.

    What DOES have to see the wake is everything asking "where does this turn
    begin" -- /undo and /retry, the reminder that rides in front of the current
    task, compaction's turn ages and its protected set. They share one
    predicate, ``message_roles.opens_a_turn``.
    """
    return ChatMessage(role=DEVELOPER, content=WAKE_TASK,
                       timestamp=datetime.now(timezone.utc))


async def create_and_register_agent(
    config: AgentSystemConfig,
    registry: ToolServerRegistry,
    agent_name: str,
    session_service=None
) -> Agent:
    """The agent *agent_name*: the registered one, or built from the merged
    config and registered -- servers/agent/entry.py, the one factory the API
    and agent-cli use. Kept for agent-run and the writer's audio plugins.

    Raises NotAnAgent (a ValueError, with the agents there are) for a tool
    server's name, an unknown one, or a registered server that is no agent.
    """
    from ..servers.agent.entry import entry_agent

    return entry_agent(agent_name, config, registry, session_service)
