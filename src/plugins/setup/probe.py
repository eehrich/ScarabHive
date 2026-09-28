"""Does the chat answer? One short request through the default agent's first LLM profile.

The honest check of a key: what the chat would send, the way an agent builds
its client (create_llm_from_profile). A key that is set can still be refused;
a model whose entry names no key can still find one in its host's variable.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from agent_system.config.settings import get_tool_server_config
from agent_system.llm.factory import create_llm_from_profile
from agent_system.llm.models import ChatMessage

#: Long enough for a cold provider, short enough for a panel waiting on it.
PROBE_TIMEOUT = 60.0


def default_chat_profile(config: Any) -> tuple[Optional[str], Optional[str]]:
    """(default agent, its first LLM profile); None where the configuration names none."""
    agent = getattr(config, "default_agent", None)
    server = get_tool_server_config(agent, config) if agent else None
    agent_config = getattr(server, "agent_config", None) if server else None
    if agent_config is None:
        return agent, None
    return agent, agent_config.default_llm_profile


def runs_as_a_batch(config: Any, profile: str) -> bool:
    system = getattr(config, "llm_system", None)
    if system is None:
        return False
    entry = system.profiles.get(profile)
    model = system.models.get(entry.model_ref) if entry else None
    return getattr(model, "provider", None) == "batch"


async def probe_chat(config: Any, profile: Optional[str] = None,
                     timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    """Send one short request through *profile* (default: the default agent's first) and say how it went.

    The answer text is not returned -- only whether one came, and else the error
    the provider gave, which names a refused key as such.
    """
    agent, first = default_chat_profile(config)
    profile = profile or first
    result: dict[str, Any] = {"agent": agent, "profile": profile}
    if not profile:
        return {**result, "ok": False, "error": f"the default agent {agent!r} names no LLM profile"}
    if runs_as_a_batch(config, profile):
        # the batch client queues the request as a real batch job and waits for the batch
        return {**result, "ok": False,
                "error": f"profile {profile!r} runs through the batch queue: a probe would queue a batch job"}
    try:
        client = create_llm_from_profile(config, profile)
        result["model"] = getattr(client, "model", None)
        answer = await asyncio.wait_for(
            client.chat([ChatMessage(role="user", content="Answer with the single word OK.")]), timeout)
    except asyncio.TimeoutError:
        return {**result, "ok": False, "error": f"no answer within {timeout:.0f} s"}
    except Exception as error:  # the provider's refusal is the finding, not a crash
        return {**result, "ok": False, "error": f"{type(error).__name__}: {str(error)[:300]}"}
    if not (answer or "").strip():
        return {**result, "ok": False, "error": "the model answered with nothing"}
    return {**result, "ok": True}
