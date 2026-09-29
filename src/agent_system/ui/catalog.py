"""The panel catalogue: every window the shell can open, for one viewer.

Three sources, one shape:

* core panels served by agent_system.ui.routes (system, session, settings, help, kit),
* admin dashboards that exist only while their feature is on,
* plugin panels, declared in a plugin's schema.yaml under ``web_ui.panel``::

      web_ui:
        panel:
          endpoint: "/plugins/{{ name }}/"
          title: "Message Debugger"
          description: "Inspect LLM requests and hook activity"
          icon: bug                  # a name in static/kit/icons.svg
          category: debug            # one of CATEGORIES
          keywords: [requests, hooks]
          window: {width: 720, height: 520}
          contexts:                  # optional entry points from the chat
            request: "/plugins/{{ name }}/?request_id={request_id}"

Who sees a plugin panel is not declared here: it is whoever the route security
in config.yaml lets open its endpoint (see roles_allowed). A plugin panel lives
under its own ``/plugins/<instance>/``.

The launcher, the command palette and the chat's context links all read this
one list, so a panel is findable everywhere or nowhere.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from agent_system.auth.enforcement import ROLE_HIERARCHY

#: Fixed on purpose: a plugin picks one, it does not invent its own.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("session", "Session"),
    ("writer", "Writer"),
    ("context", "Context & memory"),
    ("agents", "Agents & tools"),
    ("debug", "Debugging"),
    ("system", "System"),
    ("admin", "Administration"),
)
CATEGORY_IDS = frozenset(key for key, _ in CATEGORIES)
#: A context names the one value the shell puts into the panel's URL.
CONTEXTS = {"session": "session_id", "request": "request_id"}
DEFAULT_WINDOW = {"width": 720, "height": 540}
PANEL_KEYS = frozenset({"endpoint", "title", "description", "icon", "category",
                        "keywords", "window", "contexts"})
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
#: What a browser strips from a URL or reads as a slash before resolving it.
_UNSAFE_IN_PATH = re.compile(r"[\x00-\x20\x7f\\]")


@dataclass
class Panel:
    id: str
    title: str
    url: str
    icon: str
    category: str
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    window: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_WINDOW))
    contexts: dict[str, str] = field(default_factory=dict)
    #: Instances of one plugin share it (their common title); the launcher folds them together.
    group: str = ""
    #: The plugin's guide in the Help panel (its id) when it has one or a README; the shell's help button opens it.
    help: str = ""

    def visible_to(self, role: str) -> bool:
        return not self.roles or role in self.roles


class PanelSpecError(ValueError):
    """A plugin's web_ui.panel block does not describe a usable panel."""


def core_panels(*, audit_enabled: bool, profiling_enabled: bool,
                memory_profiling_enabled: bool) -> list[Panel]:
    panels = [
        Panel("session", "Session", "/ui/panels/session", "message-square", "session",
              "Context variables, message counts and token estimate of the active session",
              ["context", "tokens", "variables"], window={"width": 460, "height": 620},
              contexts={"session": "/ui/panels/session?session_id={session_id}"}),
        Panel("system", "System", "/ui/panels/system", "activity", "system",
              "Status with its reasons, deployed commit, running requests, tool servers and external MCP servers",
              ["health", "status", "tools", "mcp", "requests", "commit", "restart"], window={"width": 760, "height": 600}),
        Panel("settings", "Settings", "/ui/panels/settings", "settings", "system",
              "Profile, password and appearance", ["profile", "password", "theme", "account"],
              window={"width": 520, "height": 600}),
        Panel("help", "Help", "/ui/panels/help", "circle-help", "system",
              "The ScarabHive manual and every plugin's guide, in AmigaGuide format",
              ["documentation", "manual", "guide", "amigaguide", "docs", "plugins"],
              window={"width": 860, "height": 680}),
        Panel("ui_kit", "UI Kit", "/ui/kit", "layers", "system",
              "Every standard control in every state -- the reference for panel authors",
              ["components", "design", "tokens"], window={"width": 1000, "height": 720}),
    ]
    if audit_enabled:
        panels.append(Panel("security_audit", "Security Audit", "/ui/panels/security_audit", "shield", "admin",
                            "Every request with who sent it and what it was answered",
                            ["audit", "security", "access", "denied"], roles=["admin"],
                            window={"width": 980, "height": 640}))
    if profiling_enabled:
        panels.append(Panel("performance", "Performance", "/ui/panels/performance", "gauge", "admin",
                            "Running and slow requests, time per route, event loop lag, async tasks and threads",
                            ["profiling", "cpu", "latency", "lag", "threads", "gc"], roles=["admin"],
                            window={"width": 980, "height": 640}))
    if memory_profiling_enabled:
        panels.append(Panel("memory_profile", "Memory Profile", "/ui/panels/memory_profile", "hard-drive", "admin",
                            "Process memory, object counts by type, growth against a baseline and allocations",
                            ["profiling", "memory", "leak", "tracemalloc", "gc"], roles=["admin"],
                            window={"width": 980, "height": 640}))
    return panels


def _text(instance: str, spec: dict[str, Any], key: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PanelSpecError(f"{instance}: web_ui.panel needs {key} as text")
    return value


def _string_list(instance: str, spec: dict[str, Any], key: str) -> list[str]:
    value = spec.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PanelSpecError(f"{instance}: {key} must be a list of strings")
    return list(value)


def _local_path(instance: str, what: str, url: str) -> None:
    """A plain path on this server: no host, no dot segment a browser would resolve away.

    Spaces, control characters and backslashes are refused outright -- the
    browser strips or rewrites them, so "/\\t/host" and "/x/.. " would resolve
    to somewhere other than what is checked here.
    """
    segments = {unquote(segment) for segment in urlsplit(url).path.split("/")}
    if not url.startswith("/") or url.startswith("//") or _UNSAFE_IN_PATH.search(url) or {".", ".."} & segments:
        raise PanelSpecError(f"{instance}: {what} must be a plain path on this server, got {url!r}")


def _context_urls(instance: str, spec: dict[str, Any], endpoint: str) -> dict[str, str]:
    contexts = spec.get("contexts") or {}
    if not isinstance(contexts, dict) or not all(isinstance(url, str) for url in contexts.values()):
        raise PanelSpecError(f"{instance}: contexts must map a context to a URL")
    for context, url in contexts.items():
        if context not in CONTEXTS:
            raise PanelSpecError(f"{instance}: unknown context {context!r}; known: {sorted(CONTEXTS)}")
        _local_path(instance, f"context {context}", url)
        # The shell opens a context as the rest of the URL below the panel's own.
        rest = url[len(endpoint):]
        if not url.startswith(endpoint) or not (endpoint.endswith("/") or rest[:1] in ("", "/", "?")):
            raise PanelSpecError(f"{instance}: context {context} must lie below the endpoint {endpoint!r}")
        if set(_PLACEHOLDER.findall(url)) != {CONTEXTS[context]}:
            raise PanelSpecError(f"{instance}: context {context} must use exactly {{{CONTEXTS[context]}}}")
    return dict(contexts)


def _window(instance: str, spec: dict[str, Any]) -> dict[str, int]:
    window = spec.get("window") or {}
    if not isinstance(window, dict):
        raise PanelSpecError(f"{instance}: window must be {{width: <px>, height: <px>}}")
    size = {key: window.get(key, default) for key, default in DEFAULT_WINDOW.items()}
    if set(window) - set(DEFAULT_WINDOW) or not all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in size.values()):
        raise PanelSpecError(f"{instance}: window must be {{width: <px>, height: <px>}}")
    return size


def plugin_panel(instance: str, spec: Any, icons: Iterable[str], *, root: str | None = None) -> Panel:
    """A Panel from a plugin instance's rendered ``web_ui.panel`` block.

    root: where the endpoint must lie -- the instance's own ``/plugins/<instance>/``,
    the routes its schema router serves and the security headers let the shell frame.
    """
    if not isinstance(spec, dict):
        raise PanelSpecError(f"{instance}: web_ui.panel must be a mapping")
    unknown = sorted(str(key) for key in set(spec) - PANEL_KEYS)
    if unknown:
        raise PanelSpecError(f"{instance}: web_ui.panel has unknown keys {unknown}; known: {sorted(PANEL_KEYS)}")
    endpoint = _text(instance, spec, "endpoint")
    _local_path(instance, "endpoint", endpoint)
    root = f"/plugins/{instance}/" if root is None else root
    if not endpoint.startswith(root):
        raise PanelSpecError(f"{instance}: endpoint must lie under {root}, got {endpoint!r}")
    category = _text(instance, spec, "category")
    if category not in CATEGORY_IDS:
        raise PanelSpecError(f"{instance}: category {category!r} is not one of {sorted(CATEGORY_IDS)}")
    icon = _text(instance, spec, "icon")
    if icon not in set(icons):
        raise PanelSpecError(f"{instance}: icon {icon!r} is not in the kit sprite")
    description = spec.get("description") or ""
    if not isinstance(description, str):
        raise PanelSpecError(f"{instance}: description must be text")
    return Panel(
        id=instance,
        title=_text(instance, spec, "title"),
        url=endpoint,
        icon=icon,
        category=category,
        description=description,
        keywords=_string_list(instance, spec, "keywords"),
        window=_window(instance, spec),
        contexts=_context_urls(instance, spec, endpoint),
    )


def roles_allowed(*policies: tuple[bool, Any]) -> list[str]:
    """The roles all of these route policies ``(requires_auth, min_role)`` let in; [] = everyone.

    Ranked like the enforcement ranks them (an unknown role counts as none), so
    the catalogue lists a panel for the roles that pass every layer guarding it.
    """
    minimum = max((ROLE_HIERARCHY.get(str(min_role).lower(), 0)
                   for requires_auth, min_role in policies if requires_auth and min_role), default=0)
    if not minimum:
        return []
    return sorted(role for role, level in ROLE_HIERARCHY.items() if level >= minimum)


def disambiguate(panels: list[Panel]) -> list[Panel]:
    """Instances of one plugin share a title: the instance name tells them apart, the title groups them."""
    counts: dict[str, int] = {}
    for panel in panels:
        counts[panel.title] = counts.get(panel.title, 0) + 1
    for panel in panels:
        if counts[panel.title] > 1:
            panel.group = panel.title
            panel.title = f"{panel.title} · {panel.id}"
    return panels


def build_catalog(role: str, core: list[Panel], plugins: list[Panel]) -> dict[str, Any]:
    # Grouped among what this viewer sees: one visible instance is no group.
    panels = [*[p for p in core if p.visible_to(role)], *disambiguate([p for p in plugins if p.visible_to(role)])]
    return {
        "categories": [{"id": key, "label": label} for key, label in CATEGORIES],
        "panels": [asdict(p) for p in panels],
    }
