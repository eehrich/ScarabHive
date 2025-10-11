"""
Tests for Multi-Section Prompt System

Validates the new 3-level hierarchical prompt merging system:
1. Base template (config/prompts/system_prompt.yaml)
2. Agent template (agent_config.system_template)
3. Inline prompt override (agent_config.system_prompt)
"""
from pathlib import Path
from agent_system.servers.config_agent_factory import _load_prompt_sections, _load_system_prompt
from agent_system.config.models import ConfigBasedAgentDefinition, AgentConfig


class TestMultiSectionPrompts:
    """Test suite for multi-section prompt loading and merging."""

    def test_load_base_sections(self):
        """Test loading base template sections."""
        base_path = Path("config/prompts/system_prompt.yaml")
        sections = _load_prompt_sections(base_path, "test")
        
        # Base template should have these 3 sections
        assert "system_prompt" in sections
        assert "tools_prompt" in sections
        assert "general_instructions_prompt" in sections
        assert len(sections) == 3
        
        # All sections should be non-empty strings
        assert isinstance(sections["system_prompt"], str)
        assert len(sections["system_prompt"]) > 0
        assert isinstance(sections["tools_prompt"], str)
        assert len(sections["tools_prompt"]) > 0

    def test_load_agent_sections(self):
        """Test loading agent-specific template sections."""
        agent_path = Path("config/prompts/financial_analyst_prompt.yaml")
        sections = _load_prompt_sections(agent_path, "financial_analyst")
        
        # Agent template currently has only system_prompt
        assert "system_prompt" in sections
        assert isinstance(sections["system_prompt"], str)
        assert len(sections["system_prompt"]) > 0

    def test_section_merge_hierarchy(self):
        """Test that agent sections override base sections correctly."""
        base_path = Path("config/prompts/system_prompt.yaml")
        agent_path = Path("config/prompts/financial_analyst_prompt.yaml")
        
        base_sections = _load_prompt_sections(base_path, "test")
        agent_sections = _load_prompt_sections(agent_path, "test")
        
        # Merge: agent overrides base
        merged = base_sections.copy()
        merged.update(agent_sections)
        
        # Should have all 3 base sections
        assert len(merged) == 3
        assert "system_prompt" in merged
        assert "tools_prompt" in merged
        assert "general_instructions_prompt" in merged
        
        # system_prompt should be from agent, not base
        assert merged["system_prompt"] == agent_sections["system_prompt"]
        assert merged["system_prompt"] != base_sections["system_prompt"]
        
        # Other sections should be from base (not overridden)
        assert merged["tools_prompt"] == base_sections["tools_prompt"]
        assert merged["general_instructions_prompt"] == base_sections["general_instructions_prompt"]

    def test_section_priority_ordering(self):
        """Test that sections are ordered by priority, not alphabetically."""
        base_path = Path("config/prompts/system_prompt.yaml")
        sections = _load_prompt_sections(base_path, "test")
        
        # Define priority order
        section_order = ['system_prompt', 'tools_prompt', 'general_instructions_prompt']
        
        def sort_key(item):
            section_name, _ = item
            try:
                return (0, section_order.index(section_name))
            except ValueError:
                return (1, section_name)
        
        sorted_sections = sorted(sections.items(), key=sort_key)
        sorted_names = [name for name, _ in sorted_sections]
        
        # Should be in priority order (system_prompt first)
        assert sorted_names[0] == "system_prompt"
        assert sorted_names[1] == "tools_prompt"
        assert sorted_names[2] == "general_instructions_prompt"
        
        # Should NOT be alphabetical (which would be: general, system, tools)
        alphabetical = sorted(sections.keys())
        assert sorted_names != alphabetical

    def test_inline_prompt_override(self):
        """Test that inline prompt overrides only system_prompt section."""
        # Create agent config with inline prompt
        config = AgentConfig(
            system_template="config/prompts/financial_analyst_prompt.yaml",
            system_prompt="INLINE OVERRIDE PROMPT",
            max_steps=10
        )
        
        definition = ConfigBasedAgentDefinition(
            enabled=True,
            description="Test agent for inline prompt override",
            base_type="agent",
            agent_config=config
        )
        
        final_prompt = _load_system_prompt(definition, "test_agent")
        
        # Inline prompt should appear in final output
        assert "INLINE OVERRIDE PROMPT" in final_prompt
        
        # Base sections should still be present
        assert "tool" in final_prompt.lower()  # from tools_prompt
        assert "context" in final_prompt.lower() or "instruction" in final_prompt.lower()  # from general_instructions

    def test_full_prompt_assembly_with_template(self):
        """Test complete prompt assembly from template (no inline override)."""
        config = AgentConfig(
            system_template="config/prompts/financial_analyst_prompt.yaml",
            max_steps=15
        )
        
        definition = ConfigBasedAgentDefinition(
            enabled=True,
            description="Financial analyst for testing",
            base_type="agent",
            agent_config=config
        )
        
        final_prompt = _load_system_prompt(definition, "financial_analyst")
        
        # Should contain content from all sections
        assert final_prompt is not None
        assert len(final_prompt) > 100  # Should be substantial
        
        # Should have financial analyst-specific content
        assert "financial" in final_prompt.lower() or "stock" in final_prompt.lower()
        
        # Should have tools_prompt content
        assert "tool" in final_prompt.lower()

    def test_no_template_fallback_to_base(self):
        """Test that agents without templates use only base sections."""
        config = AgentConfig(
            max_steps=10
        )
        
        definition = ConfigBasedAgentDefinition(
            enabled=True,
            description="Test agent without template",
            base_type="agent",
            agent_config=config
        )
        
        final_prompt = _load_system_prompt(definition, "test_agent")
        
        # Should have base template content
        assert final_prompt is not None
        assert len(final_prompt) > 100
        assert "tool" in final_prompt.lower()  # from base tools_prompt

    def test_section_concatenation_format(self):
        """Test that sections are concatenated with proper formatting."""
        config = AgentConfig(
            system_template="config/prompts/financial_analyst_prompt.yaml",
            max_steps=10
        )
        
        definition = ConfigBasedAgentDefinition(
            enabled=True,
            description="Test agent for format checking",
            base_type="agent",
            agent_config=config
        )
        
        final_prompt = _load_system_prompt(definition, "test_agent")
        
        # Sections should be separated by double newlines
        assert "\n\n" in final_prompt
        
        # Non-system_prompt sections should have headers
        # (system_prompt has no header, others have "# section_name")
        assert "# tools_prompt" in final_prompt or "# general_instructions_prompt" in final_prompt

    def test_unknown_section_ordering(self):
        """Test that unknown sections come last and are sorted alphabetically."""
        # This test verifies future-proofing: if new sections are added,
        # they should come after known sections and be sorted alphabetically
        
        # Mock sections with known and unknown keys
        sections = {
            'system_prompt': 'content1',
            'tools_prompt': 'content2',
            'general_instructions_prompt': 'content3',
            'zebra_section': 'content4',
            'alpha_section': 'content5',
        }
        
        section_order = ['system_prompt', 'tools_prompt', 'general_instructions_prompt']
        
        def sort_key(item):
            section_name, _ = item
            try:
                return (0, section_order.index(section_name))
            except ValueError:
                return (1, section_name)
        
        sorted_sections = sorted(sections.items(), key=sort_key)
        sorted_names = [name for name, _ in sorted_sections]
        
        # Known sections should come first in priority order
        assert sorted_names[:3] == ['system_prompt', 'tools_prompt', 'general_instructions_prompt']
        
        # Unknown sections should come last, sorted alphabetically
        assert sorted_names[3:] == ['alpha_section', 'zebra_section']
