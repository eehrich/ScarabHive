"""Tests for simple_prompt_inject plugin.

Tests the hook-based prompt injection with various configurations,
including prompt_file loading and Jinja2 template rendering.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from agent_system.hooks import HookContext
from agent_system.hooks.plugin_hook import HookType
from agent_system.llm.models import ChatMessage


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def plugin_dir():
    """Get path to simple_prompt_inject plugin directory."""
    return Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "simple_prompt_inject"


@pytest.fixture
def make_plugin(plugin_dir):
    """Factory fixture to create plugin with custom config."""
    from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

    def _make(prompt_text: str = "Injected.", position: str = "before_last_user",
              role: str = "system", prompt_file: str = ""):
        mcp_config = MagicMock()
        mcp_config.config = {
            "prompt_text": prompt_text,
            "prompt_file": prompt_file,
            "injection_position": position,
            "role": role,
        }
        return SimplePromptInjectPlugin(plugin_dir, mcp_config)

    return _make


@pytest.fixture
def plugin(make_plugin):
    """Default plugin with standard config."""
    return make_plugin("Remember: always be concise.")


@pytest.fixture
def make_context():
    """Factory fixture to create HookContext with messages."""

    def _make(messages: list[ChatMessage] | None = None, agent=None):
        if messages is None:
            messages = [
                ChatMessage(role="system", content="You are a helpful assistant."),
                ChatMessage(role="user", content="Hello"),
            ]
        return HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="test-req-1",
            session_id="test-session-1",
            agent_name="test_agent",
            agent=agent,
            messages=messages,
        )

    return _make


@pytest.fixture
def mock_agent():
    """Mock agent with template_vars."""
    agent = MagicMock()
    agent.agent_config.template_vars = {"user_name": "Alice", "lang": "German"}
    agent._session_tracker = None  # No session tracker
    return agent


@pytest.fixture
def mock_agent_with_session():
    """Mock agent with session-scoped template_vars."""
    agent = MagicMock()
    agent.agent_config.template_vars = {"user_name": "Alice", "lang": "German"}
    agent._session_tracker.get_session_template_vars.return_value = {
        "user_name": "Bob",
        "lang": "French",
        "extra": "session_only",
    }
    return agent


# ============================================================================
# Schema loading
# ============================================================================

class TestSchemaLoading:
    """Test that schema.yaml is loaded correctly."""

    def test_hooks_loaded(self, plugin):
        hooks = plugin.get_hooks()
        assert len(hooks) == 1
        assert hooks[0]["name"] == "inject_prompt"
        assert hooks[0]["type"] == "PRE_LLM_CALL"

    def test_config_defaults(self, plugin_dir):
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin
        p = SimplePromptInjectPlugin(plugin_dir)  # no mcp_config → uses schema defaults
        assert p.prompt_template == ""
        assert p.injection_position == "before_last_user"
        assert p.role == "system"

    def test_config_override_via_mcp_config(self, make_plugin):
        p = make_plugin("custom text", "end", "user")
        assert p.prompt_template == "custom text"
        assert p.injection_position == "end"
        assert p.role == "user"


# ============================================================================
# Plugin factory
# ============================================================================

class TestPluginFactory:
    """Test the PLUGIN_FACTORY entry point."""

    def test_factory_returns_plugin(self):
        from plugins.simple_prompt_inject.plugin import PLUGIN_FACTORY
        plugin = PLUGIN_FACTORY()
        assert plugin is not None
        assert plugin.name == "simple_prompt_inject"

    def test_factory_with_mcp_config(self):
        from plugins.simple_prompt_inject.plugin import PLUGIN_FACTORY
        mcp_config = MagicMock()
        mcp_config.config = {"prompt_text": "hello"}
        plugin = PLUGIN_FACTORY(mcp_config=mcp_config)
        assert plugin.prompt_template == "hello"


# ============================================================================
# inject_prompt hook
# ============================================================================

class TestInjectPrompt:
    """Test the inject_prompt hook handler."""

    @pytest.mark.asyncio
    async def test_no_injection_when_prompt_empty(self, make_plugin, make_context):
        """Empty prompt_text should be a no-op."""
        p = make_plugin("")
        ctx = make_context()
        original_len = len(ctx.messages)

        result = await p.inject_prompt(ctx)

        assert result.success is True
        assert result.modified is False
        assert len(result.context.messages) == original_len

    @pytest.mark.asyncio
    async def test_no_injection_when_messages_none(self, plugin):
        """None messages should be a no-op."""
        ctx = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="r1",
            session_id="s1",
            messages=None,
        )
        result = await plugin.inject_prompt(ctx)
        assert result.success is True
        assert result.modified is False

    @pytest.mark.asyncio
    async def test_inject_before_last_user(self, plugin, make_context):
        """Default position: inject before the last user message."""
        ctx = make_context()
        result = await plugin.inject_prompt(ctx)

        assert result.success is True
        assert result.modified is True

        msgs = result.context.messages
        # Expect: system, INJECTED, user
        assert len(msgs) == 3
        assert msgs[0].role == "system"
        assert msgs[1].injected_by == "simple_prompt_inject"
        assert msgs[1].role == "system"
        assert msgs[1].content == "Remember: always be concise."
        assert msgs[2].role == "user"
        assert msgs[2].content == "Hello"

    @pytest.mark.asyncio
    async def test_inject_at_end(self, make_plugin, make_context):
        """Position 'end' with role=system should still go after system messages."""
        p = make_plugin("Appended text.", position="end")
        ctx = make_context()
        result = await p.inject_prompt(ctx)

        msgs = result.context.messages
        assert len(msgs) == 3
        # System role forces injection after system messages, not at end
        assert msgs[1].injected_by == "simple_prompt_inject"
        assert msgs[1].content == "Appended text."
        assert msgs[1].role == "system"

    @pytest.mark.asyncio
    async def test_inject_at_end_with_user_role(self, make_plugin, make_context):
        """Position 'end' with role=user should actually append."""
        p = make_plugin("Appended text.", position="end", role="user")
        ctx = make_context()
        result = await p.inject_prompt(ctx)

        msgs = result.context.messages
        assert len(msgs) == 3
        assert msgs[-1].injected_by == "simple_prompt_inject"
        assert msgs[-1].content == "Appended text."
        assert msgs[-1].role == "user"

    @pytest.mark.asyncio
    async def test_inject_with_user_role(self, make_plugin, make_context):
        """Should support injecting as user role."""
        p = make_plugin("User inject", role="user")
        ctx = make_context()
        result = await p.inject_prompt(ctx)

        msgs = result.context.messages
        injected = [m for m in msgs if m.injected_by == "simple_prompt_inject"]
        assert len(injected) == 1
        assert injected[0].role == "user"

    @pytest.mark.asyncio
    async def test_replaces_previous_injection(self, plugin, make_context):
        """Running the hook twice should replace, not duplicate."""
        ctx = make_context()

        result1 = await plugin.inject_prompt(ctx)
        assert len(result1.context.messages) == 3

        # Run again on the already-injected context
        result2 = await plugin.inject_prompt(result1.context)
        assert len(result2.context.messages) == 3  # still 3, not 4

        injected = [m for m in result2.context.messages if m.injected_by == "simple_prompt_inject"]
        assert len(injected) == 1

    @pytest.mark.asyncio
    async def test_no_user_message_appends(self, make_plugin):
        """If there's no user message, injection falls back to append."""
        p = make_plugin("Injected.")
        ctx = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="r1",
            session_id="s1",
            messages=[ChatMessage(role="system", content="System only")],
        )
        result = await p.inject_prompt(ctx)

        msgs = result.context.messages
        assert len(msgs) == 2
        assert msgs[-1].injected_by == "simple_prompt_inject"

    @pytest.mark.asyncio
    async def test_does_not_mutate_original_list(self, plugin, make_context):
        """The original messages list should not be modified."""
        ctx = make_context()
        original_messages = ctx.messages
        original_len = len(original_messages)

        await plugin.inject_prompt(ctx)

        assert len(original_messages) == original_len

    @pytest.mark.asyncio
    async def test_multiple_user_messages_system_role(self, plugin):
        """With role=system, injects after system messages (not mid-conversation)."""
        ctx = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="r1",
            session_id="s1",
            messages=[
                ChatMessage(role="system", content="sys"),
                ChatMessage(role="user", content="first user"),
                ChatMessage(role="assistant", content="response"),
                ChatMessage(role="user", content="second user"),
            ],
        )
        result = await plugin.inject_prompt(ctx)
        msgs = result.context.messages

        assert len(msgs) == 5
        # System role: injected after system messages at the beginning
        assert msgs[1].injected_by == "simple_prompt_inject"
        assert msgs[1].role == "system"
        assert msgs[2].content == "first user"

    @pytest.mark.asyncio
    async def test_multiple_user_messages_user_role(self, make_plugin):
        """With role=user, injects before the last user message."""
        p = make_plugin("Remember: always be concise.", role="user")
        ctx = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="r1",
            session_id="s1",
            messages=[
                ChatMessage(role="system", content="sys"),
                ChatMessage(role="user", content="first user"),
                ChatMessage(role="assistant", content="response"),
                ChatMessage(role="user", content="second user"),
            ],
        )
        result = await p.inject_prompt(ctx)
        msgs = result.context.messages

        assert len(msgs) == 5
        assert msgs[3].injected_by == "simple_prompt_inject"
        assert msgs[3].role == "user"
        assert msgs[4].content == "second user"


# ============================================================================
# on_pre_llm_call dispatch
# ============================================================================

class TestDispatch:
    """Test that on_pre_llm_call correctly dispatches to inject_prompt."""

    @pytest.mark.asyncio
    async def test_on_pre_llm_call_dispatches(self, plugin, make_context):
        """SchemaBasedPluginHook should dispatch to inject_prompt."""
        ctx = make_context()
        result = await plugin.on_pre_llm_call(ctx)

        assert result.success is True
        assert result.modified is True
        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert len(injected) == 1

    @pytest.mark.asyncio
    async def test_on_pre_llm_call_no_op_when_empty(self, make_plugin, make_context):
        """Dispatch with empty prompt should be a no-op."""
        p = make_plugin("")
        ctx = make_context()
        result = await p.on_pre_llm_call(ctx)

        assert result.success is True
        assert result.modified is False


# ============================================================================
# prompt_file loading
# ============================================================================

class TestPromptFile:
    """Test loading prompts from .md files."""

    def test_load_from_file(self, plugin_dir, tmp_path):
        """prompt_file should load content from a .md file."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        md_file = tmp_path / "test_prompt.md"
        md_file.write_text("# Instructions\nBe helpful.", encoding="utf-8")

        mcp_config = MagicMock()
        mcp_config.config = {"prompt_file": str(md_file)}
        p = SimplePromptInjectPlugin(plugin_dir, mcp_config)

        assert p.prompt_template == "# Instructions\nBe helpful."

    def test_comments_in_the_file_are_not_injected(self, plugin_dir, tmp_path):
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        md_file = tmp_path / "test_prompt.md"
        md_file.write_text("<!-- editor note -->\nBe helpful.", encoding="utf-8")

        mcp_config = MagicMock()
        mcp_config.config = {"prompt_file": str(md_file)}
        p = SimplePromptInjectPlugin(plugin_dir, mcp_config)

        assert p.prompt_template == "Be helpful."

    def test_prompt_file_takes_precedence(self, plugin_dir, tmp_path):
        """prompt_file should override prompt_text when both are set."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        md_file = tmp_path / "from_file.md"
        md_file.write_text("From file", encoding="utf-8")

        mcp_config = MagicMock()
        mcp_config.config = {
            "prompt_text": "From text",
            "prompt_file": str(md_file),
        }
        p = SimplePromptInjectPlugin(plugin_dir, mcp_config)

        assert p.prompt_template == "From file"

    def test_file_not_found_raises(self, plugin_dir):
        """Missing prompt_file should raise FileNotFoundError."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        mcp_config = MagicMock()
        mcp_config.config = {"prompt_file": "/nonexistent/path.md"}

        with pytest.raises(FileNotFoundError, match="prompt_file not found"):
            SimplePromptInjectPlugin(plugin_dir, mcp_config)

    def test_empty_prompt_file_fallback_to_text(self, plugin_dir):
        """Empty prompt_file string should fall back to prompt_text."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        mcp_config = MagicMock()
        mcp_config.config = {"prompt_file": "", "prompt_text": "fallback text"}
        p = SimplePromptInjectPlugin(plugin_dir, mcp_config)

        assert p.prompt_template == "fallback text"

    @pytest.mark.asyncio
    async def test_inject_from_file(self, plugin_dir, tmp_path, make_context):
        """Content from prompt_file should be injected correctly."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        md_file = tmp_path / "inject.md"
        md_file.write_text("File-based prompt", encoding="utf-8")

        mcp_config = MagicMock()
        mcp_config.config = {"prompt_file": str(md_file)}
        p = SimplePromptInjectPlugin(plugin_dir, mcp_config)

        ctx = make_context()
        result = await p.inject_prompt(ctx)

        assert result.success is True
        assert result.modified is True
        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert len(injected) == 1
        assert injected[0].content == "File-based prompt"


# ============================================================================
# Jinja2 template rendering
# ============================================================================

class TestTemplateRendering:
    """Test Jinja2 template rendering with agent template_vars."""

    @pytest.mark.asyncio
    async def test_render_prompt_text_with_vars(self, plugin_dir, make_context, mock_agent):
        """prompt_text should be rendered with template_vars."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        mcp_config = MagicMock()
        mcp_config.config = {"prompt_text": "Hello {{ user_name }}, respond in {{ lang }}."}
        p = SimplePromptInjectPlugin(plugin_dir, mcp_config)

        ctx = make_context(agent=mock_agent)
        result = await p.inject_prompt(ctx)

        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert len(injected) == 1
        assert injected[0].content == "Hello Alice, respond in German."

    @pytest.mark.asyncio
    async def test_render_prompt_file_with_vars(self, plugin_dir, tmp_path, make_context, mock_agent):
        """prompt_file content should be rendered with template_vars."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        md_file = tmp_path / "tmpl.md"
        md_file.write_text("# Guide for {{ user_name }}\nLanguage: {{ lang }}", encoding="utf-8")

        mcp_config = MagicMock()
        mcp_config.config = {"prompt_file": str(md_file)}
        p = SimplePromptInjectPlugin(plugin_dir, mcp_config)

        ctx = make_context(agent=mock_agent)
        result = await p.inject_prompt(ctx)

        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert len(injected) == 1
        assert injected[0].content == "# Guide for Alice\nLanguage: German"

    @pytest.mark.asyncio
    async def test_session_vars_take_precedence(self, plugin_dir, make_context, mock_agent_with_session):
        """Session-scoped template_vars should override agent_config vars."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        mcp_config = MagicMock()
        mcp_config.config = {"prompt_text": "Hello {{ user_name }}, lang={{ lang }}, extra={{ extra }}."}
        p = SimplePromptInjectPlugin(plugin_dir, mcp_config)

        ctx = make_context(agent=mock_agent_with_session)
        result = await p.inject_prompt(ctx)

        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert injected[0].content == "Hello Bob, lang=French, extra=session_only."

    @pytest.mark.asyncio
    async def test_no_agent_skips_rendering(self, make_plugin, make_context):
        """Without agent context, template should be used as-is (no rendering)."""
        p = make_plugin("Hello {{ user_name }}.")
        ctx = make_context(agent=None)
        result = await p.inject_prompt(ctx)

        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert injected[0].content == "Hello {{ user_name }}."

    @pytest.mark.asyncio
    async def test_no_template_vars_skips_rendering(self, plugin_dir, make_context):
        """Agent without template_vars → template returned as-is."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        agent = MagicMock()
        agent.agent_config.template_vars = None
        agent._session_tracker = None

        mcp_config = MagicMock()
        mcp_config.config = {"prompt_text": "Literal {{ braces }}."}
        p = SimplePromptInjectPlugin(plugin_dir, mcp_config)

        ctx = make_context(agent=agent)
        result = await p.inject_prompt(ctx)

        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert injected[0].content == "Literal {{ braces }}."

    @pytest.mark.asyncio
    async def test_jinja_syntax_error_falls_back(self, plugin_dir, make_context, mock_agent):
        """Invalid Jinja2 syntax should gracefully fall back to raw template."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        mcp_config = MagicMock()
        mcp_config.config = {"prompt_text": "Bad syntax {% if %}"}
        p = SimplePromptInjectPlugin(plugin_dir, mcp_config)

        ctx = make_context(agent=mock_agent)
        result = await p.inject_prompt(ctx)

        assert result.success is True
        assert result.modified is True
        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert injected[0].content == "Bad syntax {% if %}"

    @pytest.mark.asyncio
    async def test_plain_text_no_vars_injected_as_is(self, plugin, make_context):
        """Plain text without Jinja2 syntax still works (no agent → no vars)."""
        ctx = make_context()
        result = await plugin.inject_prompt(ctx)

        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert injected[0].content == "Remember: always be concise."
