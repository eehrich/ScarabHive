"""Where the panels of the plugins running here are, for a panel that links to another plugin's panel; and the plugin
servers themselves, for a panel that asks another plugin.

A link <a href="<url>" data-pk-panel> opens it beside the linking panel in the shell (panel-kit.js, openPanel).
Who may open a panel is the catalogue's to say; the shell tells a viewer who may not.
"""

from __future__ import annotations


def plugin_panels(exclude: str = "") -> dict[str, str]:
    """Plugin type or instance name -> the URL of its panel, for the running plugins that have one.

    A plugin may be named by its type while its instance runs under another name: the type leads to it too (to the
    first by name, where several run). ``exclude``: an instance left out, e.g. the asking plugin's own.
    """
    from agent_system.plugins.tool_adapter import plugin_tool_registry
    from agent_system.plugins.web_adapter import plugin_web_registry

    panels: dict[str, str] = {}
    for instance in sorted(plugin_web_registry.web_plugins):
        adapter = plugin_tool_registry.get_server(instance)
        if instance == exclude or adapter is None:
            continue
        panel = (adapter.plugin_schema.get("web_ui") or {}).get("panel")
        endpoint = panel.get("endpoint") if isinstance(panel, dict) else None
        if not isinstance(endpoint, str) or not endpoint.startswith(f"/plugins/{instance}/"):
            continue
        panels[instance] = endpoint
        kind = str(getattr(getattr(adapter.plugin_server, "server_config", None), "type", "") or "")
        if kind and kind != exclude:
            panels.setdefault(kind, endpoint)
    return panels


def running_plugins(exclude: str = "") -> dict[str, object]:
    """Plugin type or instance name -> the plugin server running here, for a panel that asks other plugins (an overview
    panel asking for their key figures). A type leads to its instance as in plugin_panels; ``exclude``: an
    instance left out."""
    from agent_system.plugins.tool_adapter import plugin_tool_registry

    servers: dict[str, object] = {}
    for instance in sorted(plugin_tool_registry.list_servers()):
        adapter = plugin_tool_registry.get_server(instance)
        if instance == exclude or adapter is None:
            continue
        server = getattr(adapter, "plugin_server", adapter)
        servers[instance] = server
        kind = str(getattr(getattr(server, "server_config", None), "type", "") or "")
        if kind and kind != exclude:
            servers.setdefault(kind, server)
    return servers
