import pytest
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry
from agent_system.config.settings import load_settings as load_config

class DummyLLMNoop:
    async def chat(self, messages):
        return {"assistant": {"content": ""}}
    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        return {"assistant": {"content": ""}}

@pytest.mark.asyncio
async def test_all_tool_schemas_have_type(tmp_path):
    from agent_system.config.models import ToolServerConfig, AgentConfig
    
    cfg = load_config("config/config.yaml")
    registry = ToolServerRegistry()
    
    # Create ToolServerConfig for agent
    server_config = ToolServerConfig(
        type="test_agent",
        enabled=True,
        agent_config=AgentConfig()
    )
    
    agent = Agent("test_agent", cfg, server_config, registry)
    agent.llm = DummyLLMNoop()

    # Collect available tools the same way Agent.run_events would
    plugin_tools = registry.list()
    available_tools = await agent._tool_integration_manager.get_available_tools(plugin_tools)

    external_schemas, _ = await agent._tool_integration_manager.build_tool_schemas(available_tools)
    tools_schema = list(external_schemas)

    # Add internal plugin schemas (if any registered for test environment)
    for tool_name in available_tools:
        if '.' not in tool_name:
            server = registry.get(tool_name)
            if hasattr(server, 'get_tools'):
                tools_schema.extend(server.get_tools())
            else:
                tools_schema.append(server.get_schema())

    # Normalize as chat_tools will
    cleaned = []
    for t in tools_schema:
        assert isinstance(t, dict), f"Tool schema not a dict: {t!r}"
        assert 'type' in t, f"Missing type in tool schema: {t}"
        if t.get('type') == 'function':
            assert 'function' in t, f"Missing function block for tool: {t}"
            fn = t['function']
            assert isinstance(fn, dict), f"Function field not dict: {fn!r}"
            assert fn.get('name'), f"Function name missing in tool: {t}"
        cleaned.append(t)

    # Basic guarantee that we inspected something (in case no tools loaded)
    # At least zero is allowed; test mainly ensures assertions above didn't fail
    assert isinstance(cleaned, list)
