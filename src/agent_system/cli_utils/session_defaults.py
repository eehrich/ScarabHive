"""Which agent and which LLM a continued session runs on.

Both CLI entry points ask the same question -- ``agent-cli`` at the prompt and
``agent-run`` for a one-shot task -- and they used to answer it differently:
whatever the config named that day. A session record carries the agent and the
profile it was started with, so continuing one is a matter of reading them
back and deciding what outranks what.

The decision is here, in one place, for a reason: the two entry points also
WRITE that record, and when the reader and the writer disagree the record
degrades a little on every run. agent-run used to overwrite a session's own
settings with its defaults, which is exactly what this module exists to stop.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def load_session_settings(session_manager: Any, session_user: str,
                                session_id: Optional[str]
                                ) -> tuple[Optional[str], Optional[str]]:
    """The agent and LLM profile stored in a session record, or (None, None).

    A session that cannot be read is not an error here -- ``--session`` also
    NAMES a new session, and reading a foreign one is refused on purpose. The
    load further down reports a real problem with the message that belongs to
    it.
    """
    if not session_id or session_manager is None:
        return None, None
    try:
        data = await session_manager.load_session(session_user, session_id)
    except Exception:
        logger.debug("No stored settings for session %s", session_id, exc_info=True)
        return None, None
    if not isinstance(data, dict):
        return None, None
    # The profile somebody picked: llm_profile names the one that RAN, picked or not -- continuing on it
    # froze the agent's own profile of that day, and a save then recorded it as a pick. A record from
    # before llm_profile_override has nothing else, and keeps what it always meant here.
    stored = data["llm_profile_override"] if "llm_profile_override" in data else data.get("llm_profile")
    return (data.get("agent_name") or None), (stored or None)


async def load_session_llm_params(session_manager: Any, session_user: str,
                                  session_id: Optional[str]) -> Optional[dict]:
    """The llm_params a session record carries (a chat's thinking level), or None when it has none.

    None, not {}: a session without params leaves a chat its own (agent-cli --llm-params) on /resume.
    Read like load_session_settings: a record that cannot be read is no error here.
    """
    if not session_id or session_manager is None:
        return None
    try:
        data = await session_manager.load_session(session_user, session_id)
    except Exception:
        logger.debug("No stored llm_params for session %s", session_id, exc_info=True)
        return None
    params = data.get("llm_params") if isinstance(data, dict) else None
    return dict(params) if isinstance(params, dict) and params else None


def usable_session_defaults(stored_agent: Optional[str], stored_llm: Optional[str],
                            config: Any) -> tuple[Optional[str], Optional[str]]:
    """Drop stored values the current configuration no longer knows.

    A session record is a memory, not an instruction. Sessions outlive the
    config that made them -- agents get renamed or removed, profiles get
    retired (measured on this repo: 707 of 2914 cli_user sessions name an
    agent that no longer exists) -- and the gates further down were written
    for names a PERSON typed, so they abort. Turning "continue this
    conversation" into a hard exit over a name the user never mentioned would
    be a worse bug than the one this whole feature fixes.

    An explicitly typed --agent/--llm is untouched by this: it still has to
    exist, and still aborts loudly when it does not.
    """
    agent = stored_agent
    if agent:
        plugins = getattr(config, "plugins", None)
        raw = plugins.servers.get(agent) if plugins else None
        # The RAW entry, the same gate the agent factory uses: the merged
        # config gives every tool server an agent_config.
        if not raw or not getattr(raw, "agent_config", None):
            logger.info("Session names agent '%s', which this config does not "
                        "define -- falling back to the default", agent)
            agent = None

    profile = stored_llm
    if profile:
        llm_system = getattr(config, "llm_system", None)
        profiles = getattr(llm_system, "profiles", None) or {}
        if profile not in profiles:
            logger.info("Session names LLM profile '%s', which llm.yaml no "
                        "longer defines -- falling back to the agent's own",
                        profile)
            profile = None

    return agent, profile


async def session_defaults(session_manager: Any, session_user: str,
                           session_id: Optional[str], config: Any
                           ) -> tuple[Optional[str], Optional[str]]:
    """Read a session's settings and drop what the config no longer knows."""
    stored_agent, stored_llm = await load_session_settings(
        session_manager, session_user, session_id)
    return usable_session_defaults(stored_agent, stored_llm, config)


def choose_agent_name(override: Optional[str], stored: Optional[str],
                      default: str) -> str:
    """Which agent runs: what was asked for, what the session used, the default.

    The stored name only outranks the CONFIG DEFAULT, never an explicit
    --agent. Without it, continuing a session with a bare --session handed the
    conversation to whatever agent the config happens to name today.
    """
    return override or stored or default


def choose_llm_profile(override: Optional[str], stored_llm: Optional[str],
                       stored_agent: Optional[str], entry_name: str,
                       agent_default: Optional[str]) -> Optional[str]:
    """Which LLM profile to force, or None to leave the agent on its own.

    Three conditions before a stored profile is used, and each one has a
    reason. An explicit --llm wins. The agent must be the one the profile was
    stored for -- a profile picked for another agent has no business being
    forced onto this one. And a profile that IS the agent's default is not an
    override at all; returning it would build a second client for nothing.
    """
    if override:
        return override
    if not stored_llm or entry_name != stored_agent:
        return None
    if stored_llm == agent_default:
        return None
    return stored_llm


def profile_for_record(used_profile: Optional[str],
                       agent_default: Optional[str]) -> Optional[str]:
    """What to write back as the session's LLM profile after a run.

    The profile that actually RAN, never the raw flag. session_service
    overwrites the record unconditionally, so reading the flag here wrote the
    agent's default over the session's own choice on every bare resume -- the
    choice then survived exactly one continuation.
    """
    return used_profile or agent_default
