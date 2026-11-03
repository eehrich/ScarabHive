"""ToolServerIntegration leaves the process-wide tool-catalog listeners as it
found them once it shuts down, and the small tools.base helpers."""

from unittest.mock import AsyncMock, Mock

import pytest

from agent_system.config.models import AgentSystemConfig
from agent_system.plugins import capabilities
from agent_system.tools.base import (
    strip_server_prefix,
    tool_def_from_function,
    tool_defs_from_openai,
)
from agent_system.tools.integration import ToolServerIntegration


def _integration():
    integration = ToolServerIntegration(config=AgentSystemConfig())
    registry = Mock()
    registry.shutdown_all = AsyncMock()
    integration.plugin_registry = registry
    return integration


@pytest.mark.asyncio
async def test_shutdown_removes_the_cache_listener():
    before = list(capabilities._invalidation_callbacks)
    integrations = [_integration() for _ in range(3)]
    assert len(capabilities._invalidation_callbacks) == len(before) + 3
    for integration in integrations:
        await integration.shutdown()
    assert capabilities._invalidation_callbacks == before


def test_removing_an_unknown_listener_is_harmless():
    capabilities.remove_tool_catalog_listener(lambda: None)


def test_strip_server_prefix():
    assert strip_server_prefix("writer", "writer_path_writer_x") == "path_writer_x"
    assert strip_server_prefix("writer", "search") == "search"
    assert strip_server_prefix("writer", "writer") == "writer"


def test_tool_defs_from_openai():
    tools = tool_defs_from_openai([
        {"type": "function", "function": {"name": "a", "parameters": {"type": "object"}}},
        {"name": "not_openai"},
    ])
    assert [(t.name, t.description, t.input_schema) for t in tools] == [
        ("a", "Tool a", {"type": "object"})]
    assert tool_def_from_function({"name": "b", "description": "d"}).input_schema == {}
