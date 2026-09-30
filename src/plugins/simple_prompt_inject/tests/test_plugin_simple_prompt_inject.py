"""Tests for simple_prompt_inject plugin.

Tests the hook-based prompt injection with various configurations,
including prompt_file loading and Jinja2 template rendering.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from types import SimpleNamespace
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
              role: str = "developer", prompt_file: str = ""):
        server_config = MagicMock()
        server_config.config = {
            "prompt_text": prompt_text,
            "prompt_file": prompt_file,
            "injection_position": position,
            "role": role,
        }
        return SimplePromptInjectPlugin(plugin_dir, server_config)

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
        p = SimplePromptInjectPlugin(plugin_dir)  # no server_config → uses schema defaults
        assert p.prompt_template == ""
        assert p.injection_position == "before_last_user"
        # Not "system": Anthropic and Gemini have no system role inside a
        # history and hoist such a message into the prompt head.
        assert p.role == "developer"

    def test_config_override_via_server_config(self, make_plugin):
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

    def test_factory_with_server_config(self):
        from plugins.simple_prompt_inject.plugin import PLUGIN_FACTORY
        server_config = MagicMock()
        server_config.config = {"prompt_text": "hello"}
        plugin = PLUGIN_FACTORY(server_config=server_config)
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
        assert msgs[1].role == "developer"
        assert msgs[1].content == "Remember: always be concise."
        assert msgs[2].role == "user"
        assert msgs[2].content == "Hello"

    @pytest.mark.asyncio
    async def test_inject_before_a_wake(self, plugin, make_context):
        """A woken run opens its turn with a `developer` message, not a `user`
        one. Anchored on the role alone the reminder lands in front of a
        question from an EARLIER turn -- behind an assistant turn and the whole
        exchange after it, which is the one thing "before_last_user" is for."""
        ctx = make_context([
            ChatMessage(role="user", content="analysiere X"),
            ChatMessage(role="assistant", content="fertig"),
            ChatMessage(role="developer", content="You were woken because input is waiting."),
        ])

        msgs = (await plugin.inject_prompt(ctx)).context.messages

        assert [m.injected_by for m in msgs] == [None, None, "simple_prompt_inject", None]
        assert msgs[3].role == "developer", "the reminder goes in front of the wake"

    @pytest.mark.asyncio
    async def test_a_delivered_message_is_what_gets_answered(self, plugin, make_context):
        """The anchor is whatever stands LAST, marked or not. A direct message
        delivered mid-run, a continuation nudge, a debate post -- all `user`
        with an `injected_by`, all appended after the person's task, and all of
        them the thing the model is about to answer. Anchored on the head of
        the turn instead, the reminder sits in front of the whole exchange that
        already answered it, which is the one place it must not be."""
        ctx = make_context([
            ChatMessage(role="user", content="analysiere X"),
            ChatMessage(role="assistant", content="fertig"),
            ChatMessage(role="user", content="v6 fragt: wie weit bist du?",
                        injected_by="debate_forum_direct"),
        ])

        msgs = (await plugin.inject_prompt(ctx)).context.messages

        assert msgs[2].injected_by == "simple_prompt_inject"
        assert msgs[3].injected_by == "debate_forum_direct", \
            "the reminder must be the last thing before what is being answered"

    @pytest.mark.asyncio
    async def test_a_note_inside_the_turn_is_not_the_anchor(self, plugin, make_context):
        """The counter-proof, and why this asks for the marker and not for the
        role: the loop's own notes are `developer` as well and stand INSIDE a
        turn. Anchored on one, the reminder would drift backwards every step."""
        ctx = make_context([
            ChatMessage(role="user", content="analysiere X"),
            ChatMessage(role="developer", content="2 steps left",
                        injected_by="agent.step_budget"),
        ])

        msgs = (await plugin.inject_prompt(ctx)).context.messages

        assert msgs[0].injected_by == "simple_prompt_inject"
        assert msgs[1].content == "analysiere X", "the anchor is the task, not the note"

    @pytest.mark.asyncio
    async def test_inject_at_end(self, make_plugin, make_context):
        """Position 'end' means the end for a developer note."""
        p = make_plugin("Appended text.", position="end")
        ctx = make_context()
        before = [m.content for m in ctx.messages]
        result = await p.inject_prompt(ctx)

        msgs = result.context.messages
        assert len(msgs) == 3
        assert msgs[-1].injected_by == "simple_prompt_inject"
        assert msgs[-1].content == "Appended text."
        assert [m.content for m in msgs[:len(before)]] == before

    @pytest.mark.asyncio
    async def test_an_unchanged_text_at_the_end_is_not_written_again(
            self, make_plugin, make_context):
        """Two identical writes move the end of the prompt for nothing."""
        p = make_plugin("Appended text.", position="end")
        ctx = make_context()
        first = await p.inject_prompt(ctx)

        second = await p.inject_prompt(first.context)

        assert second.modified is False
        blocks = [m for m in second.context.messages
                  if m.injected_by == "simple_prompt_inject"]
        assert len(blocks) == 1

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
    async def test_a_changed_rendering_is_appended_behind_the_old_one(
            self, make_plugin, make_context):
        """The text is Jinja-rendered, so it can change between calls.

        A guard on mere EXISTENCE would freeze the first rendering forever.
        """
        p = make_plugin("Phase: {{ phase }}.", position="end", role="developer")
        agent = MagicMock()
        agent._session_tracker.get_session_template_vars.return_value = {"phase": "draft"}
        ctx = make_context(agent=agent)
        first = await p.inject_prompt(ctx)
        assert first.modified is True
        first_block = [m for m in first.context.messages
                       if m.injected_by == "simple_prompt_inject"][0]
        assert first_block.content == "Phase: draft."

        agent._session_tracker.get_session_template_vars.return_value = {"phase": "review"}
        second = await p.inject_prompt(first.context)

        assert second.modified is True
        blocks = [m for m in second.context.messages
                  if m.injected_by == "simple_prompt_inject"]
        assert len(blocks) == 2, "the new rendering is appended, the old one stays"
        assert blocks[0].content == "Phase: draft."
        assert blocks[-1].content == "Phase: review."

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
    async def test_multiple_user_messages_developer_role(self, plugin):
        """before_last_user with the default role: in front of the LAST user message."""
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
        # Default position is before_last_user: a reminder to be read just
        # before the model answers, wherever the head of the prompt is.
        assert msgs[3].injected_by == "simple_prompt_inject"
        assert msgs[3].role == "developer"
        assert msgs[1].content == "first user"
        assert msgs[4].content == "second user"

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

        server_config = MagicMock()
        server_config.config = {"prompt_file": str(md_file)}
        p = SimplePromptInjectPlugin(plugin_dir, server_config)

        assert p.prompt_template == "# Instructions\nBe helpful."

    def test_comments_in_the_file_are_not_injected(self, plugin_dir, tmp_path):
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        md_file = tmp_path / "test_prompt.md"
        md_file.write_text("<!-- editor note -->\nBe helpful.", encoding="utf-8")

        server_config = MagicMock()
        server_config.config = {"prompt_file": str(md_file)}
        p = SimplePromptInjectPlugin(plugin_dir, server_config)

        assert p.prompt_template == "Be helpful."

    def test_prompt_file_takes_precedence(self, plugin_dir, tmp_path):
        """prompt_file should override prompt_text when both are set."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        md_file = tmp_path / "from_file.md"
        md_file.write_text("From file", encoding="utf-8")

        server_config = MagicMock()
        server_config.config = {
            "prompt_text": "From text",
            "prompt_file": str(md_file),
        }
        p = SimplePromptInjectPlugin(plugin_dir, server_config)

        assert p.prompt_template == "From file"

    def test_file_not_found_raises(self, plugin_dir):
        """Missing prompt_file should raise FileNotFoundError."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        server_config = MagicMock()
        server_config.config = {"prompt_file": "/nonexistent/path.md"}

        with pytest.raises(FileNotFoundError, match="prompt_file not found"):
            SimplePromptInjectPlugin(plugin_dir, server_config)

    def test_empty_prompt_file_fallback_to_text(self, plugin_dir):
        """Empty prompt_file string should fall back to prompt_text."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        server_config = MagicMock()
        server_config.config = {"prompt_file": "", "prompt_text": "fallback text"}
        p = SimplePromptInjectPlugin(plugin_dir, server_config)

        assert p.prompt_template == "fallback text"

    @pytest.mark.asyncio
    async def test_inject_from_file(self, plugin_dir, tmp_path, make_context):
        """Content from prompt_file should be injected correctly."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        md_file = tmp_path / "inject.md"
        md_file.write_text("File-based prompt", encoding="utf-8")

        server_config = MagicMock()
        server_config.config = {"prompt_file": str(md_file)}
        p = SimplePromptInjectPlugin(plugin_dir, server_config)

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

        server_config = MagicMock()
        server_config.config = {"prompt_text": "Hello {{ user_name }}, respond in {{ lang }}."}
        p = SimplePromptInjectPlugin(plugin_dir, server_config)

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

        server_config = MagicMock()
        server_config.config = {"prompt_file": str(md_file)}
        p = SimplePromptInjectPlugin(plugin_dir, server_config)

        ctx = make_context(agent=mock_agent)
        result = await p.inject_prompt(ctx)

        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert len(injected) == 1
        assert injected[0].content == "# Guide for Alice\nLanguage: German"

    @pytest.mark.asyncio
    async def test_session_vars_take_precedence(self, plugin_dir, make_context, mock_agent_with_session):
        """Session-scoped template_vars should override agent_config vars."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        server_config = MagicMock()
        server_config.config = {"prompt_text": "Hello {{ user_name }}, lang={{ lang }}, extra={{ extra }}."}
        p = SimplePromptInjectPlugin(plugin_dir, server_config)

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

        server_config = MagicMock()
        server_config.config = {"prompt_text": "Literal {{ braces }}."}
        p = SimplePromptInjectPlugin(plugin_dir, server_config)

        ctx = make_context(agent=agent)
        result = await p.inject_prompt(ctx)

        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert injected[0].content == "Literal {{ braces }}."

    @pytest.mark.asyncio
    async def test_jinja_syntax_error_falls_back(self, plugin_dir, make_context, mock_agent):
        """Invalid Jinja2 syntax should gracefully fall back to raw template."""
        from plugins.simple_prompt_inject.hooks import SimplePromptInjectPlugin

        server_config = MagicMock()
        server_config.config = {"prompt_text": "Bad syntax {% if %}"}
        p = SimplePromptInjectPlugin(plugin_dir, server_config)

        ctx = make_context(agent=mock_agent)
        result = await p.inject_prompt(ctx)

        assert result.success is True
        assert result.modified is True
        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert injected[0].content == "Bad syntax {% if %}"

    @pytest.mark.asyncio
    async def test_a_text_without_template_syntax_is_the_same_with_and_without_vars(
            self, make_plugin, make_context, mock_agent_with_session):
        """Without session vars the text skips Jinja; with them it is rendered.
        For a text without template syntax both must give the text itself,
        trailing newline included -- `prompt_text: |` ends in one, and a text
        that changed when the first variable appeared moved the head behind
        the system prompt mid-session."""
        text = "Line one.\nLine two.\n"
        p = make_plugin(text, position="after_system", role="system")

        without = await p.inject_prompt(make_context())
        with_vars = await p.inject_prompt(make_context(agent=mock_agent_with_session))

        rendered = [[m.content for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
                    for result in (without, with_vars)]
        assert rendered == [[text], [text]], rendered

    @pytest.mark.asyncio
    async def test_plain_text_no_vars_injected_as_is(self, plugin, make_context):
        """Plain text without Jinja2 syntax still works (no agent → no vars)."""
        ctx = make_context()
        result = await plugin.inject_prompt(ctx)

        injected = [m for m in result.context.messages if m.injected_by == "simple_prompt_inject"]
        assert injected[0].content == "Remember: always be concise."


# ============================================================================
# after_system: part of the instructions, and it does not move
# ============================================================================

def _conversation():
    return [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="first user"),
        ChatMessage(role="assistant", content="response"),
        ChatMessage(role="user", content="second user"),
    ]


def _ctx(messages):
    return HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r1",
                       session_id="s1", messages=messages)


class TestAfterSystem:

    @pytest.mark.asyncio
    async def test_it_stands_right_behind_the_system_prompt(self, make_plugin):
        p = make_plugin("Rules.", position="after_system", role="system")
        msgs = (await p.inject_prompt(_ctx(_conversation()))).context.messages

        assert [m.content for m in msgs] == [
            "sys", "Rules.", "first user", "response", "second user"]
        assert msgs[1].role == "system"
        assert msgs[1].injected_by == "simple_prompt_inject"

    @pytest.mark.asyncio
    async def test_it_does_not_move_with_the_turn(self, make_plugin):
        """The point of the position: a new turn leaves the head alone, so the
        cached prefix behind it survives."""
        p = make_plugin("Rules.", position="after_system", role="system")
        first = (await p.inject_prompt(_ctx(_conversation()))).context.messages
        later = first + [ChatMessage(role="assistant", content="answer"),
                         ChatMessage(role="user", content="third user")]

        result = await p.inject_prompt(_ctx(later))

        assert result.modified is False
        assert result.context.messages is later
        assert [m.injected_by for m in later].count("simple_prompt_inject") == 1

    @pytest.mark.asyncio
    async def test_a_changed_text_replaces_the_copy_in_place(self, make_plugin):
        p = make_plugin("Rules v1.", position="after_system", role="system")
        first = (await p.inject_prompt(_ctx(_conversation()))).context.messages
        p.prompt_template = "Rules v2."

        msgs = (await p.inject_prompt(_ctx(first))).context.messages

        assert [m.content for m in msgs] == [
            "sys", "Rules v2.", "first user", "response", "second user"]

    @pytest.mark.asyncio
    async def test_behind_every_leading_system_message(self, make_plugin):
        p = make_plugin("Rules.", position="after_system", role="system")
        msgs = (await p.inject_prompt(_ctx(
            [ChatMessage(role="system", content="sys")] + _conversation()))).context.messages

        assert [m.content for m in msgs][:3] == ["sys", "sys", "Rules."]

    @pytest.mark.asyncio
    async def test_without_a_system_prompt_it_opens_the_history(self, make_plugin):
        p = make_plugin("Rules.", position="after_system", role="system")
        msgs = (await p.inject_prompt(_ctx(_conversation()[1:]))).context.messages

        assert msgs[0].content == "Rules."

    @pytest.mark.asyncio
    async def test_a_system_role_never_lands_inside_the_history(self, make_plugin, caplog):
        """role system with a mid-history position is a config mistake: it is
        placed at the head, and the log names the setting to fix."""
        with caplog.at_level("ERROR"):
            p = make_plugin("Rules.", position="before_last_user", role="system")
        msgs = (await p.inject_prompt(_ctx(_conversation()))).context.messages

        assert msgs[1].content == "Rules."
        assert "after_system" in caplog.text

    @pytest.mark.asyncio
    async def test_a_system_message_at_the_head_is_legitimate(self, make_plugin, caplog):
        with caplog.at_level("WARNING"):
            p = make_plugin("Rules.", position="after_system", role="system")
        msgs = (await p.inject_prompt(_ctx(_conversation()))).context.messages

        assert msgs[1].role == "system" and msgs[1].content == "Rules."
        assert caplog.text == "", "a legitimate configuration logs nothing"


# ============================================================================
# Through the real dispatcher (HookRegistry)
# ============================================================================

async def _registry(*instances):
    """A HookRegistry with one inject_prompt hook per (name, config) pair."""
    from agent_system.hooks import HookRegistry
    from plugins.simple_prompt_inject.plugin import PLUGIN_FACTORY

    registry = HookRegistry()
    for name, config in instances:
        plugin = PLUGIN_FACTORY(name=name, server_config=SimpleNamespace(config=config))
        await registry.register_hook(HookType.PRE_LLM_CALL, f"{name}.inject_prompt", plugin)
    return registry


async def _call(registry, messages, agent=None):
    ctx = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r1", session_id="s1",
                      agent_name="test_agent", agent=agent, messages=messages)
    return (await registry.execute_hooks(HookType.PRE_LLM_CALL, ctx)).messages


class TestThroughTheDispatcher:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("position", ["before_last_user", "end", "after_system"])
    async def test_two_instances_keep_their_own_notes(self, position):
        """A second instance used to take the first one's message for its own
        previous copy (one marker for both) and delete or skip it."""
        registry = await _registry(
            ("reminder_a", {"prompt_text": "A", "injection_position": position}),
            ("reminder_b", {"prompt_text": "B", "injection_position": position}))

        msgs = _conversation()
        for _ in range(2):
            msgs = await _call(registry, msgs)

        notes = sorted((m.injected_by, m.content) for m in msgs if m.injected_by)
        assert notes == [("reminder_a", "A"), ("reminder_b", "B")]

    @pytest.mark.asyncio
    async def test_an_agent_variable_survives_a_session_variable(self):
        """Session vars override the agent's key by key, as in the system
        prompt. Taking them INSTEAD lost every agent var as soon as the session
        held any variable -- another plugin's included."""
        agent = MagicMock()
        agent.agent_config.template_vars = {"lang": "German"}
        agent._session_tracker.get_session_template_vars.return_value = {"other": "x"}
        registry = await _registry(
            ("simple_prompt_inject", {"prompt_text": "Answer in {{ lang }}."}))

        msgs = await _call(registry, _conversation(), agent=agent)

        assert [m.content for m in msgs if m.injected_by] == ["Answer in German."]

    def test_an_unknown_position_or_role_falls_back_to_the_default(self, make_plugin, caplog):
        """Nothing checks the config against the schema's enums: an unknown role
        went to the provider on every call, an unknown position silently acted
        as before_last_user."""
        with caplog.at_level("ERROR"):
            p = make_plugin("Rules.", position="after-system", role="Developer")

        assert (p.injection_position, p.role) == ("before_last_user", "developer")
        assert "after-system" in caplog.text and "Developer" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("position,role", [
        ("after_system", "developer"), ("after_system", "user"),
        ("after_system", "system"), ("end", "user")])
    async def test_every_request_is_a_prefix_of_the_next(self, position, role):
        """Two turns with a tool round each; between the turns the session is
        saved the way the agent saves it (system prompt rebuilt, volatile notes
        dropped). These placements must never rewrite an earlier request."""
        from agent_system.servers.agent.components.session_tracking import is_volatile_note

        registry = await _registry(("simple_prompt_inject", {
            "prompt_text": "Rules.", "injection_position": position, "role": role}))
        system = ChatMessage(role="system", content="sys")
        msgs = [system, ChatMessage(role="user", content="u1")]
        requests = []
        for turn in (1, 2):
            for step in ("tool", "answer"):
                msgs = await _call(registry, msgs)
                requests.append([(m.role, m.content) for m in msgs])
                msgs = msgs + [ChatMessage(role="assistant", content=f"{step} {turn}")]
            kept = [m for m in msgs if m.role != "system" and not is_volatile_note(m)]
            msgs = [system] + kept + [ChatMessage(role="user", content=f"u{turn + 1}")]

        for earlier, later in zip(requests, requests[1:]):
            assert later[:len(earlier)] == earlier, (earlier, later)
        assert [c for _, c in requests[-1]].count("Rules.") == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("position", ["before_last_user", "end", "after_system"])
    async def test_an_empty_rendering_withdraws_the_note(self, position):
        """A text that renders empty from now on used to leave the copy of the
        call before in the list, and the stale note went on speaking."""
        agent = MagicMock()
        agent.agent_config.template_vars = {}
        registry = await _registry(("simple_prompt_inject", {
            "prompt_text": "{% if flag %}Hurry.{% endif %}", "injection_position": position}))

        agent._session_tracker.get_session_template_vars.return_value = {"flag": True}
        msgs = await _call(registry, _conversation(), agent=agent)
        assert [m.content for m in msgs if m.injected_by] == ["Hurry."]

        agent._session_tracker.get_session_template_vars.return_value = {"flag": False}
        msgs = await _call(registry, msgs, agent=agent)
        assert [m.content for m in msgs if m.injected_by] == []


# ============================================================================
# task_start: in front of the task, inside the user's own message
# ============================================================================

def _task_start(text="Oft gelesen: Akten."):
    return {"prompt_text": text, "injection_position": "task_start", "role": "user"}


def _task(content="Schreibe Beat B01"):
    return [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content=content)]


def _agent(**session_vars):
    agent = MagicMock()
    agent.agent_config.template_vars = {}
    agent._session_tracker.get_session_template_vars.return_value = session_vars
    return agent


class TestTaskStart:

    @pytest.mark.asyncio
    async def test_the_text_stands_in_front_of_the_task_which_stays_the_users(self):
        registry = await _registry(("hint", _task_start()))
        msgs = await _call(registry, _task())

        assert [m.role for m in msgs] == ["system", "user"]
        assert msgs[1].content == "Oft gelesen: Akten.\n\n---\n\nSchreibe Beat B01"
        assert msgs[1].injected_by is None, "a marked task is skipped by every turn counter"

    @pytest.mark.asyncio
    async def test_across_a_save_every_request_is_a_prefix_of_the_next(self):
        registry = await _registry(("hint", _task_start()))
        msgs, requests = _task(), []
        for turn in (1, 2):
            msgs = await _call(registry, msgs)
            requests.append([(m.role, m.content) for m in msgs])
            # Saved and read back the way the session does it.
            msgs = [ChatMessage(**m.model_dump(mode="json")) for m in msgs] + [
                ChatMessage(role="assistant", content=f"answer {turn}"),
                ChatMessage(role="user", content=f"turn {turn + 1}")]

        assert requests[1][:len(requests[0])] == requests[0]
        assert sum(c.count("Oft gelesen") for _, c in requests[-1]) == 1

    @pytest.mark.asyncio
    async def test_an_unchanged_text_leaves_the_list_alone(self, make_plugin):
        # A new list would be synced into the session for nothing on every call.
        p = make_plugin("H.", position="task_start", role="user")
        first = await p.inject_prompt(_ctx(_task()))
        again = await p.inject_prompt(_ctx(first.context.messages))

        assert first.modified and again.modified is False

    @pytest.mark.asyncio
    async def test_a_changed_text_replaces_the_old_and_an_empty_one_takes_it_out(self):
        registry = await _registry(("hint", _task_start("{{ note }}")))
        msgs = await _call(registry, _task("Task"), agent=_agent(note="Alt."))
        msgs = await _call(registry, msgs, agent=_agent(note="Neu."))
        assert msgs[1].content == "Neu.\n\n---\n\nTask"

        msgs = await _call(registry, msgs, agent=_agent(note=""))
        assert msgs[1].content == "Task" and not msgs[1].prefixed_by

    @pytest.mark.asyncio
    async def test_a_note_injected_before_it_is_not_the_task(self):
        registry = await _registry(("hint", _task_start("H.")))
        msgs = await _call(registry, [ChatMessage(role="system", content="sys"),
                                      ChatMessage(role="user", content="note", injected_by="other"),
                                      ChatMessage(role="user", content="Task")])

        assert [m.content for m in msgs[1:]] == ["note", "H.\n\n---\n\nTask"]

    @pytest.mark.asyncio
    async def test_two_instances_keep_their_own_prefix(self):
        from plugins.simple_prompt_inject.plugin import PLUGIN_FACTORY

        a, b = (PLUGIN_FACTORY(name=name, server_config=SimpleNamespace(config=_task_start(text)))
                for name, text in (("hint_a", "A."), ("hint_b", "B.")))
        msgs = _task("Task")
        for p in (a, b):
            msgs = (await p.inject_prompt(_ctx(msgs))).context.messages

        # The next call: each finds its text behind the other's and leaves it.
        assert [(await p.inject_prompt(_ctx(msgs))).modified for p in (a, b)] == [False, False]
        assert msgs[1].content.count("A.") == msgs[1].content.count("B.") == 1

    @pytest.mark.asyncio
    async def test_a_list_task_gets_it_in_its_first_text_part(self):
        registry = await _registry(("hint", _task_start("H.")))
        msgs = await _call(registry, [ChatMessage(role="system", content="sys"), ChatMessage(
            role="user", content=[{"type": "text", "text": "Task"},
                                  {"type": "image_url", "image_url": {"url": "u"}}])])

        parts = msgs[1].content
        assert parts[0].text == "H.\n\n---\n\nTask" and len(parts) == 2

    @pytest.mark.asyncio
    async def test_a_copy_left_from_another_position_goes(self):
        """The position changed over a restart; the saved session still held the
        note after the system prompt, and the model read the text twice."""
        registry = await _registry(("hint", _task_start("H.")))
        msgs = await _call(registry, [ChatMessage(role="system", content="sys"),
                                      ChatMessage(role="user", content="H.", injected_by="hint"),
                                      ChatMessage(role="user", content="Task")])

        assert [(m.role, m.content) for m in msgs] == [("system", "sys"),
                                                       ("user", "H.\n\n---\n\nTask")]

    @pytest.mark.asyncio
    async def test_a_task_sent_back_with_the_text_is_taken_over(self):
        """A retry from the web chat sends the first message again as the text
        it showed -- with the note, without the record."""
        registry = await _registry(("hint", _task_start("H.")))
        msgs = await _call(registry, _task("H.\n\n---\n\nTask"))

        assert msgs[1].content == "H.\n\n---\n\nTask"
        assert msgs[1].prefixed_by == {"hint": "H.\n\n---\n\n"}

    @pytest.mark.asyncio
    async def test_the_text_inside_the_task_is_the_tasks_own(self):
        """The v4 tasks join their parts with this very separator: a part that
        reads like the note is not the note, and a new text leaves it."""
        registry = await _registry(("hint", _task_start("{{ note }}")))
        task = "Teil 1\n\n---\n\nAlt.\n\n---\n\nTeil 2"
        msgs = await _call(registry, _task(task), agent=_agent(note="Alt."))
        assert msgs[1].content == "Alt.\n\n---\n\n" + task

        msgs = await _call(registry, msgs, agent=_agent(note="Neu."))
        assert msgs[1].content == "Neu.\n\n---\n\n" + task

    @pytest.mark.asyncio
    async def test_an_empty_task_is_left_alone(self):
        # Written in front, the message would be the note and a separator.
        registry = await _registry(("hint", _task_start("H.")))
        msgs = await _call(registry, _task("  "))

        assert msgs[1].content == "  " and not msgs[1].prefixed_by

    @pytest.mark.asyncio
    async def test_an_archive_placeholder_is_not_the_task(self):
        import json

        registry = await _registry(("hint", _task_start("H.")))
        ref = json.dumps({"type": "archived_ref", "ref_id": "a1", "summary": "s"})
        msgs = await _call(registry, [ChatMessage(role="system", content="sys"),
                                      ChatMessage(role="user", content=ref),
                                      ChatMessage(role="user", content="Task")])

        assert [m.content for m in msgs[1:]] == [ref, "H.\n\n---\n\nTask"]

    @pytest.mark.asyncio
    async def test_without_a_task_nothing_happens(self):
        registry = await _registry(("hint", _task_start()))
        msgs = await _call(registry, [ChatMessage(role="system", content="sys")])

        assert [(m.role, m.content) for m in msgs] == [("system", "sys")]

    @pytest.mark.parametrize("role", ["developer", "system"])
    def test_another_role_is_logged_and_the_task_stays_the_users(self, make_plugin, caplog, role):
        with caplog.at_level("ERROR"):
            p = make_plugin("H.", position="task_start", role=role)

        assert (p.injection_position, p.role) == ("task_start", "user")
        assert "task_start" in caplog.text
