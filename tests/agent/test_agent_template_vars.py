"""Tests for agent template variables feature.

Tests cover:
- AgentConfig.template_vars field parsing
- Template variables being passed to Jinja2 rendering
- Variable precedence (custom vars override built-in vars)
- Integration with prompt strategies
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from agent_system.config.models import AgentConfig, AgentSystemConfig, ContextConfig
from agent_system.servers.agent.prompt_strategies import (
    PromptContext,
    RawPromptStrategy,
    TemplateFileStrategy,
    PromptRenderer,
)


@pytest.fixture
def mock_system_config() -> AgentSystemConfig:
    """Create a minimal system config for tests."""
    config = MagicMock(spec=AgentSystemConfig)
    config.context = MagicMock(spec=ContextConfig)
    config.context.auto_datetime = False
    config.context.timezone = "UTC"
    config.context.location = "Test"
    return config


@pytest.fixture
def mock_agent_instance() -> MagicMock:
    """Create a mock agent instance."""
    instance = MagicMock()
    # Remove get_custom_system_prompt to avoid SubclassHookStrategy
    del instance.get_custom_system_prompt
    return instance


class TestAgentConfigTemplateVars:
    """Tests for AgentConfig.template_vars field."""

    def test_template_vars_default_none(self) -> None:
        """Test that template_vars defaults to None."""
        config = AgentConfig()
        assert config.template_vars is None

    def test_template_vars_with_dict(self) -> None:
        """Test template_vars accepts a dictionary."""
        config = AgentConfig(template_vars={"var1": "value1", "var2": 42})
        assert config.template_vars == {"var1": "value1", "var2": 42}

    def test_template_vars_with_nested_dict(self) -> None:
        """Test template_vars accepts nested structures."""
        config = AgentConfig(template_vars={
            "author": "TestAuthor",
            "settings": {"debug": True, "level": 3},
            "tags": ["tag1", "tag2"],
        })
        assert config.template_vars["author"] == "TestAuthor"
        assert config.template_vars["settings"]["debug"] is True
        assert config.template_vars["tags"] == ["tag1", "tag2"]

    def test_template_vars_empty_dict(self) -> None:
        """Test template_vars with empty dict."""
        config = AgentConfig(template_vars={})
        assert config.template_vars == {}


class TestPromptStrategyContextValues:
    """Tests for _get_context_values including template_vars."""

    def test_context_values_without_template_vars(
        self, mock_system_config: AgentSystemConfig, mock_agent_instance: MagicMock
    ) -> None:
        """Test context values when no template_vars defined."""
        agent_config = AgentConfig()
        
        context = PromptContext(
            agent_name="test_agent",
            agent_config=agent_config,
            system_config=mock_system_config,
            available_tools=["tool1", "tool2"],
            max_steps=10,
            current_step=1,
            agent_instance=mock_agent_instance,
        )
        
        strategy = RawPromptStrategy()
        context_vals = strategy._get_context_values(context)
        
        assert context_vals["tools"] == ["tool1", "tool2"]
        assert context_vals["max_steps"] == 10
        assert context_vals["current_step"] == 1
        # No custom vars should be added
        assert "custom_var" not in context_vals

    def test_context_values_with_template_vars(
        self, mock_system_config: AgentSystemConfig, mock_agent_instance: MagicMock
    ) -> None:
        """Test context values include template_vars."""
        agent_config = AgentConfig(template_vars={
            "project_name": "MyProject",
            "author": "TestAuthor",
            "version": "1.0.0",
        })
        
        context = PromptContext(
            agent_name="test_agent",
            agent_config=agent_config,
            system_config=mock_system_config,
            available_tools=["tool1"],
            max_steps=10,
            current_step=1,
            agent_instance=mock_agent_instance,
        )
        
        strategy = RawPromptStrategy()
        context_vals = strategy._get_context_values(context)
        
        # Built-in values present
        assert context_vals["tools"] == ["tool1"]
        assert context_vals["max_steps"] == 10
        
        # Custom template vars present
        assert context_vals["project_name"] == "MyProject"
        assert context_vals["author"] == "TestAuthor"
        assert context_vals["version"] == "1.0.0"

    def test_template_vars_override_builtin_vars(
        self, mock_system_config: AgentSystemConfig, mock_agent_instance: MagicMock
    ) -> None:
        """Test that template_vars override built-in variables."""
        agent_config = AgentConfig(template_vars={
            "max_steps": 999,  # Override built-in
            "custom_var": "custom_value",
        })
        
        context = PromptContext(
            agent_name="test_agent",
            agent_config=agent_config,
            system_config=mock_system_config,
            available_tools=[],
            max_steps=10,
            current_step=1,
            agent_instance=mock_agent_instance,
        )
        
        strategy = RawPromptStrategy()
        context_vals = strategy._get_context_values(context)
        
        # template_vars should override built-in value
        assert context_vals["max_steps"] == 999
        assert context_vals["custom_var"] == "custom_value"


class TestRawPromptWithTemplateVars:
    """Tests for RawPromptStrategy with template_vars."""

    def test_raw_prompt_renders_template_vars(
        self, mock_system_config: AgentSystemConfig, mock_agent_instance: MagicMock
    ) -> None:
        """Test that template_vars are available in Jinja2 rendering."""
        agent_config = AgentConfig(
            system_prompt="You are the {{ project_name }} assistant by {{ author }}.",
            template_vars={
                "project_name": "AgentSystem",
                "author": "Developer",
            },
        )
        
        context = PromptContext(
            agent_name="test_agent",
            agent_config=agent_config,
            system_config=mock_system_config,
            available_tools=[],
            max_steps=10,
            current_step=1,
            agent_instance=mock_agent_instance,
        )
        
        strategy = RawPromptStrategy()
        system_prompt, tools_prompt = strategy.render(context)
        
        assert "AgentSystem" in system_prompt
        assert "Developer" in system_prompt
        assert tools_prompt is None

    def test_raw_prompt_with_complex_template_vars(
        self, mock_system_config: AgentSystemConfig, mock_agent_instance: MagicMock
    ) -> None:
        """Test template_vars with complex structures (lists, dicts)."""
        agent_config = AgentConfig(
            system_prompt=(
                "Project: {{ project.name }}\n"
                "Tags: {{ tags | join(', ') }}\n"
                "Config: {{ config.debug }}"
            ),
            template_vars={
                "project": {"name": "TestProject", "version": "2.0"},
                "tags": ["ai", "agent", "automation"],
                "config": {"debug": True},
            },
        )
        
        context = PromptContext(
            agent_name="test_agent",
            agent_config=agent_config,
            system_config=mock_system_config,
            available_tools=[],
            max_steps=10,
            current_step=1,
            agent_instance=mock_agent_instance,
        )
        
        strategy = RawPromptStrategy()
        system_prompt, _ = strategy.render(context)
        
        assert "Project: TestProject" in system_prompt
        assert "Tags: ai, agent, automation" in system_prompt
        assert "Config: True" in system_prompt


class TestTemplateFileWithTemplateVars:
    """Tests for TemplateFileStrategy with template_vars."""

    def test_template_file_renders_template_vars(
        self, 
        tmp_path: Path,
        mock_system_config: AgentSystemConfig, 
        mock_agent_instance: MagicMock
    ) -> None:
        """Test that template_vars are passed to file template rendering."""
        # Create a markdown template file
        template_file = tmp_path / "agent_prompt.md"
        template_file.write_text(
            "# {{ project_name }} Agent\n\n"
            "Version: {{ version }}\n"
            "Current step: {{ current_step }}/{{ max_steps }}",
            encoding="utf-8"
        )
        
        agent_config = AgentConfig(
            system_template=str(template_file),
            template_vars={
                "project_name": "CustomProject",
                "version": "3.0.0",
            },
        )
        
        context = PromptContext(
            agent_name="test_agent",
            agent_config=agent_config,
            system_config=mock_system_config,
            available_tools=[],
            max_steps=20,
            current_step=5,
            agent_instance=mock_agent_instance,
        )
        
        strategy = TemplateFileStrategy()
        system_prompt, _ = strategy.render(context)
        
        # Custom template vars
        assert "CustomProject" in system_prompt
        assert "3.0.0" in system_prompt
        # Built-in vars
        assert "5/20" in system_prompt

    def test_template_vars_pin_the_date_in_a_file_template(
        self, tmp_path: Path, mock_system_config: AgentSystemConfig, mock_agent_instance: MagicMock
    ) -> None:
        """The raw strategy honoured a pinned date; the file strategy merged the
        clock in a second time, on top of the template_vars."""
        template_file = tmp_path / "agent_prompt.md"
        template_file.write_text("Today is {{ current_date }}.", encoding="utf-8")
        mock_system_config.context.auto_datetime = True
        context = PromptContext(
            agent_name="test_agent",
            agent_config=AgentConfig(system_template=str(template_file),
                                     template_vars={"current_date": "2000-01-01"}),
            system_config=mock_system_config,
            available_tools=[],
            max_steps=20,
            current_step=5,
            agent_instance=mock_agent_instance,
        )

        system_prompt, _ = TemplateFileStrategy().render(context)

        assert system_prompt == "Today is 2000-01-01."


class TestPromptRendererWithTemplateVars:
    """Integration tests for PromptRenderer with template_vars."""

    def test_prompt_renderer_uses_template_vars(
        self, mock_system_config: AgentSystemConfig, mock_agent_instance: MagicMock
    ) -> None:
        """Test that PromptRenderer correctly uses template_vars."""
        agent_config = AgentConfig(
            system_prompt="Hello {{ name }}, you work on {{ project }}.",
            template_vars={
                "name": "Agent",
                "project": "AgentSystem",
            },
        )
        
        context = PromptContext(
            agent_name="test_agent",
            agent_config=agent_config,
            system_config=mock_system_config,
            available_tools=[],
            max_steps=10,
            current_step=1,
            agent_instance=mock_agent_instance,
        )
        
        renderer = PromptRenderer()
        system_prompt, tools_prompt = renderer.render(context)
        
        assert system_prompt == "Hello Agent, you work on AgentSystem."
        assert tools_prompt is None


class TestYAMLConfigParsing:
    """Tests for parsing template_vars from YAML config."""

    def test_parse_template_vars_from_dict(self) -> None:
        """Test parsing template_vars from dict (simulating YAML load)."""
        yaml_data = {
            "llm_profile": "chat",
            "system_prompt": "You are {{ agent_role }}.",
            "template_vars": {
                "agent_role": "a helpful assistant",
                "project": "TestProject",
            },
        }
        
        config = AgentConfig(**yaml_data)
        
        assert config.template_vars is not None
        assert config.template_vars["agent_role"] == "a helpful assistant"
        assert config.template_vars["project"] == "TestProject"
