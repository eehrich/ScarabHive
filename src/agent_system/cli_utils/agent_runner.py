"""
Shared agent runner functionality for both agent-run and agent-cli.

This module provides centralized logic for:
- Agent selection and creation
- Agent execution with status monitoring
- Error handling and reporting
"""
from __future__ import annotations

import logging

from datetime import datetime, timezone

from ..config.settings import AgentSystemConfig, get_tool_server_config
from ..core.session_presence import WAKE_TASK
from ..llm.message_roles import DEVELOPER
from ..llm.models import ChatMessage
from ..tools.base import ToolServerRegistry
from ..servers.agent.server import Agent


logger = logging.getLogger(__name__)


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
    """Create and register an agent if it doesn't already exist in registry.
    
    This handles both plugin-based agents (from plugins.servers) and
    config-based agents (from agents.yaml).
    
    Args:
        config: System configuration
        registry: tool registry to register agent in
        agent_name: Name of the agent to create
        session_service: Optional SessionService to inject into agent
        
    Returns:
        Agent instance (either newly created or existing from registry)
        
    Raises:
        ValueError: If agent configuration not found or invalid
    """
    def _with_agent_listing(message: str) -> str:
        available_agents = []
        if config.plugins and config.plugins.servers:
            available_agents.extend([
                name for name, server in config.plugins.servers.items()
                if server.enabled and server.agent_config is not None
            ])
        available_agents = sorted(set(available_agents))
        if available_agents:
            return message + "\n\nAvailable agents:\n  " + "\n  ".join(available_agents)
        return message + "\n\nNo agents are configured. Check your config files."

    # Check if agent already exists in registry
    try:
        existing_agent = registry.get(agent_name)
    except KeyError:
        existing_agent = None  # Agent doesn't exist, need to create it

    if isinstance(existing_agent, Agent):
        logger.debug(f"Using existing agent '{agent_name}' from registry")
        # Update session_service for existing agent
        if session_service and hasattr(existing_agent, '_session_service'):
            existing_agent._session_service = session_service
        return existing_agent

    if existing_agent is not None:
        # Building one here would register it OVER that server and take it out
        # of the registry for the rest of the process.
        raise ValueError(_with_agent_listing(
            f"'{agent_name}' is registered as {type(existing_agent).__name__}, not an Agent."))

    # "Is this an agent at all" has to be decided on the RAW entry:
    # plugins.default_config carries an agent_config, so the MERGED config has
    # one for every tool server as well -- measured on the real config, 96 of
    # 219 servers have no raw agent_config and every one of them is a tool
    # server. A gate on the merged config waves all of them through.
    raw_config = config.plugins.servers.get(agent_name) if config.plugins else None
    if raw_config is not None and not raw_config.agent_config:
        raise ValueError(_with_agent_listing(f"'{agent_name}' is a tool server, not an agent."))

    # Try to get tool server config from plugins.servers
    server_config = get_tool_server_config(agent_name, config)

    if not server_config:
        raise ValueError(_with_agent_listing(f"Agent '{agent_name}' not found in configuration."))

    if not server_config.agent_config:
        raise ValueError(f"Agent '{agent_name}' has no agent_config section")

    # Create the agent using the signature: Agent(name, system_config, server_config, registry, session_service)
    agent = Agent(agent_name, config, server_config, registry, session_service=session_service)
    
    # Make agent public so it shows up in tool lists if needed
    agent._tool_public = True
    
    # Register the agent in the registry
    registry.register(agent_name, agent)
    
    logger.info(f"Created agent '{agent_name}' with LLM profile '{server_config.agent_config.llm_profile}'")
    return agent
