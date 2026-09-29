"""The agent a run starts on, by name.

One rule for the API, agent-cli, agent-run, the chat's /agent and the plugins
that build their own helper agents: the registered agent, or a fresh build
from its MERGED server config. Each caller keeps only its own answer to
NotAnAgent (exit, HTTP error, a generic fallback).
"""
from __future__ import annotations

import logging
from typing import Any

from ...config.settings import get_tool_server_config
from .server import Agent

logger = logging.getLogger(__name__)


class NotAnAgent(ValueError):
    """The name cannot run as an agent here; the message lists the names that can."""

    def __init__(self, reason: str, available: list[str]) -> None:
        self.available = available
        if available:
            listing = "\n\nAvailable agents:\n  " + "\n  ".join(available)
        else:
            listing = "\n\nNo agents are configured. Check your config files."
        super().__init__(reason + listing)


def agent_entry_names(config: Any) -> list[str]:
    """Names this configuration can build an entry agent for, sorted.

    The RAW ``plugins.servers`` entry with an ``agent_config`` -- the gate
    entry_agent applies, and what the chat's /agent offers: a listing that
    offers a name the build then rejects is worse than none.

    Raw on purpose. plugins.default_config carries an agent_config, so every
    MERGED config has one and a merged gate would wave through any tool server
    (measured on the real config 25.09.2026: 101 of 236 servers have no raw
    agent_config, and no ``type:`` chain of theirs runs through an entry
    that has one -- all tool servers, no agent among them).
    """
    servers = getattr(getattr(config, "plugins", None), "servers", None) or {}
    return sorted(name for name, entry in servers.items()
                  if getattr(entry, "agent_config", None))


def entry_agent(name: str, config: Any, registry: Any, session_service: Any = None) -> Agent:
    """The agent for *name*: the registered one, or a fresh build, registered.

    A registered agent is rewired to THIS registry and, when one is given, this
    session service -- bootstrap injects both, but an agent picked up later
    (the chat's /agent) would otherwise save into whatever service built it.
    None keeps the service the agent has.

    A fresh build uses the MERGED server config (default_config plus the
    ``type:`` chain), what bootstrap gives every other agent: the raw entry
    alone ran 133 of 203 agents with a max_steps of 20 where the merged value
    is 100 or 30 (measured 01.09.2026).

    Raises NotAnAgent when the name is registered as something else --
    building one would push that server out of the registry -- or has no
    agent_config of its own.
    """
    names = agent_entry_names(config)
    if name in registry.list():
        existing = registry.get(name)
        if not isinstance(existing, Agent):
            raise NotAnAgent(f"'{name}' is registered as {type(existing).__name__}, not an Agent.", names)
        existing.registry = registry
        if session_service is not None:
            existing._session_service = session_service
        return existing
    if name not in names:
        servers = getattr(getattr(config, "plugins", None), "servers", None) or {}
        reason = (f"'{name}' is a tool server, not an agent." if name in servers
                  else f"Agent '{name}' not found in configuration.")
        raise NotAnAgent(reason, names)
    agent = Agent(name, config, get_tool_server_config(name, config), registry,
                  session_service=session_service)
    registry.register(name, agent)
    logger.info("Built agent '%s' (%s)", name, agent.agent_config.llm_profile if agent.agent_config else "-")
    return agent
