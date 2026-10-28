"""Overlays: an arm's change to an agent, for one run, without touching the agent's YAML.

Two kinds, one merge:

- An **admin overlay** (a suite's arm) may set anything the agent YAML may set -- the admin can write the
  YAML too. ``apply_overlay`` merges it.
- A **machine overlay** (written by a model, e.g. a tuner proposing a variant) is text a model wrote, so it
  may only touch an allowlist, and the result may only NARROW what the agent can do:
  ``machine_overlay_problems`` says why one is refused. Nothing in it widens rights -- a person does that,
  outside of any tuner.
"""
from __future__ import annotations

from typing import Any, Dict, List

from agent_system.config.merging import _deep_merge_dict
from agent_system.config.models import AgentConfig, AgentSystemConfig

#: agent_config fields a machine overlay may set.
MACHINE_FIELDS = frozenset({"system_prompt", "template_vars", "skills", "tools", "llm_params"})
#: Keys of one llm_params entry a machine overlay may set; the last two only up to the base's value.
MACHINE_LLM_PARAMS = frozenset({"temperature", "thinking_level", "max_tokens"})
#: Ascending, as OpenRouter's reasoning effort (config/models/llm.py).
_THINKING = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def apply_overlay(base: AgentConfig, overlay: Dict[str, Any]) -> AgentConfig:
    """*base* with *overlay* merged over it, as a config entry over the one it inherits from: mappings merge,
    ``+x``/``!x`` extend or prune a list, a plain list replaces it. Validated like a config entry."""
    if not overlay:
        return base
    merged = _deep_merge_dict(base.model_dump(), overlay)
    return AgentConfig.model_validate(merged)


def layer_agents(config: AgentSystemConfig, overlays: Dict[str, Dict[str, Any]]) -> AgentSystemConfig:
    """*config* with each named agent's server entry replaced by what it resolves to, its overlay merged over
    that -- before a registry is built from it, so every run of the agent sees it: a sub-agent a machine or a
    panel starts as well as the suite's own. Every agent that inherits from a named one inherits its overlay
    too; a named heir gets its parent's and its own, whatever order the suite names them in. Raises ValueError
    for a name the configuration lacks."""
    from agent_system.config.settings import get_tool_server_config

    servers = config.plugins.servers if config.plugins else {}

    def named_ancestors(name: str) -> int:
        # A layered entry holds what it resolved to and follows its parent no longer: parents go first.
        chain, entry = set(), servers.get(name)
        while entry is not None and entry.type in servers and entry.type != name and entry.type not in chain:
            name = entry.type
            chain.add(name)
            entry = servers.get(name)
        return len(chain & set(overlays))

    for name in sorted(overlays or {}, key=named_ancestors):
        overlay = overlays[name]
        resolved = get_tool_server_config(name, config)
        if resolved is None:
            raise ValueError(f"'{name}' is not a server of this configuration")
        servers = {**config.plugins.servers,
                   name: resolved.model_copy(update={"agent_config": apply_overlay(resolved.agent_config, overlay)})}
        config = config.model_copy(update={"plugins": config.plugins.model_copy(update={"servers": servers})})
    return config


def machine_overlay_problems(base: AgentConfig, overlay: Dict[str, Any]) -> List[str]:
    """Why *overlay*, written by a model, may not run over *base* -- empty when it may."""
    problems = [f"field '{key}' is not open to a machine overlay" for key in overlay if key not in MACHINE_FIELDS]
    tools = overlay.get("tools") or {}
    if not isinstance(tools, dict) or set(tools) - {"allowed", "blocked"}:
        problems.append("tools: only 'allowed' and 'blocked' may change")
    template_vars = overlay.get("template_vars") or {}
    if not isinstance(template_vars, dict) or not all(isinstance(v, str) for v in template_vars.values()):
        problems.append("template_vars: strings only")
    problems += _llm_params_problems(base.llm_params or {}, overlay.get("llm_params") or {})
    if problems:
        return problems

    try:
        result = apply_overlay(base, overlay)
    except Exception as e:  # noqa: BLE001 - a broken overlay is a refusal with its reason
        return [f"does not validate: {e}"]
    # Monotonic: the result may only narrow. Compared after the merge, so '+x' and '!x' count as what they do.
    widened = set(result.tools.allowed or []) - set(base.tools.allowed or [])
    if widened:
        problems.append(f"tools.allowed gains {sorted(widened)}")
    unblocked = set(base.tools.blocked or []) - set(result.tools.blocked or [])
    if unblocked:
        problems.append(f"tools.blocked loses {sorted(unblocked)}")
    known = set(base.skills.always + base.skills.on_demand) if base.skills else set()
    new_skills = set(result.skills.always + result.skills.on_demand) - known if result.skills else set()
    if new_skills:
        problems.append(f"skills gains {sorted(new_skills)}")
    return problems


def _llm_params_problems(base: Dict[str, Any], overlay: Any) -> List[str]:
    if not isinstance(overlay, dict):
        return ["llm_params: a mapping of profile to parameters"]
    problems = []
    for profile, params in overlay.items():
        if not isinstance(params, dict):
            problems.append(f"llm_params.{profile}: a mapping")
            continue
        extra = set(params) - MACHINE_LLM_PARAMS
        if extra:
            problems.append(f"llm_params.{profile}: {sorted(extra)} not open to a machine overlay")
        own = base.get(profile) or {}
        if "max_tokens" in params and not (isinstance(own.get("max_tokens"), int)
                                           and isinstance(params["max_tokens"], int)
                                           and params["max_tokens"] <= own["max_tokens"]):
            problems.append(f"llm_params.{profile}.max_tokens: at most the agent's own")
        if "thinking_level" in params and not (own.get("thinking_level") in _THINKING
                                               and params["thinking_level"] in _THINKING
                                               and _THINKING.index(params["thinking_level"])
                                               <= _THINKING.index(own["thinking_level"])):
            problems.append(f"llm_params.{profile}.thinking_level: at most the agent's own")
    return problems
