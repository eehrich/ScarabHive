import pytest
from unittest.mock import MagicMock

from agent_system.llm.factory import LLMFactory
from agent_system.servers.agent.server import Agent
from agent_system.mcp.base import MCPRegistry
from agent_system.config.models import AgentConfig, LLMConfig, ContextConfig, PromptsConfig


def make_config():
    return AgentConfig(
        llm=LLMConfig(provider="openai", model="gpt-test", openai_api_key=None),
        context=ContextConfig(auto_datetime=False),
        prompts=PromptsConfig(system_template="config/prompts/system_prompt.yaml"),
        max_steps=1,
        servers={}
    )


def test_llmfactory_returns_none_if_no_config():
    f = LLMFactory(None)
    assert f.create() is None


def test_agent_accepts_injected_llm():
    cfg = make_config()
    registry = MCPRegistry()
    fake_llm = MagicMock()
    agent = Agent("test_agent", cfg, registry, llm=fake_llm)
    assert agent.llm is fake_llm


def test_agent_uses_llm_factory_when_provided():
    cfg = make_config()
    registry = MCPRegistry()
    class FakeFactory:
        def __init__(self):
            self.created = False
        def create(self):
            self.created = True
            return MagicMock()

    f = FakeFactory()
    agent = Agent("test_agent", cfg, registry, llm_factory=f)
    assert f.created is True
    assert agent.llm is not None
