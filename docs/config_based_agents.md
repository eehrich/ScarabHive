# Configuration-Based Agents

**Epic:** 0043 - Zero-Code Agent Definition  
**Status:** Implementation Complete (Documentation in Progress)

## Overview

Configuration-based agents allow you to create custom AI agents purely through YAML configuration, without writing any Python code. This is ideal for agents that differ primarily in:

- System prompts and instructions
- Tool access permissions (allow/block lists)
- LLM model selection
- Maximum reasoning steps
- Context management strategies

For agents requiring custom logic or advanced behaviors, use the [Plugin System](plugin_authoring.md) instead.

## Quick Start

### 1. Define Your Agent in `config/agents.yaml`

Add a new entry under the `agents` section:

```yaml
agents:
  my_financial_analyst:
    enabled: true
    description: "Professional financial analyst for stock market analysis"
    base_type: "basic_agent"
    agent_config:
      llm_profile: "turbo"
      max_steps: 20
      system_template: "config/prompts/financial_analyst_prompt.md"
      tools:
        allowed:
          - "yahoo_finance/*"
          - "web_scraper/*"
          - "duckduckgo_search/*"
          - "basic_operations/wait_for"
        blocked:
          - "ssh_control/*"
          - "script_interpreter/*"
      self_tool_descriptions:
        my_financial_analyst_execute_task: "Analyze stocks and market data using financial tools"
    metadata:
      author: "Your Name"
      version: "1.0.0"
      tags: ["finance", "analysis"]
      category: "financial"
      visibility: "both"  # Visible in UI and as tool
```

### 2. Create Your Prompt Template

Create the prompt file referenced in `system_template`:

**File:** `config/prompts/financial_analyst_prompt.md`

```markdown
You are a professional financial analyst with expertise in:
- Stock market analysis and trends
- Company financials and valuation
- Market research and data interpretation
- Risk assessment and investment strategies

Your analysis should be:
- Data-driven and factual
- Balanced and objective
- Clear and actionable
- Based on current market information

Available tools:
- Yahoo Finance for stock data
- Web scraping for research
- DuckDuckGo search for information gathering

Always cite your sources and provide a timestamp for data.
```

### 3. Validate and Use Your Agent

```bash
# Validate your configuration
python -m agent_system.agent_cli config-agents validate my_financial_analyst

# List all config agents
python -m agent_system.agent_cli config-agents list

# View detailed information
python -m agent_system.agent_cli config-agents show my_financial_analyst

# Use your agent
python -m agent_system.agent_cli run my_financial_analyst "Analyze AAPL stock performance"
```

## Configuration Reference

### Top-Level Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `enabled` | boolean | Yes | Whether this agent is active |
| `description` | string | Yes | Human-readable description |
| `base_type` | string | Yes | Base agent class: `"agent"` or `"server"` |
| `agent_config` | object | Yes | Agent configuration (see below) |
| `metadata` | object | No | Additional metadata (author, version, tags, etc.) |

### Agent Config Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `llm_profile` | list[string] | Yes | - | Profile CHAIN from `config/llm.yaml`: `[primary, fallback1, ...]` |
| `llm_profile_advanced` | list[string] | No | [] | Same chain shape for the advanced model |
| `fallback_recovery_seconds` | integer | No | 3600 | Seconds before retrying the primary after a fallback (1 hour default) |
| `max_steps` | integer | Yes | 20 | Maximum reasoning steps |
| `system_prompt` | string | No* | - | Inline system prompt text |
| `system_template` | string | No* | - | Path to prompt template file |
| `template_vars` | object | No | null | Custom variables for Jinja2 template rendering |
| `tools` | object | No | {} | Tool access control |
| `context_management` | object | No | defaults | Context management config |

\* Either `system_prompt` or `system_template` must be provided, but not both.

### LLM Profile Fallbacks

When the primary LLM profile hits rate limits (HTTP 429) or quota exhaustion, the agent automatically switches to fallback profiles in order:

```yaml
my_agent:
  type: basic_agent
  agent_config:
    llm_profile: ["gemini", "openai", "anthropic"]   # chain: primary, then fallbacks
    fallback_recovery_seconds: 1800                  # retry primary after 30min (default: 3600)
```

> `llm_profile_fallbacks` was **removed**. The positional `[standard, advanced]`
> reading is gone; a chain lives in `llm_profile` itself, and the advanced model
> gets its own chain in `llm_profile_advanced`. A config that still sets
> `llm_profile_fallbacks` is rejected at load with a migration hint rather than
> run with `llm_profile[1]` silently meaning something else.

**Behavior:**
1. Agent tries primary `llm_profile` first
2. On `LLMRateLimitError` or `LLMQuotaExhaustedError`, tries next fallback profile
3. Fallback becomes **persistent** - all subsequent requests use fallback LLM
4. After `fallback_recovery_seconds` elapsed, agent tries original profile again
5. If recovery succeeds, switches back to primary profile
6. If recovery fails, re-activates fallback for another recovery period
7. If all fallbacks exhausted, raises the original error

**Automatic Recovery:**
- **Default:** Retries original profile after 1 hour (3600 seconds)
- **Configurable:** Set `fallback_recovery_seconds` to custom value
- **Use Cases:**
  - Rate limits (TPM/RPM/RPD) - temporary, recovers automatically
  - Quota exhausted - persistent until daily/monthly reset
  - API outages - retries when service restored

**Status Updates:**
- Shows active profile: `gemini:fallback`, `openai:fallback`
- Recovery info: `"Switched to openai (rate limit hit, retry in 60min)"`
- Auto-recovery: `"Fallback recovery period elapsed. Trying original LLM again."`

**Use Cases:**
- Gemini free tier (250 requests/day) → OpenAI fallback
- Primary API down → Secondary provider
- Cost optimization (cheaper primary, expensive fallback)
- Rate limit management (temporary TPM/RPM limits)

### Tools Configuration

```yaml
tools:
  allowed:
    - "plugin_name/*"        # All tools from plugin
    - "plugin_name/tool_x"   # Specific tool
  blocked:
    - "dangerous_plugin/*"   # Block entire plugin
    - "plugin_name/tool_y"   # Block specific tool
```

**Access Logic:**
1. If `allowed` is empty, all tools are allowed by default
2. `blocked` takes precedence over `allowed`
3. Patterns support wildcards (`*`)

**List Merge Syntax for Inheritance:**

When an agent inherits from another agent via `type:`, lists are **replaced by default**. Use explicit prefixes to merge:

| Prefix | Behavior | Example |
|--------|----------|---------|
| `+item` | Append item to parent list | `+new_tool/*` |
| `!pattern` | Remove matching items from parent | `!old_tool/*` |
| `item` (no prefix) | Replace mode — the list replaces the parent's | `regular_tool/*` |

A list is either **merged** or a **replacement**, never both. Mixing prefixed and
unprefixed entries in one list raises a `ValueError` at config load, naming the
key and the offending entries:

```yaml
tools:
  allowed:
    - "linear_book/*"   # ERROR: '+' forgotten -- this reads as "replace"
    - "+v4_sam/*"       #        while this one reads as "add"
```

The mix is almost always a forgotten `+`, and it cannot be resolved by guessing:
read as "replace", only the prefixed entries survive; read as "merge", the bare
one silently joins the inherited list. Both are defensible, so the config has to
say which it means.

**Example:**

```yaml
# Parent agent
book_architect:
  type: writer_agent
  agent_config:
    tools:
      allowed:
        - "writer_content/*"
        - "w_sam/*"            # Parent uses standard sub-agent manager
        - "todo/*"

# Child agent inheriting from book_architect
book_architect_gemini_batch:
  type: book_architect         # Inherits from book_architect
  agent_config:
    tools:
      allowed:
        - "!w_sam/*"           # Remove parent's w_sam from allowed
        - "+w_sam_gemini/*"    # Add gemini-specific sub-agent manager
      blocked:
        - "+w_sam/*"           # Also block it explicitly
```

**Result:** The child agent will have:
- All parent tools EXCEPT `w_sam/*` (removed by `!`)
- Plus `w_sam_gemini/*` (added by `+`)
- `w_sam/*` in blocked list

**Without prefixes** (complete replacement):
```yaml
tools:
  allowed:
    - "only_this_tool/*"  # Replaces entire parent list
```

This syntax works for **any list** in the config, not just tools — including
`llm_profile`, `blocked`, `allowed_agents`, etc.

### self_tool_descriptions Configuration

Override descriptions for the agent's own tools (inherited from base_type):

```yaml
self_tool_descriptions:
  my_agent_execute_task: "Custom description for this agent's execute_task tool"
  my_agent_list_tools: "Custom description for this agent's list_tools tool"
```

**Note**: Tool names must start with the agent's name prefix (e.g., `my_agent_`).

### hooks Configuration

Configure the hook system for this agent:

```yaml
hooks:
  enabled: true  # Enable hooks for this agent
  disabled_hooks:
    - "request_logger.log_pre_llm"  # Disable specific hooks
  hook_overrides:
    "markdown_formatter.format_markdown_output":
      enabled: true
      timeout: 5.0
      config:
        convert_to_html: false
```

See [Plugin Hooks](plugin_hooks.md) for details.

## System Prompts

### Inline Prompts

For simple, short prompts:

```yaml
agent_config:
  system_prompt: |
    You are a helpful assistant specialized in customer support.
    Be friendly, professional, and efficient.
```

### Template Files

For complex, reusable prompts, put the prompt in a **markdown file**. The whole
file is the system prompt and is rendered with Jinja2 (`{{ current_date }}`,
`{{ tools }}`, `{{ max_steps }}`, etc.). (The old multi-section YAML format —
`system_prompt` / `tools_prompt` / `general_instructions_prompt` keys — has been
removed; use one markdown file.)

**HTML comments never reach the model.** `<!-- ... -->` in a template — and in
every `{% include %}` partial — is stripped before rendering, so it is the place
for notes to whoever edits the prompt (why a rule exists, which run it came
from). A comment that fills its line takes the line with it. Text that arrives
through a variable (a chapter, a document) is left as it is.

**config/prompts/my_prompt.md:**

```markdown
You are a {{ role }} with expertise in {{ domain }}.

Your responsibilities:
- Review code for bugs and security issues
- Suggest improvements and best practices

## Tools
Available Tools: {% if tools %}{{ tools | join(', ') }}{% else %}(none){% endif %}

## Context
- Current date: {{ current_date }}
- You have at most {{ max_steps }} steps
```

Reference in agent config (per-agent variables via `template_vars`):

```yaml
agent_config:
  system_template: "config/prompts/my_prompt.md"
  template_vars:
    role: "Code Reviewer"
    domain: "Python and TypeScript"
```

### Template Variables (Jinja2)

You can define custom variables directly in your agent configuration that are available for Jinja2 template rendering. This allows you to create reusable prompt templates with agent-specific values without writing Python code.

#### Defining Template Variables

Add `template_vars` to your `agent_config`:

```yaml
agents:
  my_agent:
    enabled: true
    description: "Agent with custom template variables"
    agent_config:
      llm_profile: "chat"
      system_prompt: |
        You are the {{ project_name }} assistant, version {{ version }}.
        Project author: {{ author }}
        {% if debug_mode %}Debug mode is enabled.{% endif %}
        
        Focus areas: {{ focus_areas | join(', ') }}
      
      template_vars:
        project_name: "AgentSystem"
        version: "2.0.0"
        author: "Development Team"
        debug_mode: false
        focus_areas:
          - "code quality"
          - "best practices"
          - "performance"
```

#### Available Variables

The following variables are automatically available in all templates:

| Variable | Description |
|----------|-------------|
| `tools` | List of available tool names |
| `max_steps` | Maximum reasoning steps configured |
| `current_step` | Current step number (1-indexed) — **changes every call, see below** |
| `current_date` | Current date (YYYY-MM-DD) |
| `current_time` | Current time (HH:MM:SS) — **changes every call, see below** |
| `current_datetime` | ISO format datetime — **changes every call, see below** |
| `current_timezone` | Configured timezone |
| `current_location` | Configured location |
| `current_weekday` | Day name (e.g., "Monday") |
| `current_month` | Month name (e.g., "January") |
| `current_year` | Year (e.g., 2025) |

Custom `template_vars` are merged with these built-in variables. **Custom variables take precedence** if there's a name conflict.

**Keep the system prompt stable.** It is re-rendered before every step and is the
start of the prompt the provider caches; a value that differs from one call to
the next re-bills the whole conversation behind it, on every call. So no
`current_step`, `current_time`, `current_datetime` or `unix_timestamp` in a
system prompt (`tests/config/test_prompts_have_no_ticking_clock.py` enforces
it). `current_date` changes once a day and is fine; an agent that needs the
exact time has the `datetime` tool.

#### Using with Template Files

Works with both inline `system_prompt` and `system_template` files:

**config/prompts/reusable_prompt.md:**
```markdown
# {{ project_name }} Agent

You are a specialized assistant for **{{ project_name }}**.

## Configuration
- Version: {{ version }}
- Author: {{ author }}

## Your Focus Areas
{% for area in focus_areas %}
- {{ area }}
{% endfor %}

## Current Context
Today is {{ current_weekday }}, {{ current_date }}.
You have at most {{ max_steps }} steps.
```

**config/agents/my_agent.yaml:**
```yaml
plugins:
  servers:
    my_custom_agent:
      type: basic_agent
      enabled: true
      agent_config:
        llm_profile: "chat"
        max_steps: 20
        system_template: "config/prompts/reusable_prompt.md"
        template_vars:
          project_name: "MyProject"
          version: "1.0.0"
          author: "My Team"
          focus_areas:
            - "feature development"
            - "bug fixing"
```

#### Complex Variable Types

`template_vars` supports nested objects and lists:

```yaml
template_vars:
  # Simple values
  name: "MyAgent"
  max_retries: 3
  
  # Nested objects
  config:
    debug: true
    verbosity: "high"
    features:
      streaming: true
      caching: false
  
  # Lists
  allowed_domains:
    - "example.com"
    - "api.example.com"
  
  # Mixed
  team:
    - name: "Alice"
      role: "Lead"
    - name: "Bob"
      role: "Developer"
```

Access in templates:
```
Config debug: {{ config.debug }}
First domain: {{ allowed_domains[0] }}
Team lead: {{ team[0].name }} ({{ team[0].role }})
```

## Session Context Variables (context_vars)

Session context variables allow you to pass runtime state into a session that persists across the session's lifetime. Unlike `template_vars` (which are static config values), `context_vars` are set when spawning sub-agents or starting sessions and can change between sessions.

### Setting Context Variables

Context variables are passed when spawning sub-agents via the sub-agent manager:

```yaml
# When creating a sub-agent, context_vars can be passed:
# {
#   "operation": "create",
#   "agent_type": "scene_writer",
#   "task": "Write chapter 1",
#   "context_vars": {
#     "workflow_phase": "content",
#     "book_id": "17",
#     "chapter_id": "1"
#   }
# }
```

### Accessing Context Variables

Context variables are automatically loaded into the agent's template variables and can be used in prompts:

```yaml
agents:
  scene_writer:
    agent_config:
      system_prompt: |
        You are writing for book {{ book_id }}, chapter {{ chapter_id }}.
        Current workflow phase: {{ workflow_phase }}
```

### Phase-Based Agent Filtering

A powerful use case for context variables is **phase-based filtering** in sub-agent managers. This restricts which agents are available based on the current workflow phase:

```yaml
# config/plugins.yaml
w_sam:
  type: sub_agent_manager
  enabled: true
  
  allowed_agents:
    - story_designer
    - story_reviewer
    - scene_writer
    - quality_meta_reviewer

  # Phase filtering uses session context_vars
  phase_filtering:
    enabled: true
    phase_variable: "workflow_phase"  # Which context var to read
    phase_agents:
      planning: [story_designer, story_reviewer]
      content: [scene_writer]
      review: [quality_meta_reviewer]
      _default: []  # Empty = all allowed_agents when phase unknown
```

When `phase_filtering` is enabled:
1. The sub-agent manager reads `workflow_phase` from the session's context_vars
2. Only agents matching the current phase are shown as available
3. Attempts to spawn non-phase agents are blocked with a helpful error

### Frontend Display

Sessions with context_vars display them in the UI:
- **Badges**: `workflow_phase` and `book_id` shown as colored badges
- **Info Panel**: Click the info button (ℹ️) to see all context variables and phase-allowed agents

## LLM Profiles

LLM profiles are defined in `config/llm.yaml`. Common profiles:

| Profile | Model | Use Case |
|---------|-------|----------|
| `turbo` | GPT-4 Turbo | Fast, efficient, cost-effective |
| `normal` | GPT-4 | Balanced performance |
| `deepseek` | DeepSeek Coder | Code-focused tasks |
| `o1-mini` | OpenAI O1 Mini | Complex reasoning |
| `o1` | OpenAI O1 | Advanced reasoning |

Create custom profiles in `config/llm.yaml`:

```yaml
llm_system:
  models:
    my_custom_profile:
      provider: "openai"
      model: "gpt-4-turbo-preview"
      temperature: 0.7
      max_tokens: 4000
      context_window: 128000
```

## Examples

### Example 1: Simple Q&A Agent

```yaml
agents:
  simple_qa:
    enabled: true
    description: "Lightweight Q&A agent for quick questions"
    base_type: "basic_agent"
    agent_config:
      llm_profile: "turbo"
      max_steps: 5
      system_prompt: |
        You are a helpful assistant for answering quick questions.
        Provide concise, accurate answers. If you don't know, say so.
    metadata:
      visibility: "ui"
```

### Example 2: Code Reviewer

```yaml
agents:
  code_reviewer:
    enabled: true
    description: "Expert code reviewer for pull requests"
    base_type: "basic_agent"
    agent_config:
      llm_profile: "deepseek"
      max_steps: 15
      system_template: "config/prompts/code_reviewer_prompt.md"
      tools:
        allowed:
          - "script_interpreter/*"
          - "basic_operations/*"
        blocked:
          - "ssh_control/*"
      hooks:
        enabled: true
        hook_overrides:
          "markdown_formatter.format_markdown_output":
            enabled: true
    metadata:
      author: "DevOps Team"
      version: "2.0.0"
      tags: ["code-review", "quality"]
      category: "development"
      visibility: "ui"
```

### Example 3: Research Assistant

```yaml
agents:
  research_assistant:
    enabled: true
    description: "Comprehensive research assistant with web access"
    base_type: "basic_agent"
    agent_config:
      llm_profile: "turbo"
      max_steps: 30
      system_template: "config/prompts/research_assistant_prompt.md"
      tools:
        allowed:
          - "duckduckgo_search/*"
          - "web_scraper/*"
          - "basic_operations/*"
        blocked:
          - "ssh_control/*"
          - "script_interpreter/*"
    metadata:
      category: "research"
      visibility: "both"
```
        strategy: "SUMMARIZE_OLDEST"
        preserve_recent_messages: 12
    metadata:
      author: "Research Team"
      version: "1.5.0"
      tags: ["research", "web", "analysis"]
      category: "research"
```

## CLI Commands

### List All Config Agents

```bash
python -m agent_system.agent_cli config-agents list
```

**Output:**
```
Config-Based Agents:
╭────────────────────┬──────────┬────────┬──────────┬────────────────────────────────────╮
│ NAME               │ LLM      │ STEPS  │ STATUS   │ DESCRIPTION                        │
├────────────────────┼──────────┼────────┼──────────┼────────────────────────────────────┤
│ financial_analyst  │ turbo    │ 20     │ Enabled  │ Professional financial analyst...  │
│ code_reviewer      │ deepseek │ 15     │ Enabled  │ Expert code reviewer...            │
│ simple_qa          │ turbo    │ 5      │ Enabled  │ Lightweight Q&A agent...           │
╰────────────────────┴──────────┴────────┴──────────┴────────────────────────────────────╯
```

**JSON Output:**
```bash
python -m agent_system.agent_cli config-agents list --format json
```

### Show Agent Details

```bash
python -m agent_system.agent_cli config-agents show financial_analyst
```

**Output:**
```
============================================================
Config Agent: financial_analyst
============================================================
Status:       Enabled
Description:  Professional financial analyst for stock market analysis

Base Type:    agent
LLM Profile:  turbo
Max Steps:    20
Template:     config/prompts/financial_analyst_prompt.md

Tools:
  Allowed:  yahoo_finance/*, web_scraper/*, duckduckgo_search/*, basic_operations/wait_for
  Blocked:  ssh_control/*, script_interpreter/*

Context Management:
  Enabled:   True
  Strategy:  SUMMARIZE_OLDEST
  Preserve:  8 messages

Metadata:
  author:   Your Name
  version:  1.0.0
  tags:     finance, analysis
  category: financial
```

### Validate Configuration

```bash
# Validate all config agents
python -m agent_system.agent_cli config-agents validate

# Validate specific agent
python -m agent_system.agent_cli config-agents validate financial_analyst
```

**Output:**
```
Config Agent Validation Summary:
  Total agents: 5
  Passed: 5
  Failed: 0

✓ All config agents passed validation
```

## API Endpoints

### List Config Agents

```http
GET /api/config-agents
```

**Response:**
```json
{
  "agents": [
    {
      "name": "financial_analyst",
      "enabled": true,
      "description": "Professional financial analyst...",
      "llm_profile": "turbo",
      "max_steps": 20,
      "metadata": {
        "author": "Your Name",
        "version": "1.0.0",
        "tags": ["finance", "analysis"]
      }
    }
  ]
}
```

### Get Agent Details

```http
GET /api/config-agents/{agent_name}
```

**Response:**
```json
{
  "name": "financial_analyst",
  "enabled": true,
  "description": "Professional financial analyst...",
  "base_type": "agent",
  "llm_profile": "turbo",
  "max_steps": 20,
  "system_template": "config/prompts/financial_analyst_prompt.md",
  "has_inline_prompt": false,
  "tools": {
    "allowed": ["yahoo_finance/*", "web_scraper/*"],
    "blocked": ["ssh_control/*", "script_interpreter/*"]
  },
  "context_management": {
    "enabled": true,
    "strategy": "SUMMARIZE_OLDEST",
    "preserve_recent_messages": 8
  },
  "metadata": {
    "author": "Your Name",
    "version": "1.0.0"
  }
}
```

### Validate Config Agents

```http
POST /api/config-agents/validate
Content-Type: application/json

{
  "agent_name": "financial_analyst"  // Optional: validate specific agent
}
```

**Response:**
```json
{
  "validation_results": {
    "financial_analyst": []  // Empty array = valid
  },
  "summary": {
    "total": 1,
    "passed": 1,
    "failed": 0
  }
}
```

## Best Practices

### 1. **Use Descriptive Names**
Choose clear, purpose-driven names:
- ✅ `financial_analyst`, `code_reviewer`, `customer_support`
- ❌ `agent1`, `my_agent`, `test`

### 2. **Set Appropriate Step Limits**
- Simple Q&A: 5-10 steps
- Analysis tasks: 15-25 steps
- Complex research: 25-40 steps
- Avoid >50 steps (risk of infinite loops)

### 3. **Be Specific in Prompts**
Define clear:
- Role and expertise
- Responsibilities and limitations
- Output format preferences
- Quality standards

### 4. **Use Tool Restrictions Carefully**
- **Allow minimal tools**: Only what's needed
- **Block dangerous tools**: SSH, script execution (unless required)
- **Test access**: Validate tool permissions work as expected

### 5. **Choose Right LLM Profile**
- **Turbo**: Most tasks, cost-effective
- **DeepSeek**: Code-heavy work
- **O1/O1-mini**: Complex reasoning, planning
- **Normal**: Balanced default

### 6. **Enable Context Management**
For long conversations:
- Enable context management
- Use `SUMMARIZE_OLDEST` for important history
- Set `preserve_recent_messages` appropriately (8-12 typical)

### 7. **Version Your Agents**
Use metadata to track versions:
```yaml
metadata:
  version: "2.1.0"
  changelog: "Added web scraping, improved prompt"
```

### 8. **Test Before Deploying**
```bash
# Validate configuration
python -m agent_system.agent_cli config-agents validate my_agent

# Test with simple query
python -m agent_system.agent_cli run my_agent "Test query"

# Check tool access
python -m agent_system.agent_cli config-agents show my_agent
```

## Troubleshooting

### Agent Not Appearing in List

**Problem:** Agent doesn't show up in `config-agents list`

**Solutions:**
1. Check `enabled: true` in config
2. Validate YAML syntax: `python -m agent_system.agent_cli config-agents validate`
3. Restart application to reload config
4. Check for duplicate names

### Validation Errors

**Problem:** `config-agents validate` reports errors

**Common Issues:**
```
❌ Missing system_prompt or system_template
✅ Add either inline prompt or template file reference

❌ System template file not found
✅ Check file path is relative to project root
✅ Ensure file exists and is readable

❌ Unknown llm_profile
✅ Check profile exists in config/llm.yaml
✅ Use standard profiles: turbo, normal, deepseek

❌ Invalid max_steps
✅ Must be positive integer (1-100 recommended)
```

### Agent Not Using Expected Tools

**Problem:** Agent can't access tools or has wrong permissions

**Solutions:**
1. Check `tools.allowed` includes needed plugins/tools
2. Verify `tools.blocked` doesn't prevent access
3. Confirm tools are actually available: `python -m agent_system.agent_cli plugins list`
4. Test with wildcards: `plugin_name/*` allows all tools from plugin

### Prompt Template Not Loading

**Problem:** Template file errors or not being used

**Solutions:**
1. Check file path relative to project root
2. Validate YAML syntax in template file
3. Don't use both `system_prompt` and `system_template`
4. Check file permissions (must be readable)

## Advanced Topics

### Custom Base Types

While `"agent"` is standard, you can use `"server"` for specialized behaviors:

```yaml
base_type: "server"  # Uses different base class
```

### Prompt Template Variables

If using Jinja2 templating (advanced):

**Template:**
```yaml
system_prompt: |
  You are {{ role }} specialized in {{ domain }}.
  Context: {{ context }}

variables:
  role: "Expert Analyst"
  domain: "Financial Markets"
  context: "Real-time trading environment"
```

### Dynamic Configuration

Config agents are discovered at bootstrap. To add/modify agents dynamically:

1. Edit `config/agents.yaml`
2. Restart application or reload config
3. Validate with `config-agents validate`

## Migration from Plugin-Based Agents

### When to Migrate to Config-Based

Migrate if your plugin agent:
- ✅ Has minimal custom logic
- ✅ Differs mainly in prompt/tools/LLM
- ✅ Doesn't need complex initialization
- ✅ Doesn't require custom methods

### When to Keep Plugin-Based

Keep plugin if your agent:
- ❌ Has complex business logic
- ❌ Requires custom tool implementations
- ❌ Needs stateful behavior
- ❌ Integrates with external systems
- ❌ Requires advanced error handling

### Migration Steps

1. **Extract configuration:**
   ```python
   # From plugin code:
   llm_profile = "turbo"
   max_steps = 20
   system_prompt = "..."
   ```

2. **Create config entry:**
   ```yaml
   config_agents:
     my_agent:
       enabled: true
       agent_config:
         llm_profile: "turbo"
         max_steps: 20
         system_prompt: "..."
   ```

3. **Test thoroughly:**
   ```bash
   python -m agent_system.agent_cli config-agents validate my_agent
   python -m agent_system.agent_cli run my_agent "Test query"
   ```

4. **Remove plugin code** once validated

## See Also

- [Plugin Authoring Guide](plugin_authoring.md) - For custom agents needing code
- [MCP Configuration](mcp_configuration.md) - MCP server configuration
- [Architecture Review](architecture_review_refactoring.md) - System architecture
- [Service Layer](service_layer_implementation.md) - Service layer details

## Support


For issues, questions, or contributions:
- Check troubleshooting section above
- Validate configuration: `config-agents validate`
- Review example agents in `config/agents.yaml`
- See Epic 0043 in `backlog.md` for development status

