"""
Tests for configuration loading and validation.
"""
import pytest
import tempfile
import os

from agent_system.config.loader import load_config
from agent_system.config.models import AgentConfig, LLMConfig, ContextConfig, PromptsConfig


class TestConfigLoader:
    """Test configuration loading functionality."""
    
    def test_load_valid_config(self):
        """Test loading valid configuration."""
        config_content = """
llm:
  provider: "openai"
  model: "gpt-3.5-turbo"

context:
  auto_datetime: true

max_steps: 5

servers: {}
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(config_content)
            f.flush()
            temp_name = f.name
            
        try:
            config = load_config(temp_name)
            
            assert isinstance(config, AgentConfig)
            assert config.llm.provider == "openai"
            assert config.llm.model == "gpt-3.5-turbo"
            assert config.max_steps == 5
        finally:
            try:
                os.unlink(temp_name)
            except PermissionError:
                pass  # Ignore Windows file locking issues


class TestAgentConfig:
    """Test AgentConfig model validation."""
    
    def test_valid_config_creation(self):
        """Test creating valid configuration."""
        config = AgentConfig(
            llm=LLMConfig(provider="openai", model="gpt-3.5-turbo"),
            max_steps=3,
            servers={}
        )
        
        assert config.llm.provider == "openai"
        assert config.max_steps == 3


class TestLLMConfig:
    """Test LLM configuration validation."""
    
    def test_valid_llm_config(self):
        """Test valid LLM configuration."""
        config = LLMConfig(provider="openai", model="gpt-4")
        
        assert config.provider == "openai"
        assert config.model == "gpt-4"


# Fixtures for configuration tests
@pytest.fixture
def sample_config():
    """Fixture providing a sample configuration."""
    return AgentConfig(
        llm=LLMConfig(provider="openai", model="gpt-3.5-turbo"),
        max_steps=5,
        servers={}
    )


class TestWithConfigFixtures:
    """Tests using configuration fixtures."""
    
    def test_sample_config(self, sample_config):
        """Test sample configuration fixture."""
        assert sample_config.llm.provider == "openai"
        assert sample_config.max_steps == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
