"""What the panel picks from and compares against: agent classes, profiles, tools, hooks, skills, prompt files and the
state of the running app. Everything is read from the registries the app itself uses."""
from __future__ import annotations

import fnmatch
from types import SimpleNamespace
from typing import Any, Callable, Optional

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.hooks.registry import get_hook_registry
from agent_system.plugins.catalog import PluginCatalog
from agent_system.runtime import VISIBILITIES, ServerDecl
from agent_system.servers.agent import server as agent_server
from agent_system.servers.agent.components.server_resolution import resolve_registry_server
from agent_system.servers.agent.tool_discovery import ToolDiscoveryService
from agent_system.servers.agent.tool_schema_builder import ToolSchemaBuilder, server_matches_patterns, tool_matches_patterns
from agent_system.skills.registry import SkillRegistry, default_skill_dirs

from .store import MANAGER_TYPE, PROMPT_SUFFIXES, Snapshot, Store, catalog_for, resolve_with

#: agent_config keys whose changes are reported one level down (`tools.allowed`).
NESTED_KEYS = ("tools", "hooks", "skills")

#: Keys outside agent_config that a reload changes on a running agent, as live_state names them.
RELOADABLE_ENTRY_KEYS = ("metadata.min_role",)


def reloadable_fields() -> list[str]:
    """What POST /admin/reload-config changes on a running agent; everything else needs a restart.

    Read on call, not on import: a test that swaps the Agent class must still be able to import the plugin.
    """
    return list(agent_server.Agent._RELOADABLE_AGENT_FIELDS)


def agent_verdicts(state: Any, catalog: PluginCatalog) -> tuple[set[str], dict[str, bool]]:
    """Which entries the running app knows to be agents, and the agent classes for all others.

    A declared entry is an agent when the runtime says so for that entry (its instance, or the lazy declaration).
    The classes are `agent`, the lazy plugin types (they build Agents by contract) and the types all of whose built
    instances are Agents: a type that built an agent and a tool server (writer_issues) is no class.
    """
    verdicts: dict[str, bool] = {}
    seen: dict[str, set[bool]] = {}
    runtime = getattr(state, "runtime", None)
    if runtime is not None:
        for name, decl in runtime.declarations().items():
            view = runtime.view(name)
            if view is not None:
                verdicts[name] = view.is_agent
                seen.setdefault(decl.type, set()).add(view.is_agent)
            elif decl.is_declared_agent:
                verdicts[name] = True
    classes = ({"agent"} | {t for t in catalog.types() if (catalog.manifest(t) or {}).get("lazy") is True}
               | {typ for typ, found in seen.items() if found == {True}})
    return classes, verdicts


def agent_classes(state: Any, catalog: PluginCatalog) -> set[str]:
    return agent_verdicts(state, catalog)[0]


def is_agent_rule(state: Any, catalog: PluginCatalog) -> Callable[[str, str], bool]:
    """`(name, base) -> bool`: the runtime's verdict for a declared entry, the class rule for the rest."""
    classes, verdicts = agent_verdicts(state, catalog)
    return lambda name, base: verdicts[name] if name in verdicts else base in classes


def reload_reaches(name: str) -> bool:
    """Whether POST /admin/reload-config refreshes this server: it walks the plugin registry."""
    from agent_system.plugins.tool_adapter import plugin_tool_registry
    from agent_system.services.config_reload import _reload_target
    adapter = plugin_tool_registry.get_server(name)
    return adapter is not None and _reload_target(getattr(adapter, "plugin_server", None)) is not None


def inherited(snap: Snapshot, name: str, typ: Optional[str]) -> Optional[ToolServerConfig]:
    """What an entry of type `typ` gets without keys of its own; `enabled` is never inherited (the runtime reads it
    from the entry itself). No type means the loader's default, basic_agent."""
    resolved = resolve_with(snap.config, name, {"type": typ} if typ else {})
    return resolved.model_copy(update={"enabled": False}) if resolved is not None else None


def visibility(resolved: ToolServerConfig, catalog: PluginCatalog) -> str:
    """The same answer the runtime gives: own metadata, then the plugin manifest, else private."""
    return ServerDecl(name="", type=resolved.type, server_config=resolved, plugin_metadata=catalog.manifest(resolved.type)).visibility


def _differing(a: dict, b: dict, nested: Callable[[str], bool]) -> list[str]:
    changed = []
    for key in set(a) | set(b):
        x, y = a.get(key), b.get(key)
        if x == y:
            continue
        if nested(key) and all(side is None or isinstance(side, dict) for side in (x, y)):
            x, y = x or {}, y or {}
            changed += [f"{key}.{sub}" for sub in set(x) | set(y) if x.get(sub) != y.get(sub)]
        else:
            changed.append(key)
    return changed


def changed_keys(live: dict, disk: dict) -> list[str]:
    """What differs between two resolved dumps, `enabled` aside: agent_config keys by their own name (`max_steps`,
    `tools.allowed`), everything else by its key, mappings one level down (`metadata.visibility`)."""
    live, disk = dict(live), dict(disk)
    for side in (live, disk):
        # Equal whenever this runs today (the runtime declares enabled entries only); not a setting the reload or a
        # restart would report as a change of the running agent.
        side.pop("enabled", None)
    agent = _differing(live.pop("agent_config", None) or {}, disk.pop("agent_config", None) or {},
                       lambda key: key in NESTED_KEYS)
    return sorted(agent + _differing(live, disk, lambda key: True))


def live_state(name: str, resolved: Optional[ToolServerConfig], enabled: bool, state: Any) -> dict:
    """How the disk entry relates to what the app runs. Without a runtime in the app there is nothing to compare.

    The running side is the declaration, with the agent_config and the role gate (metadata.min_role) of the built
    instance when there is one: a config reload changes those. Only a built agent the reload reaches can take the
    reloadable fields without a restart.
    """
    result: dict[str, Any] = {"state": None, "changed": [], "reload_fields": [], "restart": False}
    runtime = getattr(state, "runtime", None)
    if runtime is None:
        return result
    decl = runtime.describe(name)
    if decl is None:
        return {**result, "state": "new", "restart": True} if enabled else {**result, "state": "off"}
    if not enabled:
        return {**result, "state": "removed", "restart": True}
    if resolved is None:  # running, but the entry on disk does not resolve: a restart would not start it
        return {**result, "state": "changed", "restart": True}
    registry = getattr(state, "tool_registry", None)
    instance = registry.get(name) if registry is not None and name in registry.list() else None
    built = isinstance(instance, agent_server.Agent)
    live = decl.server_config.model_dump(mode="json")
    if isinstance(instance, agent_server.Agent):
        live["agent_config"] = instance.agent_config.model_dump(mode="json") if instance.agent_config else None
        # The role gate is the instance's as well: a reload moves it (Agent.reload_config), the declaration stays.
        if (live.get("metadata") or {}).get("min_role") != instance.min_role:
            live["metadata"] = {**(live.get("metadata") or {}), "min_role": instance.min_role}
    changed = changed_keys(live, resolved.model_dump(mode="json"))
    reload_fields = ([key for key in changed if key in reloadable_fields() or key in RELOADABLE_ENTRY_KEYS]
                     if built and reload_reaches(name) else [])
    return {"state": "changed" if changed else "in_sync", "changed": changed, "reload_fields": reload_fields,
            "restart": len(reload_fields) < len(changed)}


def profiles(config: AgentSystemConfig) -> list[dict]:
    """The shape of GET /llm/profiles, from the given config."""
    llm = config.llm_system
    rows = []
    for name, profile in ((llm.profiles if llm is not None else None) or {}).items():
        model = (llm.models or {}).get(profile.model_ref)
        rows.append({"name": name, "model_ref": profile.model_ref, "provider": model.provider if model else None,
                     "model": model.model if model else None, "description": profile.description or name})
    return sorted(rows, key=lambda row: row["name"].lower())


def skills(config: AgentSystemConfig) -> list[dict]:
    """A private scan with the config's roots: the process registry serves prompt rendering and stays untouched."""
    registry = SkillRegistry()
    registry.discover(list(getattr(getattr(config, "skills", None), "skill_dirs", None) or default_skill_dirs()))
    return [{"name": skill.name, "description": skill.description} for skill in registry.list_skills()]


def hooks() -> list[dict]:
    """One row per hook name -- the override is per name -- with every type it is registered for."""
    registry = get_hook_registry()
    types: dict[str, list[str]] = {}
    for hook_type, names in registry.list_hooks().items():
        for name in names:
            types.setdefault(name, []).append(hook_type)
    rows = []
    for name in sorted(types):
        info = registry.get_hook_info(name) or {}
        rows.append({"name": name, "types": sorted(types[name]), "description": info.get("description", ""),
                     "enabled": info.get("enabled", True), "order": info.get("order"), "timeout": info.get("timeout")})
    return rows


def prompt_files(store: Store, snap: Snapshot) -> list[str]:
    """Every template an entry uses, plus the prompt files under the config directory."""
    found = set()
    for resolved in snap.resolved.values():
        template = resolved.agent_config.system_template if resolved is not None and resolved.agent_config else None
        if template:
            found.add(store.rel(store.root / template))
    for suffix in PROMPT_SUFFIXES:
        found |= {store.rel(path) for path in store.config_path.parent.glob(f"**/prompts/**/*{suffix}")}
    return sorted(found)


def meta(store: Store, state: Any) -> dict:
    snap = store.snapshot()
    catalog = catalog_for(snap.config)
    return {
        "classes": [{"name": name, "description": (catalog.manifest(name) or {}).get("description")}
                    for name in sorted(agent_classes(state, catalog))],
        "profiles": profiles(snap.config),
        "skills": skills(snap.config),
        "hooks": hooks(),
        "prompt_files": prompt_files(store, snap),
        "visibility": list(VISIBILITIES),
        "reload_fields": reloadable_fields(),
        "sub_agents": MANAGER_TYPE in catalog.types(),
    }


class _InternalServers:
    """Stands in for the agent's tool integration: the plugin registry, and no external MCP servers."""

    def __init__(self):
        from agent_system.plugins.tool_adapter import plugin_tool_registry
        self.tool_integration = SimpleNamespace(initialized=True, plugin_registry=plugin_tool_registry)

    async def build_tool_schemas(self, available_tools: list[str]) -> tuple[list, dict]:
        return [], {}


async def tool_catalog(state: Any) -> list[dict]:
    """The tools an agent can be given, found the way tool discovery finds them: every server of the plugin registry
    plus every tool-visible server of the app's registry, each mapped to its tools as the schema build does it."""
    registry = getattr(state, "tool_registry", None)
    runtime = getattr(state, "runtime", None)
    integration = _InternalServers()
    visible = ToolDiscoveryService("agent_editor", None, integration, registry)._is_tool_visible
    names = list(dict.fromkeys([*integration.tool_integration.plugin_registry.list_servers(),
                                *(name for name in (registry.list() if registry is not None else []) if visible(name))]))
    builder = ToolSchemaBuilder("agent_editor", integration,
                                lambda name: resolve_registry_server(registry, integration, name) if name in names else None)
    schemas, mapping, _, _ = await builder.build_schemas(list(names))
    tools: dict[str, list[dict]] = {name: [] for name in names}
    for schema in schemas:
        function = schema.get("function") or {}
        server = mapping.get(function.get("name"))
        if server in tools:
            tools[server].append({"name": function["name"], "description": function.get("description") or ""})
    rows = []
    for name in sorted(tools):
        decl = runtime.describe(name) if runtime is not None else None
        rows.append({"server": name, "type": decl.type if decl else None,
                     "tools": sorted(tools[name], key=lambda tool: tool["name"])})
    return rows


def external_servers(config: AgentSystemConfig) -> dict[str, bool]:
    """The external MCP servers of the loaded config (`external_servers.remote_servers`, whichever file declares
    them), each with whether it is enabled."""
    remote = getattr(config.external_servers, "remote_servers", None) or {}
    return {name: server.enabled for name, server in remote.items()}


def problems(store: Store, snap: Snapshot, name: str, skill_names: set[str],
             tools: Optional[Callable[[list[str], list[str]], dict]]) -> list[str]:
    """What the editor warns about for this agent, for the list's filter: an entry that does not resolve, profiles
    that do not exist, a missing prompt template, unknown skills, and (with `tools`, effective_tools for a pair of
    lists) tool patterns that match nothing or name an external server that is off."""
    if name in snap.resolve_errors:
        return [f"Does not resolve: {snap.resolve_errors[name]}"]
    resolved = snap.resolved.get(name)
    agent = resolved.agent_config if resolved is not None else None
    if agent is None:
        return []
    # no profiles at all is no exception here, unlike the core's log check: every chain fails, as the model tab says
    profile_names = set(getattr(snap.config.llm_system, "profiles", None) or {})
    chain = [agent.llm_profile] if isinstance(agent.llm_profile, str) else list(agent.llm_profile)
    found = [f"Unknown model profile: {profile}" for profile in dict.fromkeys(chain + (agent.llm_profile_advanced or []))
             if profile not in profile_names]
    if agent.system_template and not agent.system_prompt and not (store.root / agent.system_template).is_file():
        found.append(f"Prompt template not found: {store.rel(store.root / agent.system_template)}")
    if agent.skills is not None:
        found += [f"Unknown skill: {skill}" for skill in dict.fromkeys(agent.skills.always + agent.skills.on_demand)
                  if skill not in skill_names]
    if tools is not None:
        result = tools(list(agent.tools.allowed or []), list(agent.tools.blocked or []))
        found += [f"Matches no tool: {pattern}" for pattern in result["unmatched"]]
        # tools.deferred holds back allowed tools only: a pattern that names none of them defers nothing
        granted = [tool.split("/", 1) for tool in result["tools"]]
        found += [f"Deferred matches no allowed tool: {pattern}" for pattern in dict.fromkeys(agent.tools.deferred or [])
                  if pattern not in result["external"]
                  and not any(tool_matches_patterns(tool, server, [pattern]) for server, tool in granted)]
        # a blocked pattern for a server that is off blocks nothing yet: no problem
        found += [f"External server is off: {pattern}" for pattern, on in result["external"].items()
                  if not on and pattern in result["allowed"]]
    return found


def external_reach(pattern: str, servers: dict[str, bool]) -> Optional[bool]:
    """Whether a pattern can grant a tool of a server in `servers` ({name: enabled}): None when it cannot, else
    whether one of the servers it can reach is enabled.

    An external tool is `server.tool` to discovery and `server.tool` + `server_tool` to the tool filter
    (tool_schema_builder). Measured against both: only the exact dotted name and a pattern ending in `*` pass both --
    `*.echo` and `everything.ech?` pass discovery and die in the filter, a `/` never matches at all. Which tools a
    server has is unknown here, so a pattern that ends in `*` counts as reaching every server its head can match
    (fnmatch, as the runtime matches, case included).
    """
    if "/" in pattern:
        return None
    if not any(character in pattern for character in "*?["):
        head, dot, tool = pattern.partition(".")
        return servers.get(head) if dot and tool else None
    if not pattern.endswith("*"):
        return None
    head, dot, _ = pattern.partition(".")
    # with a dot the head faces the server name, without one the whole pattern faces `server.` and the `*` takes the rest
    reached = [on for name, on in servers.items()
               if (fnmatch.fnmatch(name, head) if dot else fnmatch.fnmatch(f"{name}.", pattern))]
    return any(reached) if reached else None


def effective_tools(catalog: list[dict], allowed: list[str], blocked: list[str],
                    external: Optional[dict[str, bool]] = None) -> dict:
    """Which catalog tools an agent with these lists gets, in the agent's order: discovery lets a server through
    when the whole allowed list matches it, the schema build keeps its tools the whole list matches, then drops the
    blocked ones (a tool rule, no server pass).

    A pattern that names no catalogue tool but can reach a server of `external` (`everything.*`, `ever*`; see
    external_reach) is not judged: the catalogue holds no external tools. `external` maps such patterns to whether a
    server they reach is enabled.

    `per_tool` names, for every tool a pattern matches, the allowed patterns that grant it (within the server pass)
    and the blocked patterns that match it (granted or not); `counts` is how many tools each pattern names there.
    """
    per_tool: dict[str, dict] = {}
    counts = dict.fromkeys([*allowed, *blocked], 0)
    tools = []
    for row in catalog:
        server = row["server"]
        passes = server_matches_patterns(server, allowed)
        for tool in row["tools"]:
            name = tool["name"]
            allowed_by = [p for p in allowed if tool_matches_patterns(name, server, [p])] if passes else []
            blocked_by = [p for p in blocked if tool_matches_patterns(name, server, [p])]
            for pattern in [*allowed_by, *blocked_by]:
                counts[pattern] += 1
            if allowed_by or blocked_by:
                per_tool[f"{server}/{name}"] = {"allowed_by": allowed_by, "blocked_by": blocked_by}
            if allowed_by and not blocked_by:
                tools.append(f"{server}/{name}")
    reach = {pattern: external_reach(pattern, external or {}) for pattern in counts if not counts[pattern]}
    outside = {pattern: on for pattern, on in reach.items() if on is not None}
    return {
        "allowed": allowed,
        "blocked": blocked,
        "tools": tools,
        "per_tool": per_tool,
        "counts": counts,
        "unmatched": [pattern for pattern in dict.fromkeys(allowed + blocked) if not counts[pattern] and pattern not in outside],
        "external": outside,
    }
