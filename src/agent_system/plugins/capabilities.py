"""The seams a plugin needs to do more than answer tool calls.

Three things were missing from the plugin system, and every one of them was a
reason why the external MCP client had to live in the core instead of in a
plugin:

1. **Nothing runs after a plugin is created.** ``register_plugin`` called the
   factory and stopped there, so a plugin could not open connections.
2. **Nothing runs before it goes away.** ``unregister_plugin`` deleted a dict
   entry, so a plugin holding sockets or tasks had no place to close them.
3. **A plugin could not tell the core that its tools changed.** Neither the
   plugin base nor the hook context carried a way back, so a newly connected
   server stayed invisible until some cache expired -- up to an hour.

This module adds all three, deliberately small: two optional coroutines on the
plugin object (duck-typed, so no plugin has to change), and a registry for
plugins that supply tools they do not own themselves.

The registry is process-global because the plugin registry it mirrors is, and
because the agent core reaches it from request handlers that have no other path
to it. It is reset between tests via :func:`reset`.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Capability name for "I can offer tools that belong to somebody else",
#: which is what an MCP client does: it federates a foreign server's tools.
EXTERNAL_TOOLS = "external_tools"


@runtime_checkable
class PluginLifecycle(Protocol):
    """Optional coroutines a plugin may define. Both are awaited if present."""

    async def start_plugin(self) -> None: ...

    async def stop_plugin(self) -> None: ...


@runtime_checkable
class ExternalToolProvider(Protocol):
    """A plugin that federates tools from somewhere outside this process."""

    async def list_external_tools(self, *, force_refresh: bool = False) -> Dict[str, List[Dict[str, Any]]]:
        """Return ``{server_name: [{name, description, input_schema, blocked}]}``."""
        ...

    async def call_external_tool(self, server: str, tool: str, arguments: Dict[str, Any]) -> Any:
        """Call *tool* on *server* and return its result."""
        ...


_providers: Dict[str, Any] = {}
_invalidation_callbacks: List[Callable[[], Any]] = []


# --------------------------------------------------------------------- providers

def register_provider(capability: str, provider: Any) -> None:
    """Announce that *provider* supplies *capability*.

    Last one wins, with a warning: two plugins federating external tools would
    otherwise fight over the same namespace and the loser would vanish without
    a trace.
    """
    previous = _providers.get(capability)
    if previous is not None and previous is not provider:
        logger.warning(
            "Capability '%s' was provided by %s and is now taken over by %s",
            capability, type(previous).__name__, type(provider).__name__,
        )
    _providers[capability] = provider


def unregister_provider(capability: str, provider: Any = None) -> None:
    """Withdraw a provider. A stale withdrawal is ignored, not an error."""
    current = _providers.get(capability)
    if current is None:
        return
    if provider is not None and current is not provider:
        return
    del _providers[capability]


def get_provider(capability: str) -> Optional[Any]:
    """The provider for *capability*, or None if nobody offers it."""
    return _providers.get(capability)


# ------------------------------------------------------------------ invalidation

def on_tool_catalog_changed(callback: Callable[[], Any]) -> None:
    """Register a callback for "the set of available tools has changed".

    The core registers its cache-clearing here; a plugin calls
    :func:`notify_tool_catalog_changed` after connecting or dropping a server.
    """
    if callback not in _invalidation_callbacks:
        _invalidation_callbacks.append(callback)


def notify_tool_catalog_changed() -> None:
    """Tell every listener that the tool catalog changed.

    Callable from sync or async code, which matters because plugins signal this
    from connection handling that may be either. An async callback is scheduled
    on the running loop; with no loop running it is run to completion. A failing
    listener is logged, never raised -- this is a notification, not a
    transaction.
    """
    for callback in list(_invalidation_callbacks):
        try:
            result = callback()
            if inspect.isawaitable(result):
                try:
                    asyncio.get_running_loop().create_task(_await_quietly(result))
                except RuntimeError:
                    asyncio.run(_await_quietly(result))
        except Exception as e:
            logger.debug("Tool-catalog invalidation callback failed: %s", e)


async def _await_quietly(awaitable: Any) -> None:
    try:
        await awaitable
    except Exception as e:
        logger.debug("Async tool-catalog invalidation failed: %s", e)


# ----------------------------------------------------------------------- runtime

async def start_plugin(plugin: Any) -> None:
    """Await ``plugin.start_plugin()`` if it has one.

    A plugin that fails to start must not take the whole registration down --
    the other plugins, and the agent, keep working without it.
    """
    hook = getattr(plugin, "start_plugin", None)
    if hook is None:
        return
    try:
        await hook()
    except Exception as e:
        logger.error("Plugin '%s' failed to start: %s", getattr(plugin, "name", plugin), e, exc_info=True)


async def stop_plugin(plugin: Any) -> None:
    """Await ``plugin.stop_plugin()`` if it has one. Never raises."""
    hook = getattr(plugin, "stop_plugin", None)
    if hook is None:
        return
    try:
        await hook()
    except Exception as e:
        logger.warning("Plugin '%s' failed to stop cleanly: %s", getattr(plugin, "name", plugin), e)


def reset() -> None:
    """Drop all providers and callbacks (used by tests)."""
    _providers.clear()
    _invalidation_callbacks.clear()
