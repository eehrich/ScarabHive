"""
Tests for LLM config inheritance when bootstrapping servers.
"""
from agent_system.servers.bootstrap import bootstrap_servers
from agent_system.mcp.base import MCPRegistry
from agent_system.config.models import AgentSystemConfig, MCPSystemConfig, MCPConfig, AgentConfig, LLMSystemConfig, LLMModelConfig, LLMProfile



def test_agent_inherits_global_llm():
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={
                "test-model": LLMModelConfig(provider="openai", model="test-model")
            },
            profiles={
                "normal": LLMProfile(model_ref="test-model")
            }
        ),
        plugins=PluginsConfig(
            servers={
                "agent": MCPConfig(type="agent", enabled=True, agent_config=AgentConfig())
            }
        )
    )

    registry = MCPRegistry()
    bootstrap_servers(cfg, registry)

    agent = registry.get("agent")
    assert hasattr(agent, "agent_config")
    # Agent inherits global llm_system through system_config (not agent_config)
    assert agent.system_config.llm_system.models["test-model"].provider == "openai"


def test_agent_server_override():
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={
                "test-model": LLMModelConfig(provider="openai", model="test-model")
            },
            profiles={
                "normal": LLMProfile(model_ref="test-model")
            }
        ),
        plugins=PluginsConfig(
            servers={
                "agent": MCPConfig(type="agent", enabled=True, agent_config=AgentConfig())
            }
        )
    )
    # Set custom attribute on the MCPConfig for override test
    cfg.plugins.servers["agent"].default_provider = "ollama"

    registry = MCPRegistry()
    bootstrap_servers(cfg, registry)

    agent = registry.get("agent")
    assert hasattr(agent, "agent_config")
    
    # Verify the server override was passed through mcp_config
    assert agent.mcp_config.default_provider == "ollama"
    # Agent still inherits global llm_system through system_config
    assert agent.system_config.llm_system.models["test-model"].provider == "openai"


def test_web_research_agent_server_override():
    from agent_system.plugins.discovery import discover_all_plugins
    from pathlib import Path
    import pytest
    
    # Test plugin discovery first
    src_plugins = Path.cwd() / "src" / "plugins"
    plugins = discover_all_plugins(dirs=[src_plugins] if src_plugins.exists() else None)
    assert "web_research_agent" in plugins, "web_research_agent plugin must be present in repository for this test"
    
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={
                "llama3.1:8b": LLMModelConfig(provider="ollama", model="llama3.1:8b"),
                "gpt-5-mini": LLMModelConfig(provider="openai", model="gpt-5-mini")
            },
            profiles={
                "normal": LLMProfile(model_ref="llama3.1:8b"),
                "fast": LLMProfile(model_ref="gpt-5-mini")
            }
        ),
        plugins=PluginsConfig(
            servers={
                "web_research_agent": MCPConfig(type="web_research_agent", enabled=True, agent_config=AgentConfig())
            }
        )
    )
    # Set custom attributes
    cfg.plugins.servers["web_research_agent"].default_provider = "openai"
    cfg.plugins.servers["web_research_agent"].model = "gpt-5-mini"

    registry = MCPRegistry()
    try:
        bootstrap_servers(cfg, registry)
    except Exception as e:
        pytest.fail(f"Failed to bootstrap web_research_agent: {e}")

    # Check what servers are actually registered
    registered_servers = list(registry._servers.keys())
    assert "web_research_agent" in registered_servers, f"web_research_agent not registered. Available servers: {registered_servers}"

    agent = registry.get("web_research_agent")
    # Check custom attributes were passed through mcp_config
    assert agent.mcp_config.default_provider == "openai"
    assert agent.mcp_config.model == "gpt-5-mini"
    # Agent should also have access to global llm_system
    assert "gpt-5-mini" in agent.system_config.llm_system.models

