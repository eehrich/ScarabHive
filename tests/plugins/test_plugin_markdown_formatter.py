"""Tests for markdown_formatter plugin."""

import pytest
from pathlib import Path
from agent_system.llm.models import ChatMessage
from agent_system.hooks import HookContext, HookType
from plugins.markdown_formatter.hooks import MarkdownFormatterPlugin


@pytest.fixture
def formatter():
    """Create a markdown formatter plugin instance."""
    plugin_dir = Path(__file__).parent.parent.parent / 'src' / 'plugins' / 'markdown_formatter'
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
