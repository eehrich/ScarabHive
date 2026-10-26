"""A prompt can read its variables, not reach Python.

Prompts were rendered with a bare ``jinja2.Template`` / ``Environment``: a
template like ``{{ cycler.__init__.__globals__.os.getcwd() }}`` ran in the API
process. Today only an admin writes a prompt (agent YAML), but a prompt is the
very text a model would write when it proposes a variant of an agent -- so both
renderers (an inline ``system_prompt`` and a template file) are sandboxed.

Rendering every agent of the real config with and without the sandbox gave the
same 213 prompts (2026-10-26): what prompts legitimately do still works.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

from agent_system.config.models import AgentConfig, AgentSystemConfig, ContextConfig
from agent_system.servers.agent.prompt_strategies import PromptContext, RawPromptStrategy
from agent_system.utils.prompt_renderer import _render_text_template

#: Reaches the os module of the interpreter -- in a plain environment this prints the working directory.
ESCAPE = "{{ cycler.__init__.__globals__.os.getcwd() }}"
#: Builds far past the size limit; cheap enough here to finish without the limit, so a missing limit shows as
#: the printed result instead of a hanging test. The real threat (``9 ** 99999999``) freezes the process.
#: Neither result ("True", 1200000) appears in the source, which is what a refused render falls back to.
OVERSIZED = "[{{ (2 ** 200000) > 1 }}|{{ ('ab' * 600000) | length }}]"


def _context(system_prompt: str, **template_vars) -> PromptContext:
    system_config = MagicMock(spec=AgentSystemConfig)
    system_config.context = MagicMock(spec=ContextConfig)
    system_config.context.auto_datetime = False
    system_config.context.timezone = "UTC"
    system_config.context.location = "Test"
    instance = MagicMock()
    del instance.get_custom_system_prompt
    agent_config = AgentConfig(system_prompt=system_prompt, template_vars=template_vars or None)
    return PromptContext(agent_name="worker", agent_config=agent_config, system_config=system_config,
                         available_tools=["read", "write"], max_steps=10, current_step=1, agent_instance=instance)


class TestInlineSystemPrompt:
    def test_variables_and_filters_still_render(self):
        rendered, _ = RawPromptStrategy().render(
            _context("You are {{ role }}; tools: {{ tools | join(', ') }}.", role="worker"))
        assert rendered == "You are worker; tools: read, write."

    def test_python_internals_are_out_of_reach(self):
        rendered, _ = RawPromptStrategy().render(_context("Start. " + ESCAPE))
        assert os.getcwd() not in rendered

    def test_small_repetition_renders_oversized_one_does_not(self):
        assert RawPromptStrategy().render(_context("{{ '-' * 3 }}{{ 2 ** 3 }}"))[0] == "---8"
        rendered, _ = RawPromptStrategy().render(_context(OVERSIZED))
        assert "True" not in rendered and "1200000" not in rendered


class TestTemplateFile:
    def test_variables_and_includes_still_render(self, tmp_path: Path):
        (tmp_path / "part.md").write_text("rule {{ n }}", encoding="utf-8")
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Hello {{ name }}. {% include 'part.md' %}", encoding="utf-8")
        assert _render_text_template(str(prompt), {"name": "worker", "n": 3})["system_prompt"] == "Hello worker. rule 3"

    def test_python_internals_are_out_of_reach(self, tmp_path: Path):
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Start. " + ESCAPE, encoding="utf-8")
        assert os.getcwd() not in _render_text_template(str(prompt), {})["system_prompt"]

    def test_oversized_repetition_is_refused(self, tmp_path: Path):
        prompt = tmp_path / "prompt.md"
        prompt.write_text(OVERSIZED, encoding="utf-8")
        rendered = _render_text_template(str(prompt), {})["system_prompt"]
        assert "True" not in rendered and "1200000" not in rendered
