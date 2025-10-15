"""Tests for markdown_formatter plugin."""

import pytest
from pathlib import Path
from agent_system.llm.models import ChatMessage
from agent_system.hooks import HookContext, HookType
from plugins.markdown_formatter.hooks import MarkdownFormatterPlugin


@pytest.fixture
def formatter():
    """Create a markdown formatter plugin instance."""
    plugin_dir = Path(__file__).parent.parent / 'src' / 'plugins' / 'markdown_formatter'
    return MarkdownFormatterPlugin(plugin_dir)


def create_context(messages=None, session_id="test_session"):
    """Helper to create HookContext for testing."""
    return HookContext(
        hook_type=HookType.SESSION_START,
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
async def test_inject_markdown_system_prompt_only_once_per_session(formatter):
    """Test that system prompt is only injected once per session."""
    messages = [ChatMessage(role='user', content='Hello')]
    session_id = "persistent_session"
    
    # First call - should inject
    context1 = create_context(messages, session_id=session_id)
    result1 = await formatter.inject_markdown_system_prompt(context1)
    assert result1.modified is True
    
    # Second call with same session - should skip
    context2 = create_context(messages, session_id=session_id)
    result2 = await formatter.inject_markdown_system_prompt(context2)
    assert result2.modified is False
    assert result2.metadata['reason'] == 'already_injected'


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
    if not formatter.markdown_converter:
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
    if not formatter.markdown_converter:
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
    # If Rich is available, should have ANSI codes
    if result.modified:
        assert result.metadata['content_format'] == 'ansi'
        ansi_content = result.context.output
        # ANSI escape codes start with \x1b or \033
        assert '\x1b[' in ansi_content or '\033[' in ansi_content
    else:
        # Rich not available, should fallback to plain text
        assert result.metadata['content_format'] == 'text'
        assert result.metadata['reason'] == 'rich_not_available'


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
    """Test that HTML input is converted back to markdown then to ANSI."""
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
    # If both Rich and markdownify are available, should convert HTML->Markdown->ANSI
    if result.modified:
        assert result.metadata['content_format'] == 'ansi'
        ansi_content = result.context.output
        # Should have ANSI escape codes
        assert '\x1b[' in ansi_content or '\033[' in ansi_content
        # Should not contain raw HTML tags
        assert '<h1>' not in ansi_content
        assert '<pre>' not in ansi_content
    else:
        # Libraries not available, should fallback to plain text
        assert result.metadata['content_format'] == 'text'
