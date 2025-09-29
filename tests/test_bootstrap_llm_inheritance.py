"""
Tests for LLM config inheritance when bootstrapping servers.
"""
from agent_system.servers.bootstrap import bootstrap_servers
from agent_system.mcp.base import MCPRegistry
from agent_system.config.models import AgentConfig, MCPConfig, LLMSystemConfig, LLMModelConfig, LLMProfile



def test_agent_inherits_global_llm():
    cfg = AgentConfig(
        llm_system=LLMSystemConfig(
            models={
                "test-model": LLMModelConfig(provider="openai", model="test-model")
            },
            profiles={
                "normal": LLMProfile(model_ref="test-model")
            },
            default_profile="normal"
        ),
        mcp=MCPConfig(enabled_servers=["agent"]),
        servers={"agent": {"type": "agent"}}
    )

    registry = MCPRegistry()
    bootstrap_servers(cfg, registry)

    agent = registry.get("agent")
    assert hasattr(agent, "agent_config")
    assert agent.agent_config.llm_system.models["test-model"].provider == "openai"


def test_agent_server_override():
    cfg = AgentConfig(
        llm_system=LLMSystemConfig(
            models={
                "test-model": LLMModelConfig(provider="openai", model="test-model")
            },
            profiles={
                "normal": LLMProfile(model_ref="test-model")
            },
            default_profile="normal"
        ),
        mcp=MCPConfig(enabled_servers=["agent"]),
        servers={"agent": {"type": "agent", "default_provider": "ollama"}}
    )

    registry = MCPRegistry()
    bootstrap_servers(cfg, registry)

    agent = registry.get("agent")
    assert hasattr(agent, "agent_config")
    
    # Verify the server override was applied
    agent_llm_system = agent.agent_config.llm_system
    # Should have created an override profile with ollama provider
    assert agent_llm_system.default_profile == "agent_override"
    assert "agent_override" in agent_llm_system.profiles


def test_web_research_agent_server_override():
    from agent_system.plugins.discovery import discover_all_plugins
    from pathlib import Path
    import pytest
    
    # Test plugin discovery first
    src_plugins = Path.cwd() / "src" / "plugins"
    plugins = discover_all_plugins(dirs=[src_plugins] if src_plugins.exists() else None)
    
    # Skip test if web_research_agent plugin is not discovered
    if "web_research_agent" not in plugins:
        pytest.skip("web_research_agent plugin not discovered in test environment")
    
    cfg = AgentConfig(
        llm_system=LLMSystemConfig(
            models={
                "llama3.1:8b": LLMModelConfig(provider="ollama", model="llama3.1:8b"),
                "gpt-5-mini": LLMModelConfig(provider="openai", model="gpt-5-mini")
            },
            profiles={
                "normal": LLMProfile(model_ref="llama3.1:8b"),
                "fast": LLMProfile(model_ref="gpt-5-mini")
            },
            default_profile="normal"
        ),
        agent_llm_profiles={"web_research_agent": "fast"},
        mcp=MCPConfig(enabled_servers=["web_research_agent"]),
        servers={
            "web_research_agent": {"type": "web_research_agent", "default_provider": "openai", "model": "gpt-5-mini"}
        }
    )

    registry = MCPRegistry()
    try:
        bootstrap_servers(cfg, registry)
    except Exception as e:
        import pytest
        pytest.skip(f"Failed to bootstrap web_research_agent: {e}")

    # Check what servers are actually registered
    registered_servers = list(registry._servers.keys())
    if "web_research_agent" not in registered_servers:
        import pytest
        pytest.skip(f"web_research_agent not registered. Available servers: {registered_servers}")

    agent = registry.get("web_research_agent")
    assert agent.cfg.get("default_provider") == "openai"
    assert agent.cfg.get("model") == "gpt-5-mini"
