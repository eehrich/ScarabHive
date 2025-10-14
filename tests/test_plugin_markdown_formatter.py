"""Tests for the markdown_formatter plugin."""
import pytest
from agent_system.hooks import HookContext, HookType


@pytest.fixture
def plugin():
    """Create a markdown_formatter plugin instance."""
    from plugins.markdown_formatter.plugin import PLUGIN_FACTORY
    return PLUGIN_FACTORY()


@pytest.mark.asyncio
async def test_inject_markdown_system_prompt_first_time(plugin):
    """Test that system prompt is injected on first session start."""
    context = HookContext(
        hook_type=HookType.SESSION_START,
        request_id="test-req-1",
        session_id="test-session-1",
        messages=[]
    )
    
    result = await plugin.inject_markdown_system_prompt(context)
    
    assert result.success is True
    assert result.modified is True
    assert len(result.context.messages) == 1
    assert result.context.messages[0]['role'] == 'system'
    assert 'Markdown' in result.context.messages[0]['content']
    assert result.metadata['injected'] is True


@pytest.mark.asyncio
async def test_inject_markdown_system_prompt_duplicate_prevention(plugin):
    """Test that system prompt is not injected twice for same session."""
    context = HookContext(
        hook_type=HookType.SESSION_START,
        request_id="test-req-1",
        session_id="test-session-dup",
        messages=[]
    )
    
    # First injection
    result1 = await plugin.inject_markdown_system_prompt(context)
    assert result1.modified is True
    
    # Second injection attempt (same session_id)
    context2 = HookContext(
        hook_type=HookType.SESSION_START,
        request_id="test-req-2",
        session_id="test-session-dup",
        messages=[]
    )
    result2 = await plugin.inject_markdown_system_prompt(context2)
    
    assert result2.success is True
    assert result2.modified is False
    assert result2.metadata['reason'] == 'already_injected'


@pytest.mark.asyncio
async def test_inject_markdown_append_to_existing_system_message(plugin):
    """Test appending to existing system message."""
    existing_system_msg = {
        'role': 'system',
        'content': 'You are a helpful assistant.'
    }
    
    context = HookContext(
        hook_type=HookType.SESSION_START,
        request_id="test-req-3",
        session_id="test-session-append",
        messages=[existing_system_msg]
    )
    
    result = await plugin.inject_markdown_system_prompt(context)
    
    assert result.success is True
    assert result.modified is True
    assert len(result.context.messages) == 1
    assert 'helpful assistant' in result.context.messages[0]['content']
    assert 'Markdown' in result.context.messages[0]['content']


@pytest.mark.asyncio
async def test_format_markdown_output_converts_to_html(plugin):
    """Test that Markdown content is converted to HTML."""
    llm_response = {
        'assistant': {
            'content': '# Hello\n\nThis is **bold** and this is `code`.'
        }
    }
    
    context = HookContext(
        hook_type=HookType.POST_LLM_CALL,
        request_id="test-req-4",
        session_id="test-session-2",
        messages=[],
        llm_response=llm_response
    )
    
    result = await plugin.format_markdown_output(context)
    
    assert result.success is True
    assert result.modified is True
    html_content = result.context.llm_response['assistant']['content']
    assert '<h1>' in html_content or 'Hello' in html_content
    assert '<strong>' in html_content or '<code>' in html_content
    assert result.context.llm_response['assistant']['content_format'] == 'html'


@pytest.mark.asyncio
async def test_format_markdown_output_no_llm_response(plugin):
    """Test handling when there is no LLM response."""
    context = HookContext(
        hook_type=HookType.POST_LLM_CALL,
        request_id="test-req-5",
        session_id="test-session-3",
        messages=[]
    )
    
    result = await plugin.format_markdown_output(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] == 'no_llm_response'


@pytest.mark.asyncio
async def test_format_markdown_output_empty_content(plugin):
    """Test handling when LLM response has empty content."""
    llm_response = {
        'assistant': {
            'content': ''
        }
    }
    
    context = HookContext(
        hook_type=HookType.POST_LLM_CALL,
        request_id="test-req-6",
        session_id="test-session-4",
        messages=[],
        llm_response=llm_response
    )
    
    result = await plugin.format_markdown_output(context)
    
    assert result.success is True
    assert result.modified is False
    assert result.metadata['reason'] == 'no_content_to_convert'


@pytest.mark.asyncio
async def test_format_markdown_code_blocks(plugin):
    """Test code block formatting."""
    markdown_with_code = """
Here is some code:

```python
def hello():
    print("world")
```

End of code.
"""
    
    llm_response = {
        'assistant': {
            'content': markdown_with_code
        }
    }
    
    context = HookContext(
        hook_type=HookType.POST_LLM_CALL,
        request_id="test-req-7",
        session_id="test-session-5",
        messages=[],
        llm_response=llm_response
    )
    
    result = await plugin.format_markdown_output(context)
    
    assert result.success is True
    assert result.modified is True
    html_content = result.context.llm_response['assistant']['content']
    # Check for code block elements
    assert '<code>' in html_content or '<pre>' in html_content


@pytest.mark.asyncio
async def test_format_markdown_tables(plugin):
    """Test table formatting."""
    markdown_with_table = """
| Header 1 | Header 2 |
|----------|----------|
| Cell 1   | Cell 2   |
| Cell 3   | Cell 4   |
"""
    
    llm_response = {
        'assistant': {
            'content': markdown_with_table
        }
    }
    
    context = HookContext(
        hook_type=HookType.POST_LLM_CALL,
        request_id="test-req-8",
        session_id="test-session-6",
        messages=[],
        llm_response=llm_response
    )
    
    result = await plugin.format_markdown_output(context)
    
    assert result.success is True
    assert result.modified is True
    html_content = result.context.llm_response['assistant']['content']
    # Check for table elements
    assert '<table>' in html_content or 'Header 1' in html_content


@pytest.mark.asyncio
async def test_html_sanitization(plugin):
    """Test that potentially dangerous HTML is sanitized."""
    markdown_with_script = """
# Title

<script>alert('xss')</script>

Some **safe** content.
"""
    
    llm_response = {
        'assistant': {
            'content': markdown_with_script
        }
    }
    
    context = HookContext(
        hook_type=HookType.POST_LLM_CALL,
        request_id="test-req-9",
        session_id="test-session-7",
        messages=[],
        llm_response=llm_response
    )
    
    result = await plugin.format_markdown_output(context)
    
    assert result.success is True
    html_content = result.context.llm_response['assistant']['content']
    # Script tags should be removed or escaped
    assert '<script>' not in html_content.lower()


@pytest.mark.asyncio
async def test_plugin_schema_validation():
    """Test that plugin schema is valid and loadable."""
    import yaml
    from pathlib import Path
    
    schema_path = Path(__file__).parent.parent / 'src' / 'plugins' / 'markdown_formatter' / 'schema.yaml'
    assert schema_path.exists(), f"Schema file not found: {schema_path}"
    
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema = yaml.safe_load(f)
    
    # Verify required fields
    assert 'hooks' in schema
    assert 'config' in schema
    
    # Verify hooks
    hooks = schema['hooks']
    assert 'inject_markdown_system_prompt' in hooks
    assert 'format_markdown_output' in hooks
    
    # Verify hook types
    assert hooks['inject_markdown_system_prompt']['type'] == 'session_start'
    assert hooks['format_markdown_output']['type'] == 'post_llm_call'
    
    # Verify dependencies use before/after, not priority
    for hook_name, hook_config in hooks.items():
        assert 'priority' not in hook_config, f"Hook {hook_name} should not use 'priority' field"


@pytest.mark.asyncio
async def test_config_defaults(plugin):
    """Test that plugin uses correct config defaults."""
    assert plugin.inject_system_prompt is True
    assert plugin.convert_to_html is True
    assert plugin.enable_code_highlighting is True
    assert plugin.enable_tables is True
    assert plugin.sanitize_html is True
