"""Tests for server configuration inheritance.

Tests the ability to define a base server (e.g., writer_agent) and have
other servers inherit from it via type reference.
"""
from __future__ import annotations

import pytest

from agent_system.config.models import AgentSystemConfig, PluginsConfig, MCPConfig, AgentConfig
from agent_system.config.settings import (
    get_mcp_config_by_name,
    _resolve_server_inheritance,
    _deep_merge_dict,
    _inheritance_cache,
    _plugins_cache,
)


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear caches before each test to avoid pollution between tests."""
    import agent_system.config.settings as settings
    _inheritance_cache.clear()
    settings._plugins_cache = None
    yield
    _inheritance_cache.clear()
    settings._plugins_cache = None


@pytest.fixture
def config_with_inheritance():
    """Create a config with server inheritance."""
    return AgentSystemConfig(
        plugins=PluginsConfig(
            plugin_dirs=["src/plugins"],
            default_config=MCPConfig(
                type="basic_agent",
                enabled=False,
                agent_config=AgentConfig(
                    llm_profile="normal",
                    max_steps=20,
                )
            ),
            servers={
                # Base server that others inherit from
                "writer_agent": MCPConfig(
                    type="basic_agent",
                    enabled=True,
                    description="Base writer agent",
                    agent_config=AgentConfig(
                        llm_profile=["normal", "think"],
                        max_steps=100,
                        hooks={"enabled": True, "overrides": {"some_hook": {"enabled": True}}},
                    )
                ),
                # Server that inherits from writer_agent
                "character_designer": MCPConfig(
                    type="writer_agent",  # Inherits from writer_agent
                    enabled=True,
                    description="Character designer agent",
                    agent_config=AgentConfig(
                        max_steps=50,  # Override max_steps
                    )
                ),
                # Another server inheriting from writer_agent
                "story_designer": MCPConfig(
                    type="writer_agent",
                    enabled=True,
                    agent_config=AgentConfig(
                        llm_profile="think",  # Override llm_profile
                    )
                ),
                # Direct plugin type (no inheritance)
                "simple_agent": MCPConfig(
                    type="basic_agent",
                    enabled=True,
                    agent_config=AgentConfig(
                        max_steps=10,
                    )
                ),
            }
        )
    )


@pytest.fixture
def config_with_chain_inheritance():
    """Create a config with multi-level inheritance chain."""
    return AgentSystemConfig(
        plugins=PluginsConfig(
            plugin_dirs=["src/plugins"],
            default_config=MCPConfig(type="basic_agent", enabled=False),
            servers={
                "base_agent": MCPConfig(
                    type="basic_agent",
                    enabled=True,
                    agent_config=AgentConfig(max_steps=100)
                ),
                "writer_agent": MCPConfig(
                    type="base_agent",  # Inherits from base_agent
                    enabled=True,
                    agent_config=AgentConfig(
                        llm_profile="normal",
                        hooks={"enabled": True},
                    )
                ),
                "character_designer": MCPConfig(
                    type="writer_agent",  # Inherits from writer_agent -> base_agent
                    enabled=True,
                    agent_config=AgentConfig(max_steps=50)
                ),
            }
        )
    )


@pytest.fixture
def config_with_circular_inheritance():
    """Create a config with circular inheritance (should fail)."""
    return AgentSystemConfig(
        plugins=PluginsConfig(
            plugin_dirs=["src/plugins"],
            default_config=MCPConfig(type="basic_agent", enabled=False),
            servers={
                "agent_a": MCPConfig(type="agent_b", enabled=True),
                "agent_b": MCPConfig(type="agent_c", enabled=True),
                "agent_c": MCPConfig(type="agent_a", enabled=True),  # Circular!
            }
        )
    )


class TestDeepMergeDict:
    """Tests for _deep_merge_dict helper."""

    def test_simple_merge(self):
        """Test simple dict merge."""
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge_dict(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        """Test nested dict merge."""
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        override = {"a": {"y": 20, "z": 30}}
        result = _deep_merge_dict(base, override)
        assert result == {"a": {"x": 1, "y": 20, "z": 30}, "b": 3}

    def test_none_values_preserved(self):
        """Test that None values don't override existing values."""
        base = {"a": 1, "b": 2}
        override = {"a": None, "c": 3}
        result = _deep_merge_dict(base, override)
        assert result == {"a": 1, "b": 2, "c": 3}


class TestResolveServerInheritance:
    """Tests for _resolve_server_inheritance."""

    def test_direct_plugin_type(self, config_with_inheritance):
        """Test server with direct plugin type (no inheritance)."""
        final_type, config_dict = _resolve_server_inheritance(
            "simple_agent", config_with_inheritance
        )
        assert final_type == "basic_agent"
        assert config_dict["type"] == "basic_agent"

    def test_single_level_inheritance(self, config_with_inheritance):
        """Test single-level inheritance (character_designer -> writer_agent)."""
        final_type, config_dict = _resolve_server_inheritance(
            "character_designer", config_with_inheritance
        )
        
        # Should resolve to basic_agent
        assert final_type == "basic_agent"
        assert config_dict["type"] == "basic_agent"
        
        # Should inherit hooks from writer_agent
        assert config_dict["agent_config"]["hooks"]["enabled"] is True
        
        # Should have overridden max_steps
        assert config_dict["agent_config"]["max_steps"] == 50

    def test_multi_level_inheritance(self, config_with_chain_inheritance):
        """Test multi-level inheritance chain."""
        final_type, config_dict = _resolve_server_inheritance(
            "character_designer", config_with_chain_inheritance
        )
        
        # Should resolve through chain to basic_agent
        assert final_type == "basic_agent"
        
        # Should have max_steps=50 (from character_designer)
        assert config_dict["agent_config"]["max_steps"] == 50
        
        # Should have hooks from writer_agent
        assert config_dict["agent_config"]["hooks"]["enabled"] is True

    def test_circular_inheritance_detection(self, config_with_circular_inheritance):
        """Test that circular inheritance is detected and raises error."""
        with pytest.raises(ValueError, match="Circular inheritance detected"):
            _resolve_server_inheritance("agent_a", config_with_circular_inheritance)

    def test_nonexistent_server(self, config_with_inheritance):
        """Test handling of nonexistent server."""
        final_type, config_dict = _resolve_server_inheritance(
            "nonexistent", config_with_inheritance
        )
        # Should return the name as-is
        assert final_type == "nonexistent"


class TestGetMcpConfigByName:
    """Tests for get_mcp_config_by_name with inheritance."""

    def test_inherited_config(self, config_with_inheritance):
        """Test that get_mcp_config_by_name resolves inheritance."""
        result = get_mcp_config_by_name("character_designer", config_with_inheritance)
        
        assert result is not None
        # Type should be resolved to basic_agent
        assert result.type == "basic_agent"
        # Description should be from character_designer
        assert result.description == "Character designer agent"
        # max_steps should be 50 (from character_designer)
        assert result.agent_config.max_steps == 50
        # hooks should be inherited from writer_agent
        assert result.agent_config.hooks is not None
        assert result.agent_config.hooks.enabled is True

    def test_direct_config(self, config_with_inheritance):
        """Test direct plugin type without inheritance."""
        result = get_mcp_config_by_name("simple_agent", config_with_inheritance)
        
        assert result is not None
        assert result.type == "basic_agent"
        assert result.agent_config.max_steps == 10

    def test_base_server_itself(self, config_with_inheritance):
        """Test getting the base server config directly."""
        result = get_mcp_config_by_name("writer_agent", config_with_inheritance)
        
        assert result is not None
        assert result.type == "basic_agent"
        assert result.description == "Base writer agent"
        assert result.agent_config.max_steps == 100

    def test_nonexistent_server(self, config_with_inheritance):
        """Test handling of nonexistent server."""
        result = get_mcp_config_by_name("nonexistent", config_with_inheritance)
        assert result is None


class TestInheritanceIntegration:
    """Integration tests for inheritance in realistic scenarios."""

    def test_writer_agent_inheritance_scenario(self):
        """Test realistic writer agent inheritance scenario."""
        config = AgentSystemConfig(
            plugins=PluginsConfig(
                plugin_dirs=["src/plugins"],
                default_config=MCPConfig(
                    type="basic_agent",
                    enabled=False,
                    agent_config=AgentConfig(
                        llm_profile="normal",
                        max_steps=20,
                        system_template="config/prompts/system_prompt.yaml",
                    )
                ),
                servers={
                    "writer_agent": MCPConfig(
                        type="basic_agent",
                        enabled=True,
                        agent_config=AgentConfig(
                            llm_profile=["normal", "think"],
                            max_steps=100,
                            hooks={
                                "enabled": True,
                                "overrides": {
                                    "context_summarizer.summarize_context": {"enabled": False},
                                    "writer_context_summarizer.summarize_context": {"enabled": True},
                                }
                            }
                        )
                    ),
                    "character_designer": MCPConfig(
                        type="writer_agent",
                        enabled=True,
                        description="Character specialist",
                        agent_config=AgentConfig(
                            max_steps=50,
                            system_template="config/agents_writer/prompts/character_designer.md",
                        )
                    ),
                    "scene_writer": MCPConfig(
                        type="writer_agent",
                        enabled=True,
                        description="Scene writer",
                        agent_config=AgentConfig(
                            llm_profile="think",
                            max_steps=200,
                        )
                    ),
                }
            )
        )
        
        # Test character_designer
        char_config = get_mcp_config_by_name("character_designer", config)
        assert char_config.type == "basic_agent"
        assert char_config.description == "Character specialist"
        assert char_config.agent_config.max_steps == 50
        assert char_config.agent_config.system_template == "config/agents_writer/prompts/character_designer.md"
        # Should inherit hooks from writer_agent
        assert char_config.agent_config.hooks.enabled is True
        assert "writer_context_summarizer.summarize_context" in char_config.agent_config.hooks.overrides
        
        # Test scene_writer
        scene_config = get_mcp_config_by_name("scene_writer", config)
        assert scene_config.type == "basic_agent"
        assert scene_config.agent_config.llm_profile == "think"
        assert scene_config.agent_config.max_steps == 200
        # Should also inherit hooks
        assert scene_config.agent_config.hooks.enabled is True
