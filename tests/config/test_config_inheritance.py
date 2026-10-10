"""Tests for server configuration inheritance.

Tests the ability to define a base server (e.g., writer_agent) and have
other servers inherit from it via type reference.
"""
from __future__ import annotations

import pytest

from agent_system.config.models import AgentSystemConfig, PluginsConfig, ToolServerConfig, AgentConfig
from agent_system.config.settings import (
    get_tool_server_config,
    _resolve_server_inheritance,
    _deep_merge_dict,
)


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear caches before each test to avoid pollution between tests."""
    import agent_system.config.inheritance as inheritance
    inheritance._plugins_cache = None
    yield
    inheritance._plugins_cache = None


@pytest.fixture
def config_with_inheritance():
    """Create a config with server inheritance."""
    return AgentSystemConfig(
        plugins=PluginsConfig(
            plugin_dirs=["src/plugins"],
            default_config=ToolServerConfig(
                type="basic_agent",
                enabled=False,
                agent_config=AgentConfig(
                    llm_profile="normal",
                    max_steps=20,
                )
            ),
            servers={
                # Base server that others inherit from
                "writer_agent": ToolServerConfig(
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
                "character_designer": ToolServerConfig(
                    type="writer_agent",  # Inherits from writer_agent
                    enabled=True,
                    description="Character designer agent",
                    agent_config=AgentConfig(
                        max_steps=50,  # Override max_steps
                    )
                ),
                # Another server inheriting from writer_agent
                "story_designer": ToolServerConfig(
                    type="writer_agent",
                    enabled=True,
                    agent_config=AgentConfig(
                        llm_profile="think",  # Override llm_profile
                    )
                ),
                # Direct plugin type (no inheritance)
                "simple_agent": ToolServerConfig(
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
            default_config=ToolServerConfig(type="basic_agent", enabled=False),
            servers={
                "base_agent": ToolServerConfig(
                    type="basic_agent",
                    enabled=True,
                    agent_config=AgentConfig(max_steps=100)
                ),
                "writer_agent": ToolServerConfig(
                    type="base_agent",  # Inherits from base_agent
                    enabled=True,
                    agent_config=AgentConfig(
                        llm_profile="normal",
                        hooks={"enabled": True},
                    )
                ),
                "character_designer": ToolServerConfig(
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
            default_config=ToolServerConfig(type="basic_agent", enabled=False),
            servers={
                "agent_a": ToolServerConfig(type="agent_b", enabled=True),
                "agent_b": ToolServerConfig(type="agent_c", enabled=True),
                "agent_c": ToolServerConfig(type="agent_a", enabled=True),  # Circular!
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
    """Tests for get_tool_server_config with inheritance."""

    def test_inherited_config(self, config_with_inheritance):
        """Test that get_tool_server_config resolves inheritance."""
        result = get_tool_server_config("character_designer", config_with_inheritance)
        
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
        result = get_tool_server_config("simple_agent", config_with_inheritance)
        
        assert result is not None
        assert result.type == "basic_agent"
        assert result.agent_config.max_steps == 10

    def test_base_server_itself(self, config_with_inheritance):
        """Test getting the base server config directly."""
        result = get_tool_server_config("writer_agent", config_with_inheritance)
        
        assert result is not None
        assert result.type == "basic_agent"
        assert result.description == "Base writer agent"
        assert result.agent_config.max_steps == 100

    def test_nonexistent_server(self, config_with_inheritance):
        """Test handling of nonexistent server."""
        result = get_tool_server_config("nonexistent", config_with_inheritance)
        assert result is None

    def test_each_config_object_resolves_its_own_entries(self, config_with_inheritance):
        """A second config next to the first (a fresh load from disk beside the live one) gets its own values."""
        edited = config_with_inheritance.model_copy(deep=True)
        edited.plugins.servers["writer_agent"].agent_config.max_steps = 7
        edited.plugins.servers["character_designer"].agent_config.max_steps = 8

        assert get_tool_server_config("character_designer", config_with_inheritance).agent_config.max_steps == 50
        assert get_tool_server_config("story_designer", config_with_inheritance).agent_config.max_steps == 100
        assert get_tool_server_config("character_designer", edited).agent_config.max_steps == 8
        assert get_tool_server_config("story_designer", edited).agent_config.max_steps == 7
        assert get_tool_server_config("story_designer", config_with_inheritance).agent_config.max_steps == 100


class TestInheritanceIntegration:
    """Integration tests for inheritance in realistic scenarios."""

    def test_writer_agent_inheritance_scenario(self):
        """Test realistic writer agent inheritance scenario."""
        config = AgentSystemConfig(
            plugins=PluginsConfig(
                plugin_dirs=["src/plugins"],
                default_config=ToolServerConfig(
                    type="basic_agent",
                    enabled=False,
                    agent_config=AgentConfig(
                        llm_profile="normal",
                        max_steps=20,
                        system_template="config/prompts/system_prompt.md",
                    )
                ),
                servers={
                    "writer_agent": ToolServerConfig(
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
                    "character_designer": ToolServerConfig(
                        type="writer_agent",
                        enabled=True,
                        description="Character specialist",
                        agent_config=AgentConfig(
                            max_steps=50,
                            system_template="config/agents_writer/prompts/character_designer.md",
                        )
                    ),
                    "scene_writer": ToolServerConfig(
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
        char_config = get_tool_server_config("character_designer", config)
        assert char_config.type == "basic_agent"
        assert char_config.description == "Character specialist"
        assert char_config.agent_config.max_steps == 50
        assert char_config.agent_config.system_template == "config/agents_writer/prompts/character_designer.md"
        # Should inherit hooks from writer_agent
        assert char_config.agent_config.hooks.enabled is True
        assert "writer_context_summarizer.summarize_context" in char_config.agent_config.hooks.overrides
        
        # Test scene_writer
        scene_config = get_tool_server_config("scene_writer", config)
        assert scene_config.type == "basic_agent"
        assert scene_config.agent_config.llm_profile == "think"
        assert scene_config.agent_config.max_steps == 200
        # Should also inherit hooks
        assert scene_config.agent_config.hooks.enabled is True


class TestListMergeSyntax:
    """Tests for explicit +/! list merge syntax during inheritance."""

    def test_list_without_syntax_replaces(self):
        """Test that lists without +/! prefix completely replace parent list."""
        base = {
            "agent_config": {
                "tools": {
                    "allowed": ["tool_a/*", "tool_b/*"],
                }
            }
        }
        override = {
            "agent_config": {
                "tools": {
                    "allowed": ["tool_c/*"],  # No +/! = REPLACE
                }
            }
        }
        result = _deep_merge_dict(base, override)
        
        # Should completely replace
        assert result["agent_config"]["tools"]["allowed"] == ["tool_c/*"]

    def test_plus_prefix_appends(self):
        """Test that + prefix appends to parent list."""
        base = {
            "agent_config": {
                "tools": {
                    "allowed": ["tool_a/*", "tool_b/*"],
                }
            }
        }
        override = {
            "agent_config": {
                "tools": {
                    "allowed": ["+tool_c/*"],  # + = append
                }
            }
        }
        result = _deep_merge_dict(base, override)
        
        # Should append (+ stripped from result)
        assert result["agent_config"]["tools"]["allowed"] == [
            "tool_a/*", "tool_b/*", "tool_c/*"
        ]

    def test_exclamation_prefix_removes(self):
        """Test that ! prefix removes matching items from parent list."""
        base = {
            "agent_config": {
                "tools": {
                    "allowed": ["tool_a/*", "tool_b/*", "tool_c/*"],
                }
            }
        }
        override = {
            "agent_config": {
                "tools": {
                    "allowed": ["!tool_b/*"],  # ! = remove
                }
            }
        }
        result = _deep_merge_dict(base, override)
        
        # Should remove tool_b/*
        assert result["agent_config"]["tools"]["allowed"] == [
            "tool_a/*", "tool_c/*"
        ]

    def test_combined_add_and_remove(self):
        """Test combining + and ! in same list."""
        base = {
            "agent_config": {
                "tools": {
                    "allowed": ["w_sam/*", "datetime/*", "todo/*"],
                }
            }
        }
        override = {
            "agent_config": {
                "tools": {
                    "allowed": [
                        "!w_sam/*",        # Remove w_sam
                        "+w_sam_gemini/*", # Add gemini version
                    ],
                }
            }
        }
        result = _deep_merge_dict(base, override)
        
        assert "w_sam/*" not in result["agent_config"]["tools"]["allowed"]
        assert "w_sam_gemini/*" in result["agent_config"]["tools"]["allowed"]
        assert "datetime/*" in result["agent_config"]["tools"]["allowed"]
        assert "todo/*" in result["agent_config"]["tools"]["allowed"]

    def test_mixing_prefixed_and_bare_entries_raises(self):
        """A list either merges or replaces — never both.

        Read as "replace", the prefixed entries would be the only survivors;
        read as "merge", the bare one silently joins the inherited list. Both
        readings are defensible, so guessing would make a forgotten '+' change
        an agent's tools without a word.
        """
        base = {"items": ["a", "b"]}
        override = {"items": ["+c", "d"]}  # d has no prefix but + exists

        with pytest.raises(ValueError) as exc:
            _deep_merge_dict(base, override)
        assert "'d'" in str(exc.value) or "['d']" in str(exc.value)

    def test_error_names_the_offending_entries_and_where(self):
        """The message has to be actionable: which key, which entries."""
        base = {"agent_config": {"tools": {"allowed": ["a/*"]}}}
        override = {"agent_config": {"tools": {"allowed": ["forgot/*", "+ok/*"]}}}

        with pytest.raises(ValueError) as exc:
            _deep_merge_dict(base, override, "my_agent")
        message = str(exc.value)
        assert "my_agent.agent_config.tools.allowed" in message
        assert "forgot/*" in message
        assert "+ok/*" in message

    def test_prefix_without_an_inherited_list_is_stripped(self):
        """Nothing to merge into is not a reason to keep the '+': a literal
        '+okf/*' in the resolved config matches no tool at all."""
        result = _deep_merge_dict({}, {"items": ["+c", "!gone/*"]})
        assert result["items"] == ["c"]

    def test_parent_prefixes_do_not_leak_into_the_child(self):
        """A server's own '+x' is only stripped when it is merged against
        default_config, which happens AFTER inheritance. Without normalising the
        inherited list here, that '+' rides along and later reads as a mixed
        list nobody wrote."""
        base = {"items": ["+inherited/*"]}          # parent, not yet normalised
        override = {"items": ["+own/*"]}
        assert _deep_merge_dict(base, override)["items"] == ["inherited/*", "own/*"]

    def test_wildcard_removal_pattern(self):
        """Test that ! with wildcard removes multiple matching items."""
        base = {
            "tools": ["plugin_a/tool1", "plugin_a/tool2", "plugin_b/tool1"]
        }
        override = {
            "tools": ["!plugin_a/*"]  # Remove all plugin_a tools
        }
        result = _deep_merge_dict(base, override)

        assert result["tools"] == ["plugin_b/tool1"]

    def test_an_inherited_string_is_a_list_of_one(self):
        """`llm_profile: a` inherited, `["+c"]` added: the chain is [a, c]. The
        string counted as an empty list, and c became the primary alone."""
        assert _deep_merge_dict({"llm_profile": "a"}, {"llm_profile": ["+c"]})["llm_profile"] == ["a", "c"]

    def test_a_nested_list_loses_its_prefix_under_an_absent_or_none_parent(self):
        """A dict the parent lacks, or leaves None (default_config without an
        agent_config), was taken over raw: its "+c" survived as a literal name."""
        child = {"agent_config": {"llm_profile": ["+c"], "tools": {"allowed": ["+x/*"]}, "system_prompt": None}}
        for parent in ({}, {"agent_config": None}):
            merged = _deep_merge_dict(parent, child)["agent_config"]
            assert merged["llm_profile"] == ["c"] and merged["tools"]["allowed"] == ["x/*"], (parent, merged)
            # taken over whole otherwise: a None it carries stays (terminal's `initial_cwd: null`)
            assert "system_prompt" in merged and merged["system_prompt"] is None, merged

    def test_a_wildcard_anywhere_removes(self):
        """`!*_sam/*` removed nothing, silently: only a trailing or a leading `*`
        was understood. A `!` pattern is an fnmatch pattern, as the tool patterns
        are, and `x/*` still takes the bare `x` with it."""
        base = {"tools": ["coder_sam/*", "project_sam/*", "web_scraper/*", "file_ops"]}

        assert _deep_merge_dict(base, {"tools": ["!*_sam/*"]})["tools"] == ["web_scraper/*", "file_ops"]
        assert _deep_merge_dict(base, {"tools": ["!*scraper*"]})["tools"] == [
            "coder_sam/*", "project_sam/*", "file_ops"]
        assert _deep_merge_dict(base, {"tools": ["!file_ops/*"]})["tools"] == [
            "coder_sam/*", "project_sam/*", "web_scraper/*"]

    def test_deduplication_on_add(self):
        """Test that duplicate items are not added twice."""
        base = {"items": ["a", "b", "c"]}
        override = {"items": ["+b", "+d"]}  # b already exists
        
        result = _deep_merge_dict(base, override)
        
        # b should not be duplicated
        assert result["items"] == ["a", "b", "c", "d"]

    def test_llm_profile_advanced_still_replaced(self):
        """Test that lists without +/! syntax are still replaced."""
        base = {
            "agent_config": {
                "llm_profile_advanced": ["profile_a", "profile_b"]
            }
        }
        override = {
            "agent_config": {
                "llm_profile_advanced": ["profile_c"]  # No +/! = replace
            }
        }
        result = _deep_merge_dict(base, override)

        assert result["agent_config"]["llm_profile_advanced"] == ["profile_c"]

    def test_realistic_gemini_batch_scenario(self):
        """Test realistic scenario like book_architect_gemini_batch."""
        parent_config = {
            "type": "basic_agent",
            "enabled": True,
            "agent_config": {
                "llm_profile": "chat",
                "max_steps": 500,
                "tools": {
                    "allowed": [
                        "writer_content/*",
                        "writer_path/*",
                        "w_sam/*",
                        "datetime/*",
                        "todo/*",
                    ],
                    "blocked": []
                }
            }
        }
        child_override = {
            "type": "book_architect",
            "agent_config": {
                "llm_profile": "gemini-pro-batch",
                "tools": {
                    "allowed": [
                        "!w_sam/*",        # Remove standard w_sam
                        "+w_sam_gemini/*", # Add gemini version
                    ],
                    "blocked": ["+w_sam/*"]  # Also block it explicitly
                }
            }
        }
        
        result = _deep_merge_dict(parent_config, child_override)
        
        # w_sam should be removed from allowed
        assert "w_sam/*" not in result["agent_config"]["tools"]["allowed"]
        # w_sam_gemini should be added
        assert "w_sam_gemini/*" in result["agent_config"]["tools"]["allowed"]
        # Other parent tools should remain
        assert "writer_content/*" in result["agent_config"]["tools"]["allowed"]
        assert "datetime/*" in result["agent_config"]["tools"]["allowed"]
        
        # blocked should have w_sam
        assert "w_sam/*" in result["agent_config"]["tools"]["blocked"]
        
        # llm_profile should be overridden
        assert result["agent_config"]["llm_profile"] == "gemini-pro-batch"


class TestToolListInheritance:
    """Regression: absent tools keys in a child must not wipe parent lists.

    ToolConfig.__init__ used to inject allowed/blocked into the pydantic
    field set even when the YAML never mentioned them; exclude_unset in
    _resolve_server_inheritance then exported phantom empty lists which
    _merge_lists_with_syntax treats as a full replacement of the parent list.
    """

    def _config(self) -> AgentSystemConfig:
        # model_validate on plain dicts mirrors the YAML production path --
        # building ToolConfig objects by hand would sidestep the bug.
        return AgentSystemConfig.model_validate({
            "plugins": {
                "plugin_dirs": ["src/plugins"],
                "default_config": {"type": "basic_agent", "enabled": False},
                "servers": {
                    "parent_x": {
                        "type": "basic_agent",
                        "enabled": True,
                        "agent_config": {
                            "tools": {"allowed": ["tool_a/*"], "blocked": ["danger/*"]},
                        },
                    },
                    "child_x": {
                        "type": "parent_x",
                        "enabled": True,
                        "agent_config": {"tools": {"allowed": ["+tool_b/*"]}},
                    },
                    "child_y": {
                        "type": "parent_x",
                        "enabled": True,
                        "agent_config": {"tools": {"blocked": ["+more/*"]}},
                    },
                },
            },
        })

    def test_child_setting_only_allowed_keeps_parent_blocked(self):
        result = get_tool_server_config("child_x", self._config())
        tools = result.agent_config.tools
        assert tools.allowed == ["tool_a/*", "tool_b/*"]
        assert tools.blocked == ["danger/*"], "parent blocked list was wiped"

    def test_child_setting_only_blocked_keeps_parent_allowed(self):
        result = get_tool_server_config("child_y", self._config())
        tools = result.agent_config.tools
        assert tools.allowed == ["tool_a/*"], "parent allowed list was wiped"
        assert tools.blocked == ["danger/*", "more/*"]

    def test_explicit_none_is_still_normalized_to_empty_list(self):
        from agent_system.config.models import ToolConfig

        tc = ToolConfig.model_validate({"allowed": None})
        assert tc.allowed == []
        tc2 = ToolConfig(allowed=None, blocked=None)
        assert tc2.allowed == [] and tc2.blocked == []
