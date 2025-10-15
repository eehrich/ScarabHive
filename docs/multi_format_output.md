# Multi-Format Output Architecture

This document describes the multi-format output system that allows the AgentSystem to render the same content differently based on the target interface (web, CLI, API).

## Overview

The system separates **storage format** from **display format**:

- **Storage**: All messages stored as Markdown in session files
- **Display**: Formatted on-demand based on interface requirements

This allows:
- Clean, human-readable session storage
- Same message rendered differently in web vs CLI
- Easy addition of new output formats
- No HTML/ANSI in stored messages

## Architecture Components

### 1. HookContext Extension

The `HookContext` dataclass includes an `output_format` field:

```python
@dataclass
class HookContext:
    # ... other fields ...
    output: str = ""
    output_format: str = "text"  # Target format: 'html', 'ansi', 'text', 'markdown'
```

### 2. Hook Execution

The `execute_format_output_hooks()` method accepts an `output_format` parameter:

```python
async def execute_format_output_hooks(
    self,
    content: str,
    output_format: str = "html"
) -> tuple[str, str]:
    """Execute FORMAT_OUTPUT hooks with target format.
    
    Args:
        content: The content to format (usually Markdown)
        output_format: Target format ('html', 'ansi', 'text', 'markdown')
        
    Returns:
        Tuple of (formatted_content, content_format)
    """
```

### 3. Format Output Hook

Plugins implement multi-format conversion:

```python
async def format_markdown_output(self, context: HookContext) -> HookResult:
    target_format = context.output_format or 'text'
    
    if target_format == 'html':
        # Convert to HTML with Prism.js syntax highlighting
        html = convert_markdown_to_html(context.output)
        return HookResult(
            success=True,
            modified=True,
            context=replace(context, output=html),
            metadata={'content_format': 'html'}
        )
    
    elif target_format == 'ansi':
        # Convert to ANSI colored terminal output
        ansi = convert_markdown_to_ansi(context.output)
        return HookResult(
            success=True,
            modified=True,
            context=replace(context, output=ansi),
            metadata={'content_format': 'ansi'}
        )
    
    else:
        # Return plain text/markdown unchanged
        return HookResult(success=True, modified=False, context=context)
```

## Supported Formats

### HTML (`output_format='html'`)

**Target**: Web frontend  
**Implementation**: Python `markdown` library with `fenced_code` extension  
**Features**:
- Syntax highlighting with Prism.js-compatible classes
- Tables, autolinks, rich formatting
- XSS sanitization
- Code blocks: `<code class="language-python">...</code>`

**Example**:
```markdown
# Header
```python
def hello():
    print("world")
```
```

Becomes:
```html
<h1>Header</h1>
<pre><code class="language-python">def hello():
    print("world")
</code></pre>
```

### ANSI (`output_format='ansi'`)

**Target**: Terminal/CLI  
**Implementation**: Rich library  
**Features**:
- Colored text and syntax highlighting
- Bold, italic, underline formatting
- Colored headers and lists
- Graceful fallback if Rich unavailable

**Example**:
```markdown
# Header
**Bold** text
```

Becomes:
```
[1m[38;2;139;233;253mHeader[0m
[1mBold[0m text
```

### Text (`output_format='text'` or `'markdown'`)

**Target**: Storage, direct API access  
**Implementation**: No conversion  
**Features**:
- Original Markdown preserved
- Human-readable
- Used for session storage

## Integration Points

### Web API (interface_api.py)

```python
# In _run_events() method
formatted_output, content_format = await self.hook_manager.execute_format_output_hooks(
    content=llm_response,
    output_format='html'  # Request HTML for web frontend
)

yield {
    "event": "assistant_message",
    "content": formatted_output,
    "content_format": content_format  # 'html'
}
```

### CLI (Future Implementation)

```python
# In CLI output handler
formatted_output, content_format = await hook_manager.execute_format_output_hooks(
    content=message.content,
    output_format='ansi'  # Request ANSI for terminal
)

print(formatted_output)  # Colored terminal output
```

### Session Storage

```python
# Messages always stored as Markdown
message = ChatMessage(
    role='assistant',
    content="# Response\n\n```python\nprint('hello')\n```"
)
session.messages.append(message)
session.save()  # Markdown stored, not HTML or ANSI
```

## Message Flow

### 1. LLM Response
```
LLM generates Markdown:
"# Result\n\n```python\nprint('hello')\n```"
```

### 2. Storage
```python
# Stored unchanged as Markdown in session file
ChatMessage(role='assistant', content="# Result\n\n```python...")
```

### 3. Display - Web Frontend
```python
# API calls with output_format='html'
formatted_output, content_format = execute_format_output_hooks(
    content="# Result\n\n```python...",
    output_format='html'
)

# Returns:
# formatted_output = "<h1>Result</h1><pre><code class='language-python'>..."
# content_format = 'html'
```

### 4. Display - Terminal
```python
# CLI calls with output_format='ansi'
formatted_output, content_format = execute_format_output_hooks(
    content="# Result\n\n```python...",
    output_format='ansi'
)

# Returns:
# formatted_output = "\x1b[1m\x1b[38;2;139;233;253mResult\x1b[0m\n\n..."
# content_format = 'ansi'
```

## Frontend Integration

### Web (templates/index.html)

```html
<!-- Prism.js for syntax highlighting -->
<link href="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/prism.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/prism-python.min.js"></script>
<!-- More language components... -->
```

```javascript
// static/js/chat_module.js
function displayMessage(content, format) {
    const messageDiv = document.createElement('div');
    
    if (format === 'html') {
        messageDiv.innerHTML = content;  // HTML with <code class="language-*">
        Prism.highlightAllUnder(messageDiv);  // Apply syntax highlighting
    } else {
        messageDiv.textContent = content;  // Plain text fallback
    }
    
    chatContainer.appendChild(messageDiv);
}
```

### Terminal (Future CLI)

```python
# CLI handler
from rich.console import Console

console = Console()
console.print(formatted_output)  # Rich automatically interprets ANSI codes
```

## Plugin Development

### Adding Format Support

To add a new format (e.g., `'json'`, `'rtf'`):

1. **Update hook implementation**:
```python
async def format_output(self, context: HookContext) -> HookResult:
    target_format = context.output_format or 'text'
    
    if target_format == 'json':
        # Convert to JSON structure
        json_output = convert_to_json(context.output)
        return HookResult(
            success=True,
            modified=True,
            context=replace(context, output=json_output),
            metadata={'content_format': 'json'}
        )
```

2. **Update caller**:
```python
formatted_output, content_format = await execute_format_output_hooks(
    content=message.content,
    output_format='json'  # New format
)
```

3. **Add tests**:
```python
async def test_format_to_json(formatter):
    context = HookContext(
        hook_type=HookType.FORMAT_OUTPUT,
        output="# Test",
        output_format='json'
    )
    result = await formatter.format_output(context)
    assert result.metadata['content_format'] == 'json'
```

## Benefits

### Clean Separation
- Storage never contains HTML/ANSI escape codes
- Messages human-readable in session files
- Easy to migrate/export sessions

### Interface Flexibility
- Same content optimized for each interface
- Web: Rich HTML with syntax highlighting
- CLI: Colored terminal output
- API: Raw Markdown for external processing

### Extensibility
- New formats easily added
- Plugins can handle multiple formats
- No changes to core message storage

### Performance
- Format conversion on-demand only
- No unnecessary conversions during storage
- Efficient streaming to frontend

## Configuration

### Plugin Schema (schema.yaml)

```yaml
hooks:
  - type: format_output
    function: format_markdown_output
    priority: 100
    description: "Convert Markdown to HTML/ANSI/text based on output_format"

config:
  enable_code_highlighting:
    type: boolean
    default: true
    
  sanitize_html:
    type: boolean
    default: true
```

### Server Configuration (server.py)

```python
# Always specify output_format when calling format hooks
formatted_output, content_format = await self.hook_manager.execute_format_output_hooks(
    content=llm_response,
    output_format='html'  # Web API always uses HTML
)
```

## Troubleshooting

### HTML Appears Escaped in Browser

**Cause**: Format output hooks not executed, or `output_format` not set to `'html'`

**Fix**:
1. Check plugin enabled in `config/plugins.yaml`
2. Verify hook type is `format_output`, not `post_llm_call`
3. Ensure `execute_format_output_hooks()` called with `output_format='html'`

### ANSI Codes Visible in Terminal

**Cause**: Terminal doesn't support ANSI, or Rich library not installed

**Fix**:
1. Install Rich: `pip install rich>=13.0.0`
2. Use terminal supporting ANSI (most modern terminals)
3. Check plugin gracefully falls back to plain text

### Syntax Highlighting Not Working

**Web**:
1. Check browser console for Prism.js errors
2. Verify HTML has `<code class="language-*">` structure
3. Ensure Prism components loaded in correct order

**Terminal**:
1. Verify Rich library installed
2. Check terminal supports 256 colors: `echo $TERM`
3. Test with: `python -c "from rich.console import Console; Console().print('[bold red]Test[/]')"`

## Future Enhancements

- **PDF Export**: `output_format='pdf'` for generating PDF reports
- **RTF Format**: `output_format='rtf'` for rich text documents
- **LaTeX**: `output_format='latex'` for academic papers
- **Custom Themes**: User-selectable color schemes for ANSI/HTML
- **Format Negotiation**: Auto-detect best format based on client capabilities

## References

- [Markdown Library Documentation](https://python-markdown.github.io/)
- [Rich Library Documentation](https://rich.readthedocs.io/)
- [Prism.js Documentation](https://prismjs.com/)
- [Plugin Authoring Guide](plugin_authoring.md)
- [Hook Types Reference](plugin_hooks.md)
