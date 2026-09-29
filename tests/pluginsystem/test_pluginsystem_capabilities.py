"""The plugin-system seams: lifecycle hooks and the capability registry.

Three things the plugin system could not do, each of which forced the external
MCP client to live in the core:

* run anything after a plugin is created,
* run anything before it goes away,
* let a plugin tell the core that the set of available tools changed.

The tests below pin the behaviour that matters at those seams -- in particular
that a plugin which fails is contained, because "one bad plugin takes the
process down" would be a much worse bug than the one being fixed.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_system.plugins import capabilities


@pytest.fixture(autouse=True)
def clean_registry():
    capabilities.reset()
    yield
    capabilities.reset()


class Plain:
    """A plugin with no lifecycle at all -- most plugins look like this."""


class Recorder:
    def __init__(self):
        self.events = []

    async def start_plugin(self):
        self.events.append("start")

    async def stop_plugin(self):
        self.events.append("stop")


class Exploding:
    name = "exploding"

    async def start_plugin(self):
        raise RuntimeError("boom on start")

    async def stop_plugin(self):
        raise RuntimeError("boom on stop")


class TestLifecycleHooks:
    @pytest.mark.asyncio
    async def test_hooks_run_when_present(self):
        plugin = Recorder()
        await capabilities.start_plugin(plugin)
        await capabilities.stop_plugin(plugin)
        assert plugin.events == ["start", "stop"]

    @pytest.mark.asyncio
    async def test_plugins_without_hooks_are_untouched(self):
        """Existing plugins must not have to change. Absence is not an error."""
        plugin = Plain()
        await capabilities.start_plugin(plugin)
        await capabilities.stop_plugin(plugin)

    @pytest.mark.asyncio
    async def test_a_failing_plugin_is_contained(self):
        """One plugin failing must not abort registration or shutdown.

        Without this the MCP client plugin -- which talks to the network at
        start -- could take the whole process down whenever a remote server is
        unreachable.
        """
        plugin = Exploding()
        await capabilities.start_plugin(plugin)  # must not raise
        await capabilities.stop_plugin(plugin)   # must not raise


class TestProviderRegistry:
    def test_register_and_look_up(self):
        provider = object()
        capabilities.register_provider(capabilities.EXTERNAL_TOOLS, provider)
        assert capabilities.get_provider(capabilities.EXTERNAL_TOOLS) is provider

    def test_missing_provider_is_none_not_an_error(self):
        assert capabilities.get_provider(capabilities.EXTERNAL_TOOLS) is None
        assert capabilities.get_provider("no-such-capability") is None

    def test_unregister_only_removes_your_own(self):
        """A late withdrawal must not knock out whoever took over."""
        first, second = object(), object()
        capabilities.register_provider(capabilities.EXTERNAL_TOOLS, first)
        capabilities.register_provider(capabilities.EXTERNAL_TOOLS, second)
        capabilities.unregister_provider(capabilities.EXTERNAL_TOOLS, first)
        assert capabilities.get_provider(capabilities.EXTERNAL_TOOLS) is second
        capabilities.unregister_provider(capabilities.EXTERNAL_TOOLS, second)
        assert capabilities.get_provider(capabilities.EXTERNAL_TOOLS) is None

    def test_unregistering_an_unknown_capability_is_harmless(self):
        capabilities.unregister_provider("never-registered")


class TestCatalogInvalidation:
    def test_sync_callback_runs(self):
        calls = []
        capabilities.on_tool_catalog_changed(lambda: calls.append(1))
        capabilities.notify_tool_catalog_changed()
        assert calls == [1]

    def test_the_same_callback_is_not_registered_twice(self):
        calls = []
        callback = lambda: calls.append(1)  # noqa: E731
        capabilities.on_tool_catalog_changed(callback)
        capabilities.on_tool_catalog_changed(callback)
        capabilities.notify_tool_catalog_changed()
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_async_callback_runs_on_the_loop(self):
        """The core's cache invalidation is a coroutine; it must still fire."""
        calls = []

        async def callback():
            calls.append(1)

        capabilities.on_tool_catalog_changed(callback)
        capabilities.notify_tool_catalog_changed()
        await asyncio.sleep(0)  # let the scheduled task run
        assert calls == [1]

    def test_a_failing_listener_does_not_break_the_notification(self):
        """This is a notification, not a transaction."""
        calls = []

        def bad():
            raise RuntimeError("nope")

        capabilities.on_tool_catalog_changed(bad)
        capabilities.on_tool_catalog_changed(lambda: calls.append(1))
        capabilities.notify_tool_catalog_changed()
        assert calls == [1]


class TestRegistryIntegration:
    """The registry must start a plugin exactly once, from any path."""

    @pytest.mark.asyncio
    async def test_start_is_idempotent_across_paths(self):
        from agent_system.plugins.tool_adapter import PluginToolAdapter, PluginToolRegistry

        registry = PluginToolRegistry()
        plugin = Recorder()
        registry.plugin_servers["rec"] = PluginToolAdapter("rec", plugin)

        await registry.start_plugin("rec")
        await registry.start_plugin("rec")   # direct repeat
        await registry.start_all()           # and the sweep
        assert plugin.events == ["start"]

    @pytest.mark.asyncio
    async def test_shutdown_all_stops_started_plugins_only_once(self):
        from agent_system.plugins.tool_adapter import PluginToolAdapter, PluginToolRegistry

        registry = PluginToolRegistry()
        started, never = Recorder(), Recorder()
        registry.plugin_servers["started"] = PluginToolAdapter("started", started)
        registry.plugin_servers["never"] = PluginToolAdapter("never", never)

        await registry.start_plugin("started")
        await registry.shutdown_all()
        await registry.shutdown_all()

        assert started.events == ["start", "stop"]
        # Never started, so never stopped -- stopping it would run teardown on
        # something that was never set up.
        assert never.events == []

    @pytest.mark.asyncio
    async def test_the_core_actually_runs_the_hooks(self):
        """ToolServerIntegration must wire start_all/shutdown_all up, not just own them.

        The seam is only worth anything if the core uses it. Without this test
        both wirings could be deleted and the whole suite stayed green -- the
        plugin would simply never start, and an external server would look
        like "none configured" rather than like a bug.
        """
        from agent_system.config.models import AgentSystemConfig
        from agent_system.tools.integration import ToolServerIntegration
        from agent_system.plugins.tool_adapter import PluginToolAdapter, PluginToolRegistry

        registry = PluginToolRegistry()
        plugin = Recorder()
        registry.plugin_servers["rec"] = PluginToolAdapter("rec", plugin)

        config = AgentSystemConfig()
        integration = ToolServerIntegration(config=config)
        integration.plugin_registry = registry
        integration.servers_bootstrapped = True  # skip the real bootstrap

        await integration.initialize(config)
        assert plugin.events == ["start"], "initialize() did not start the plugins"

        await integration.shutdown()
        assert plugin.events == ["start", "stop"], "shutdown() did not stop the plugins"

    @pytest.mark.asyncio
    async def test_a_stopped_plugin_can_be_started_again(self):
        """shutdown_all releases resources; it does not deregister."""
        from agent_system.plugins.tool_adapter import PluginToolAdapter, PluginToolRegistry

        registry = PluginToolRegistry()
        plugin = Recorder()
        registry.plugin_servers["rec"] = PluginToolAdapter("rec", plugin)

        await registry.start_plugin("rec")
        await registry.shutdown_all()
        await registry.start_plugin("rec")
        assert plugin.events == ["start", "stop", "start"]
