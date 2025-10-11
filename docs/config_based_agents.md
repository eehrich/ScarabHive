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

### 1. Define Your Agent in `config/mcp.yaml`

Add a new entry under the `config_agents` section:

```yaml
config_agents:
  my_financial_analyst:
    enabled: true
    description: "Professional financial analyst for stock market analysis"
    base_type: "agent"
    agent_config:
      llm_profile: "turbo"
      max_steps: 20
      system_template: "config/prompts/financial_analyst_prompt.yaml"
      tools:
        allowed:
          - "yahoo_finance/*"
          - "web_scraper/*"
          - "duckduckgo_search/*"
          - "basic_operations/wait_for"
        blocked:
          - "ssh_control/*"
          - "script_interpreter/*"
      context_management:
        enabled: true
        strategy: "SUMMARIZE_OLDEST"
        preserve_recent_messages: 8
    metadata:
      author: "Your Name"
      version: "1.0.0"
      tags: ["finance", "analysis"]
      category: "financial"
```

### 2. Create Your Prompt Template

Create the prompt file referenced in `system_template`:

**File:** `config/prompts/financial_analyst_prompt.yaml`

```yaml
system_prompt: |
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
  
  Always cite your sources and provide timestamp for data.
```

### 3. Validate and Use Your Agent

```bash
# Validate your configuration
python -m agent_system.cli config-agents validate my_financial_analyst

# List all config agents
python -m agent_system.cli config-agents list

# View detailed information
python -m agent_system.cli config-agents show my_financial_analyst

# Use your agent
python -m agent_system.cli run my_financial_analyst "Analyze AAPL stock performance"
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
| `llm_profile` | string | Yes | - | LLM profile from `config/llm.yaml` |
| `max_steps` | integer | Yes | 20 | Maximum reasoning steps |
| `system_prompt` | string | No* | - | Inline system prompt text |
| `system_template` | string | No* | - | Path to prompt template file |
| `tools` | object | No | {} | Tool access control |
| `context_management` | object | No | defaults | Context management config |

\* Either `system_prompt` or `system_template` must be provided, but not both.

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

### Context Management Configuration

```yaml
context_management:
  enabled: true                    # Enable context management
  strategy: "SUMMARIZE_OLDEST"     # Strategy: SUMMARIZE_OLDEST, TRUNCATE, SLIDING_WINDOW
  preserve_recent_messages: 8      # Number of recent messages to keep intact
```

**Available Strategies:**
- `SUMMARIZE_OLDEST`: Summarize old messages when context limit reached
- `TRUNCATE`: Remove oldest messages
- `SLIDING_WINDOW`: Keep fixed window of recent messages

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

For complex, reusable prompts:

**config/prompts/my_prompt.yaml:**

```yaml
system_prompt: |
  You are a {role} with expertise in {domain}.
  
  Your responsibilities:
  - {responsibility_1}
  - {responsibility_2}
  
  Guidelines:
  - {guideline_1}
  - {guideline_2}

# Optional: Template variables (if using Jinja2 templating)
variables:
  role: "Code Reviewer"
  domain: "Python and TypeScript"
  responsibility_1: "Review code for bugs and security issues"
  responsibility_2: "Suggest improvements and best practices"
  guideline_1: "Be constructive and specific"
  guideline_2: "Provide code examples when helpful"
```

Reference in agent config:

```yaml
agent_config:
  system_template: "config/prompts/my_prompt.yaml"
```

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
config_agents:
  simple_qa:
    enabled: true
    description: "Lightweight Q&A agent for quick questions"
    base_type: "agent"
    agent_config:
      llm_profile: "turbo"
      max_steps: 5
      system_prompt: |
        You are a helpful assistant for answering quick questions.
        Provide concise, accurate answers. If you don't know, say so.
```

### Example 2: Code Reviewer

```yaml
config_agents:
  code_reviewer:
    enabled: true
    description: "Expert code reviewer for pull requests"
    base_type: "agent"
    agent_config:
      llm_profile: "deepseek"
      max_steps: 15
      system_template: "config/prompts/code_reviewer_prompt.yaml"
      tools:
        allowed:
          - "script_interpreter/*"
          - "basic_operations/*"
        blocked:
          - "ssh_control/*"
      context_management:
        enabled: true
        strategy: "SLIDING_WINDOW"
        preserve_recent_messages: 10
    metadata:
      author: "DevOps Team"
      version: "2.0.0"
      tags: ["code-review", "quality"]
```

### Example 3: Research Assistant

```yaml
config_agents:
  research_assistant:
    enabled: true
    description: "Comprehensive research assistant with web access"
    base_type: "agent"
    agent_config:
      llm_profile: "o1-mini"
      max_steps: 30
      system_template: "config/prompts/research_assistant_prompt.yaml"
      tools:
        allowed:
          - "duckduckgo_search/*"
          - "web_scraper/*"
          - "basic_operations/*"
        blocked:
          - "ssh_control/*"
          - "script_interpreter/*"
      context_management:
        enabled: true
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
python -m agent_system.cli config-agents list
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
python -m agent_system.cli config-agents list --format json
```

### Show Agent Details

```bash
python -m agent_system.cli config-agents show financial_analyst
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
Template:     config/prompts/financial_analyst_prompt.yaml

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
python -m agent_system.cli config-agents validate

# Validate specific agent
python -m agent_system.cli config-agents validate financial_analyst
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
  "system_template": "config/prompts/financial_analyst_prompt.yaml",
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
python -m agent_system.cli config-agents validate my_agent

# Test with simple query
python -m agent_system.cli run my_agent "Test query"

# Check tool access
python -m agent_system.cli config-agents show my_agent
```

## Troubleshooting

### Agent Not Appearing in List

**Problem:** Agent doesn't show up in `config-agents list`

**Solutions:**
1. Check `enabled: true` in config
2. Validate YAML syntax: `python -m agent_system.cli config-agents validate`
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
3. Confirm tools are actually available: `python -m agent_system.cli plugins list`
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

1. Edit `config/mcp.yaml`
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
   python -m agent_system.cli config-agents validate my_agent
   python -m agent_system.cli run my_agent "Test query"
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
- Review example agents in `config/mcp.yaml`
- See Epic 0043 in `backlog.md` for development status
