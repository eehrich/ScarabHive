"""Tests for markdown_formatter plugin."""

import pytest
from pathlib import Path
from agent_system.llm.models import ChatMessage
from agent_system.hooks import HookContext, HookType
from plugins.markdown_formatter.hooks import MarkdownFormatterPlugin


@pytest.fixture
def formatter():
    """Create a markdown formatter plugin instance."""
    plugin_dir = Path(__file__).parent.parent.parent.parent.parent / 'src' / 'plugins' / 'markdown_formatter'
    return MarkdownFormatterPlugin(plugin_dir)


def create_context(messages=None, session_id="test_session", hook_type=HookType.PRE_LLM_CALL):
    """Helper to create HookContext for testing."""
    return HookContext(
        hook_type=hook_type,
        request_id="test",
        session_id=session_id,
        agent=None,
        agent_name="test_agent",
        messages=messages or []
    )


@pytest.mark.asyncio
async def test_inject_markdown_system_prompt_new_session(formatter):
    """Test system prompt injection for new session."""
    messages = [
        ChatMessage(role='user', content='Hello'),
    ]
    
    context = create_context(messages, session_id="new_session_1")
    result = await formatter.inject_markdown_system_prompt(context)
    
    assert result.success is True
    assert result.modified is True
    assert len(result.context.messages) == 2  # System message + user message
    assert result.context.messages[0].role == 'system'
    assert 'Markdown' in result.context.messages[0].content


@pytest.mark.asyncio
async def test_inject_markdown_system_prompt_with_existing_system(formatter):
    """Test that system prompt is appended to existing system message."""
    messages = [
        ChatMessage(role='system', content='You are a helpful assistant'),
        ChatMessage(role='user', content='Hello'),
    ]
    
    context = create_context(messages, session_id="new_session_2")
    result = await formatter.inject_markdown_system_prompt(context)
    
    assert result.success is True
    assert result.modified is True
    assert len(result.context.messages) == 2  # Still 2 messages
    # System prompt should be appended
    assert 'helpful assistant' in result.context.messages[0].content
    assert 'Markdown' in result.context.messages[0].content


@pytest.mark.asyncio
async def test_inject_markdown_system_prompt_prevents_duplication(formatter):
    """Test that system prompt is not injected twice if already present."""
    # Create a system message that already contains the markdown prompt
    existing_content = f"You are a helpful assistant.\n\n{formatter.system_prompt_template}"
    messages = [
        ChatMessage(role='system', content=existing_content),
        ChatMessage(role='user', content='Hello'),
    ]
    
    context = create_context(messages, session_id="test_session")
    result = await formatter.inject_markdown_system_prompt(context)
    
    # Should not modify if prompt already present
    assert result.success is True
    assert result.modified is False
    # Content should not be duplicated
    assert result.context.messages[0].content.count(formatter.system_prompt_template) == 1


@pytest.mark.asyncio
async def test_inject_markdown_system_prompt_disabled(formatter):
    """Test that injection can be disabled."""
    formatter.inject_system_prompt = False
    
    messages = [ChatMessage(role='user', content='Hello')]
    context = create_context(messages, session_id="new_session_3")
    result = await formatter.inject_markdown_system_prompt(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] == 'injection_disabled'


@pytest.mark.asyncio
async def test_inject_works_with_dict_messages(formatter):
    """Test that injection works with dict messages."""
    messages = [
        {'role': 'system', 'content': 'Existing system'},
        {'role': 'user', 'content': 'Hello'},
    ]
    
    context = create_context(messages, session_id="new_session_4")
    result = await formatter.inject_markdown_system_prompt(context)
    
    assert result.success is True
    assert result.modified is True
    # System message should have been modified
    first_msg = result.context.messages[0]
    if isinstance(first_msg, dict):
        assert 'Existing system' in first_msg['content']
        assert 'Markdown' in first_msg['content']
    else:
        assert 'Existing system' in first_msg.content
        assert 'Markdown' in first_msg.content


@pytest.mark.asyncio
async def test_format_markdown_output_conversion(formatter):
    """Test Markdown to HTML conversion."""
    # Skip if markdown library not available
    try:
        import markdown  # noqa: F401
    except ImportError:
        pytest.skip("Markdown library not available")
    
    output = '# Hello\n\nThis is **bold** and this is `code`.'
    
    context = HookContext(
        hook_type=HookType.FORMAT_OUTPUT,
        request_id="test",
        session_id="test_session",
        agent=None,
        agent_name="test_agent",
        output=output,
        output_format='html'  # Request HTML format
    )
    
    result = await formatter.format_markdown_output(context)
    
    assert result.success is True
    assert result.modified is True
    
    html_content = result.context.output
    assert '<h1>' in html_content
    assert '<strong>' in html_content or '<b>' in html_content
    assert '<code>' in html_content
    assert result.metadata['content_format'] == 'html'


@pytest.mark.asyncio
async def test_format_markdown_output_disabled(formatter):
    """Test that conversion can be disabled."""
    formatter.convert_to_html = False
    
    output = '# Hello'
    
    context = HookContext(
        hook_type=HookType.FORMAT_OUTPUT,
        request_id="test",
        session_id="test_session",
        agent=None,
        agent_name="test_agent",
        output=output
    )
    
    result = await formatter.format_markdown_output(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['content_format'] == 'text'


@pytest.mark.asyncio
async def test_format_markdown_output_no_output(formatter):
    """Test handling when there's no output."""
    context = HookContext(
        hook_type=HookType.FORMAT_OUTPUT,
        request_id="test",
        session_id="test_session",
        agent=None,
        agent_name="test_agent",
        output=None
    )
    
    result = await formatter.format_markdown_output(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] == 'no_output'
    assert result.metadata['content_format'] == 'text'


@pytest.mark.asyncio
async def test_html_sanitization(formatter):
    """Test that HTML sanitization removes dangerous content."""
    try:
        import markdown  # noqa: F401
    except ImportError:
        pytest.skip("Markdown library not available")
    
    # This would be dangerous if not sanitized
    output = '<script>alert("xss")</script>Normal **text**'
    
    context = HookContext(
        hook_type=HookType.FORMAT_OUTPUT,
        request_id="test",
        session_id="test_session",
        agent=None,
        agent_name="test_agent",
        output=output,
        output_format='html'  # Request HTML format
    )
    
    result = await formatter.format_markdown_output(context)
    
    assert result.success is True
    html_content = result.context.output
    # Script tag should be removed
    assert '<script>' not in html_content
    assert 'alert' not in html_content


@pytest.mark.asyncio
async def test_format_markdown_to_ansi(formatter):
    """Test conversion of Markdown to ANSI terminal output."""
    output = """# Test Header

This is **bold** and *italic* text.

```python
def hello():
    print("world")
```

- List item 1
- List item 2
"""
    
    context = HookContext(
        hook_type=HookType.FORMAT_OUTPUT,
        request_id="test",
        session_id="test_session",
        agent=None,
        agent_name="test_agent",
        output=output,
        output_format='ansi'  # Request ANSI format
    )
    
    result = await formatter.format_markdown_output(context)
    
    assert result.success is True
    # Plugin returns markdown with metadata indicating CLI should render with Rich
    # It does NOT generate ANSI codes itself - that's the CLI's job
    assert result.metadata['content_format'] == 'ansi'
    assert result.metadata.get('render_with_rich') is True
    # Content should still be markdown (not ANSI-encoded)
    assert result.context.output == output  # Unchanged markdown
    assert result.modified is False  # Not modified, just tagged for Rich rendering


@pytest.mark.asyncio
async def test_format_markdown_to_text(formatter):
    """Test that 'text' format returns markdown unchanged."""
    output = "# Header\n\n**Bold** text"
    
    context = HookContext(
        hook_type=HookType.FORMAT_OUTPUT,
        request_id="test",
        session_id="test_session",
        agent=None,
        agent_name="test_agent",
        output=output,
        output_format='text'  # Request text format
    )
    
    result = await formatter.format_markdown_output(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.context.output == output
    assert result.metadata['content_format'] == 'text'


@pytest.mark.asyncio
async def test_format_html_to_ansi(formatter):
    """Test that HTML input is converted back to markdown for Rich Console rendering."""
    # Simulate HTML output from agent (already formatted as HTML)
    html_output = """<h1>Header 1</h1>
<p><strong>Bold</strong> and <em>italic</em> text</p>
<pre><code class="language-python">def hello():
    print("world")
</code></pre>
<ul>
<li>Item 1</li>
<li>Item 2</li>
</ul>"""
    
    context = HookContext(
        hook_type=HookType.FORMAT_OUTPUT,
        request_id="test",
        session_id="test_session",
        agent=None,
        agent_name="test_agent",
        output=html_output,
        output_format='ansi'  # Request ANSI format (for CLI)
    )
    
    result = await formatter.format_markdown_output(context)
    
    assert result.success is True
    # Plugin converts HTML to Markdown for Rich Console rendering
    assert result.metadata['content_format'] == 'ansi'
    assert result.metadata.get('render_with_rich') is True
    assert result.metadata.get('converted_from') == 'html'
    # Content is converted from HTML to Markdown
    assert result.modified is True
    # Verify markdown content was generated (should contain # for headers, ** for bold, etc.)
    markdown_output = result.context.output
    assert '# Header 1' in markdown_output or 'Header 1' in markdown_output
    assert '**Bold**' in markdown_output or 'Bold' in markdown_output


@pytest.mark.asyncio
async def test_allowed_html_tags_config_is_applied():
    """``allowed_html_tags`` was read from the schema and never used."""
    pytest.importorskip("markdown")
    plugin_dir = Path(__file__).parent.parent
    formatter = MarkdownFormatterPlugin(plugin_dir)
    formatter.allowed_html_tags = {"p", "strong"}
    context = HookContext(
        hook_type=HookType.FORMAT_OUTPUT, request_id="test", session_id="s",
        agent=None, agent_name="test_agent",
        output="| A |\n|---|\n| 1 |\n\n**fett**", output_format="html")

    result = await formatter.format_markdown_output(context)

    assert "<table" not in result.context.output
    assert "<strong>fett</strong>" in result.context.output


@pytest.mark.asyncio
async def test_template_already_in_the_agent_prompt_touches_no_other_message():
    """Through the real HookRegistry, as the agent loop drives it: each step
    gets the list the previous one returned, with the first system message
    rendered fresh. An agent prompt that already carries the template pushed
    it into the next system message instead, one further per step -- the tools
    prompt, then a note mid-history -- rewriting the cached prefix each time."""
    from types import SimpleNamespace
    from agent_system.hooks.registry import HookRegistry
    from agent_system.plugins.discovery import register_plugin_hooks
    from plugins.markdown_formatter.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY("markdown_formatter", None, SimpleNamespace(config={}))
    registry = HookRegistry()
    await register_plugin_hooks("markdown_formatter", plugin, plugin.get_schema_data(), registry=registry)
    agent_prompt = f"agent prompt\n\n{plugin.system_prompt_template}"
    messages = [ChatMessage(role="system", content=agent_prompt),
                ChatMessage(role="system", content="tools prompt"),
                ChatMessage(role="user", content="hi"),
                ChatMessage(role="system", content="note mid-history"),
                ChatMessage(role="user", content="more")]
    for step in range(1, 4):
        messages[0] = ChatMessage(role="system", content=agent_prompt)
        out = await registry.execute_hooks(HookType.PRE_LLM_CALL, HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="s",
            agent_name="a", messages=messages, step=step))
        messages = out.messages

    assert [m.content for m in messages] == [
        agent_prompt, "tools prompt", "hi", "note mid-history", "more"]


@pytest.mark.asyncio
async def test_template_is_appended_to_the_fresh_agent_prompt_at_every_step():
    """The loop renders the first system message anew before each step; the
    template goes back onto it once, and nowhere else."""
    from types import SimpleNamespace
    from agent_system.hooks.registry import HookRegistry
    from agent_system.plugins.discovery import register_plugin_hooks
    from plugins.markdown_formatter.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY("markdown_formatter", None, SimpleNamespace(config={}))
    registry = HookRegistry()
    await register_plugin_hooks("markdown_formatter", plugin, plugin.get_schema_data(), registry=registry)
    messages = [ChatMessage(role="system", content="agent prompt"),
                ChatMessage(role="system", content="tools prompt"),
                ChatMessage(role="user", content="hi")]
    for step in range(1, 4):
        messages[0] = ChatMessage(role="system", content="agent prompt")
        out = await registry.execute_hooks(HookType.PRE_LLM_CALL, HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="s",
            agent_name="a", messages=messages, step=step))
        messages = out.messages
        assert [m.content for m in messages] == [
            f"agent prompt\n\n{plugin.system_prompt_template}", "tools prompt", "hi"]


@pytest.mark.parametrize("target", ["ansi", "text"])
@pytest.mark.asyncio
async def test_answer_wrapped_in_a_markdown_fence_is_unwrapped_for_the_terminal(target):
    """Through the real HookRegistry: the hook unwrapped the fence but answered
    modified=False, so the registry kept the original and the terminal showed
    the whole answer as a code block."""
    from types import SimpleNamespace
    from agent_system.hooks.registry import HookRegistry
    from agent_system.plugins.discovery import register_plugin_hooks
    from plugins.markdown_formatter.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY("markdown_formatter", None, SimpleNamespace(config={}))
    registry = HookRegistry()
    await register_plugin_hooks("markdown_formatter", plugin, plugin.get_schema_data(), registry=registry)
    out = await registry.execute_hooks(HookType.FORMAT_OUTPUT, HookContext(
        hook_type=HookType.FORMAT_OUTPUT, request_id="r", session_id="s", agent_name="a",
        output="```markdown\n# Title\n\n**x**\n```", output_format=target))

    assert out.output == "# Title\n\n**x**"
    assert out.metadata["content_format"] == target


@pytest.mark.asyncio
async def test_html_conversion_runs_off_the_event_loop_thread(monkeypatch):
    """A long answer of many lines takes seconds to convert; on the loop
    thread it blocked the server and the hook's timeout could not end it."""
    import threading
    from types import SimpleNamespace
    from agent_system.hooks.registry import HookRegistry
    from agent_system.plugins.discovery import register_plugin_hooks
    from plugins.markdown_formatter import hooks
    from plugins.markdown_formatter.plugin import PLUGIN_FACTORY

    seen = []

    def fake_markdown_to_html(text, **kwargs):
        seen.append(threading.get_ident())
        return "<p>converted</p>"

    monkeypatch.setattr(hooks, "markdown_to_html", fake_markdown_to_html)
    plugin = PLUGIN_FACTORY("markdown_formatter", None, SimpleNamespace(config={}))
    registry = HookRegistry()
    await register_plugin_hooks("markdown_formatter", plugin, plugin.get_schema_data(), registry=registry)
    out = await registry.execute_hooks(HookType.FORMAT_OUTPUT, HookContext(
        hook_type=HookType.FORMAT_OUTPUT, request_id="r", session_id="s", agent_name="a",
        output="# Title", output_format="html"))

    assert out.output == "<p>converted</p>"
    assert len(seen) == 1 and seen[0] != threading.get_ident()
