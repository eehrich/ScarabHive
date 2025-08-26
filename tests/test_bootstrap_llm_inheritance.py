"""
Tests for LLM config inheritance when bootstrapping servers.
"""
from agent_system.servers.bootstrap import bootstrap_servers
from agent_system.mcp.base import MCPRegistry
from agent_system.config.models import AgentConfig, MCPConfig


def test_agent_inherits_global_llm():
    cfg = AgentConfig()
    cfg.llm.provider = "openai"
    cfg.mcp = MCPConfig(enabled_servers=["agent"])
    cfg.servers = {"agent": {"type": "agent"}}

    registry = MCPRegistry()
    bootstrap_servers(cfg, registry)

    agent = registry.get("agent")
    assert hasattr(agent, "agent_config")
    assert agent.agent_config.llm.provider == "openai"


def test_agent_server_override():
    cfg = AgentConfig()
    cfg.llm.provider = "openai"
    cfg.mcp = MCPConfig(enabled_servers=["agent"])
    cfg.servers = {"agent": {"type": "agent", "default_provider": "ollama"}}

    registry = MCPRegistry()
    bootstrap_servers(cfg, registry)

    agent = registry.get("agent")
    assert agent.agent_config.llm.provider == "ollama"


def test_web_research_agent_server_override():
    cfg = AgentConfig()
    cfg.mcp = MCPConfig(enabled_servers=["web_research_agent"])
    cfg.servers = {
        "web_research_agent": {"type": "web_research_agent", "default_provider": "openai", "model": "gpt-5-mini"}
    }

    registry = MCPRegistry()
    bootstrap_servers(cfg, registry)

    agent = registry.get("web_research_agent")
    assert agent.agent_config.llm.provider == "openai"
    assert agent.agent_config.llm.model == "gpt-5-mini"
