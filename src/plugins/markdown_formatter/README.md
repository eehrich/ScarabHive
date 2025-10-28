# Markdown Formatter Plugin

A multi-format output formatter that converts LLM responses from Markdown to various display formats.

## Features

- **Multi-Format Output**: Convert Markdown to HTML, ANSI, or plain text based on interface needs
- **System Prompt Injection**: Guides LLM to generate Markdown-formatted responses
- **Syntax Highlighting**:
  - HTML: Prism.js-compatible code blocks with `language-*` classes
  - ANSI: Rich library terminal colors and formatting
- **HTML Sanitization**: XSS protection with configurable allowed tags
- **Interface-Specific Formatting**:
  - Web API: HTML output with syntax highlighting
  - CLI: ANSI colored terminal output
  - Storage: Messages stored as plain Markdown

## Format Output Hook

The `format_output` hook converts Markdown content to the requested format:

```python
context = HookContext(
    hook_type=HookType.FORMAT_OUTPUT,
    output="# Hello\n\n```python\nprint('world')\n```",
    output_format='html'  # or 'ansi', 'text', 'markdown'
)
```

### Supported Formats

- **`html`**: Converts to HTML5 with Prism.js-compatible syntax highlighting
  - Code blocks: `<code class="language-python">...</code>`
  - Sanitized against XSS attacks
  - Tables, autolinks, and rich formatting

- **`ansi`**: Returns Markdown unchanged for Rich library rendering
  - Syntax-highlighted code blocks via Rich Markdown
  - Bold, italic, and other text formatting
  - Colored headers and lists
  - Falls back to plain text if Rich is unavailable

- **`text`** or **`markdown`**: Returns original Markdown unchanged
  - Used for storage and API responses
  - Preserves all Markdown formatting

## Pre LLM Call Hook

The `pre_llm_call` hook injects a system prompt to guide LLM output formatting:

```python
context = HookContext(
    hook_type=HookType.PRE_LLM_CALL,
    messages=[ChatMessage(role='user', content='Hello')]
)
```

The system prompt encourages the LLM to use Markdown formatting for better readability.

## Configuration

All settings in `schema.yaml`:

```yaml
config:
  inject_system_prompt:
    type: boolean
    default: true
    description: "Inject system prompt to guide LLM to use Markdown"

  system_prompt_template:
    type: string
    default: "Format your responses using Markdown for better readability."

  convert_to_html:
    type: boolean
    default: true
    description: "Enable Markdown to HTML conversion"

  enable_code_highlighting:
    type: boolean
    default: true
    description: "Enable syntax highlighting for code blocks"

  enable_tables:
    type: boolean
    default: true

  enable_autolinks:
    type: boolean
    default: true

  sanitize_html:
    type: boolean
    default: true
    description: "Sanitize HTML output to prevent XSS attacks"

  allowed_html_tags:
    type: array
    default: [h1, h2, h3, h4, h5, h6, p, br, strong, em, code, pre, ul, ol, li, table, thead, tbody, tr, th, td, a, blockquote, hr]
```

## Architecture

### Storage vs Display Separation

The plugin implements a clean separation between storage and display formats:

1. **Storage**: Messages stored as Markdown in session files
2. **Display**: Formatted on-demand based on interface:
   - Web API (`output_format='html'`): HTML with Prism.js highlighting
   - CLI (`output_format='ansi'`): Markdown rendered with Rich library
   - Direct access: Plain Markdown

### Hook Execution Flow

1. **Pre LLM Call** → `inject_markdown_system_prompt()`
   - Adds system prompt to guide LLM
   - Runs once per session
   - Modifies message list before LLM call

2. **LLM Response** → Stored as Markdown
   - No modification of LLM output
   - Preserved in session storage

3. **Display Request** → `format_markdown_output()`
   - Receives `output_format` parameter ('html', 'ansi', 'text')
   - Converts Markdown to requested format
   - Returns formatted content + content_format metadata

### Integration Points

**Web API** (`interface_api.py`):
```python
formatted_output, content_format = await hook_manager.execute_format_output_hooks(
    content=llm_response,
    output_format='html'  # Request HTML for web frontend
)
```

**CLI** (future):
```python
formatted_output, content_format = await hook_manager.execute_format_output_hooks(
    content=llm_response,
    output_format='ansi'  # Request Markdown for Rich rendering
)
```

## Dependencies

- **Required**:
  - `markdown>=3.5.0` - Markdown to HTML conversion

- **Optional**:
  - `rich>=13.0.0` - ANSI terminal formatting (graceful fallback if missing)

## Frontend Integration

The web frontend uses Prism.js for client-side syntax highlighting:

```html
<!-- Prism.js core + theme -->
<link href="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/prism.min.js"></script>

<!-- Language components (C must load before C++) -->
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/prism-python.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/prism-javascript.min.js"></script>
<!-- ... more languages ... -->
```

JavaScript rendering:
```javascript
messageDiv.innerHTML = content;  // HTML with <code class="language-python">
Prism.highlightAllUnder(messageDiv);  // Apply syntax highlighting
```

## Example Output

### Input (Markdown)
```markdown
# Example Code

Here's a Python function:

```python
def fibonacci(n):
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)
```

**Time complexity**: O(2^n)
```

### HTML Output
```html
<h1>Example Code</h1>
<p>Here's a Python function:</p>
<pre><code class="language-python">def fibonacci(n):
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)
</code></pre>
<p><strong>Time complexity</strong>: O(2^n)</p>
```

### ANSI Output
```
# Example Code

Here's a Python function:

```python
def fibonacci(n):
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)
```

**Time complexity**: O(2^n)
```
*(Rendered with Rich Markdown library for syntax highlighting and colors)*

## Testing

Run all tests:
```bash
pytest tests/test_plugin_markdown_formatter.py -v
```

Test coverage includes:
- System prompt injection (new sessions, existing system messages, duplicates)
- HTML conversion with Prism.js classes
- ANSI conversion with Rich (or fallback)
- Plain text passthrough
- XSS sanitization
- Multi-format support

## Troubleshooting

### Syntax Highlighting Not Working (Web)

1. Check browser console for Prism.js errors
2. Verify HTML has `<code class="language-*">` structure
3. Ensure Prism language components loaded in correct order (C before C++)
4. Check that `enable_code_highlighting: true` in config

### ANSI Colors Not Working (CLI)

1. Check if Rich library installed: `pip list | grep rich`
2. Verify terminal supports ANSI colors
3. Check hook execution with `output_format='ansi'` (returns Markdown for Rich rendering)
4. If Rich unavailable, plugin falls back to plain text

### HTML Appears Escaped

This indicates the `format_output` hook wasn't executed. Ensure:
1. Plugin enabled in `config/plugins.yaml`
2. Hook type is `format_output`, not `post_llm_call`
3. `execute_format_output_hooks()` called with `output_format='html'`

## Version History

- **v1.0**: Initial release with HTML conversion
- **v1.1**: Added multi-format support (HTML, ANSI, text)
- **v1.2**: Changed from `post_llm_call` to `format_output` hook type
- **v1.3**: Added ANSI terminal formatting with Rich library
