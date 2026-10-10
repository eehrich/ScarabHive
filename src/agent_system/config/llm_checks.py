"""The agents' LLM settings checked after a load -- loud, never fatal: profile-keyed llm_params that
name no member of the agent's chains (dropped, and named), and chain members that are no profile at
all. One mistake in one agent must not keep every server down, so these report instead of raising.

A report made before logging is configured -- every entry point loads the config first -- is kept
and replayed once it is (flush_deferred_config_errors, called by setup_logging).
"""
from __future__ import annotations

import copy
import logging
from typing import Any

from .models import AgentSystemConfig

# The loader's one logger, the name it reported under before it was split into modules: a filter
# on load_settings.__module__ (the agent editor's quiet_loader) must keep catching all of it.
logger = logging.getLogger("agent_system.config.settings")


#: Already-reported (agent, keys) pairs. get_tool_server_config re-validates on
#: every agent creation; the operator needs the line once, not per spawn.
_reported_stale_llm_params: set = set()

#: Errors raised before any log handler existed. Every entry point loads the
#: config FIRST and configures logging after, so these would only ever reach
#: stderr — not the logfile the operator actually reads. setup_logging replays
#: them once a handler is there.
_deferred_config_errors: list = []


def flush_deferred_config_errors() -> int:
    """Re-emit config errors that were raised before logging was configured."""
    pending, _deferred_config_errors[:] = list(_deferred_config_errors), []
    for fmt, args in pending:
        logger.error(fmt, *args)
    return len(pending)


def _warn_stale_llm_params(name: str, raw: Any, agent_cfg: Any) -> None:
    """Name the agent whose profile-keyed llm_params were dropped.

    The validator sees the params but not which agent they belong to, so the
    comparison happens at the call sites, where both are at hand. Loud on
    purpose: the values silently stop applying, which is exactly what the
    strict check was built to prevent. It just must not cost the start.
    """
    if agent_cfg is None or not isinstance(raw, dict):
        return
    dropped = set(raw) - set(getattr(agent_cfg, "llm_params", None) or {})
    if not dropped:
        return
    marker = (name, frozenset(dropped))
    if marker in _reported_stale_llm_params:
        return
    _reported_stale_llm_params.add(marker)
    # available_llm_profiles instead of list(llm_profile): the field may be a
    # plain STRING, and list("turbo") spells the chain out letter by letter in
    # the error message the operator has to act on.
    chain = list(getattr(agent_cfg, "available_llm_profiles", None) or [])
    fmt = ("Agent '%s': llm_params for %s have NO effect — those profiles are in "
           "none of its LLM chains %s. Values: %s")
    args = (name, sorted(dropped), chain, {k: raw[k] for k in sorted(dropped)})
    logger.error(fmt, *args)
    if not logging.getLogger().handlers:
        _deferred_config_errors.append((fmt, args))


def _report_dropped_llm_params(data: dict, cfg: AgentSystemConfig) -> None:
    """Whole-config pass over the raw server dicts (load_settings).

    Except where the load could not judge. An entry that inherits -- its
    ``type`` names another server -- and does not set both chains itself was
    validated against chains that are not its own: the model default stands
    where the parent's chain will be once _resolve_server_inheritance has run.
    Its profile-keyed params were dropped for a mismatch that does not exist,
    and the merge after that had nothing left to keep. Measured on
    v4_beat_scorer: `llm_writer_simple: {max_tokens: 120000}` in the child,
    the chain inherited, and the key gone from get_tool_server_config with
    "in none of its LLM chains ['normal']".

    So those keys go back, and the judgement happens where the chain is known:
    get_tool_server_config validates the merged config the same way, and drops
    and names what is still stale there.
    """
    raw_servers = ((data.get("plugins") or {}).get("servers") or {})
    for name, server in (getattr(cfg.plugins, "servers", None) or {}).items():
        raw_server = raw_servers.get(name) or {}
        raw_agent = raw_server.get("agent_config") or {}
        raw = raw_agent.get("llm_params")
        agent_cfg = getattr(server, "agent_config", None)
        inherits = raw_server.get("type") in raw_servers
        if inherits and not {"llm_profile", "llm_profile_advanced"} <= set(raw_agent):
            _restore_llm_params(raw, agent_cfg)
            continue
        _warn_stale_llm_params(name, raw, agent_cfg)


def _restore_llm_params(raw: Any, agent_cfg: Any) -> None:
    """Put back the keys the load dropped. In place: assigning the field would
    run the validator again, without the context that makes it tolerant."""
    kept = getattr(agent_cfg, "llm_params", None)
    if not isinstance(raw, dict) or not isinstance(kept, dict):
        return
    for key in set(raw) - set(kept):
        kept[key] = copy.deepcopy(raw[key])


def _profile_names(entries: Any) -> list[str]:
    """The profiles a chain names: in a list, ``"+x"`` adds x and a ``"!x"`` entry
    removes one and names none; a string is one name as written (it merges nothing)."""
    if not isinstance(entries, list):
        return [str(entries)]
    return [e[1:] if e.startswith("+") else e for e in map(str, entries) if not e.startswith("!")]


def _report_unknown_llm_profiles(cfg: AgentSystemConfig) -> None:
    """Name chain members that do not exist as profiles.

    Only the PRIMARY profile is resolved at startup; fallback links are built
    lazily, in the failure path. A typo in one therefore stays invisible until
    the primary model rate-limits — the exact moment the fallback was
    configured for. Then it dies with "Profile 'x' not found" and the
    configured resilience turns out to never have existed.

    Loud, but not fatal — same rule as the stale llm_params next door: one
    typo in ONE agent must not keep every server down, and the primary
    profile of every OTHER agent still works.
    """
    profiles = set(getattr(cfg.llm_system, "profiles", None) or {})
    if not profiles:
        return  # nothing to compare against (partial config / tests)
    for name, server in (getattr(cfg.plugins, "servers", None) or {}).items():
        agent_cfg = getattr(server, "agent_config", None)
        if agent_cfg is None:
            continue
        # Only the chains the entry sets itself: an unset llm_profile is the model default "normal", not a choice;
        # the inherited chain is checked on the entry that sets it.
        own = agent_cfg.model_fields_set
        chain: list[str] = []
        if "llm_profile" in own:
            chain += _profile_names(agent_cfg.llm_profile)
        if "llm_profile_advanced" in own:
            chain += _profile_names(agent_cfg.llm_profile_advanced or [])
        chain = list(dict.fromkeys(chain))
        unknown = [p for p in chain if p not in profiles]
        if not unknown:
            continue
        # a merging list's first entry carries its prefix, so an inherited primary is never "unknown" here
        primary = agent_cfg.default_llm_profile if "llm_profile" in own else None
        fmt = ("Agent '%s': LLM profiles %s are in its chain but in no "
               "llm_system.profiles — %s. Chain: %s")
        args = (name, sorted(unknown),
                "THE AGENT WILL NOT START" if primary in unknown
                else "the fallback dies in the incident it exists for",
                list(chain))
        logger.error(fmt, *args)
        if not logging.getLogger().handlers:
            _deferred_config_errors.append((fmt, args))
