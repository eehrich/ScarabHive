# BasicAgent LLM Profile Support

## Overview

The `basic_agent` now supports dynamic LLM profile switching via the `llm_profile` parameter in the `execute_task` tool. This allows parent agents to delegate tasks to more capable LLMs when needed.

## Usage

### Tool Parameters

```json
{
  "task": "Your task description",
  "llm_profile": "think"  // Optional: Override agent's default LLM profile
}
```

The `llm_profile` parameter is validated against the agent's configured profiles and rendered as an enum in the tool schema, making it easy for LLMs to see available options.

### Available Profiles

The available profiles are configured in the agent's `llm_profile` configuration in `config/plugins.yaml`. The profiles are defined as a list, where:
- **First element** = Default profile (used when no `llm_profile` parameter is provided)
- **Other elements** = Available options that can be specified in the tool call

Common profiles include:

- `turbo` - Fast, cost-effective model for simple tasks (gpt-5-nano)
- `normal` - Balanced model for standard tasks (gpt-5-mini)
- `think` - Advanced model for complex reasoning (gpt-5.1) - **Often used as default**
- `big` - Large context window model (gpt-4.1)
- `chat` - Fast non-streaming model (deepseek-chat)
- `code` - Specialized for coding tasks (gpt-5.1-codex)

### Configuration

#### Plugin Configuration (`config/plugins.yaml`)

The agent's LLM profiles are configured as a list in the `agent_config.llm_profile` field:

```yaml
basic_agent:
  type: basic_agent
  enabled: true
  agent_config:
    llm_profile: ["think", "normal", "turbo", "chat"]
    # First element ("think") is the default
    # Other elements are available as options in the tool
```

**Configuration Rules:**
- Can be a **string** (single profile, no choices): `llm_profile: "normal"`
- Can be a **list** (multiple profiles with default): `llm_profile: ["think", "normal", "turbo"]`
- First element in list = default profile (used when no `llm_profile` parameter provided)
- Other elements = available options (rendered in schema enum for LLM visibility)

#### LLM System Configuration (`config/llm.yaml`)

Profiles reference models defined in the `models` section:

```yaml
llm_system:
  models:
    gpt-5.1:
      provider: openai_httpx
      model: gpt-5.1
      context_window: 1000000
      # ... other model config

  profiles:
    think:
      model_ref: gpt-5.1
      description: "Advanced model for complex reasoning"
      max_steps: 100
```

#### Per-Agent LLM Parameter Overrides (`agent_config.llm_params`)

Agents can override LLM model parameters (e.g. `thinking_level`, `max_tokens`,
`service_tier`) on top of the referenced model config — instead of creating a
separate `llm_system.models` entry for every combination:

```yaml
my_agent:
  agent_config:
    llm_profile: [deepseek-chat, or-gpt-terra-unlimited]
    llm_params:
      thinking_level: low
      max_tokens: 8000
```

**Semantics:**
- Applied centrally in `resolve_llm_config_for_agent()` over the resolved
  model config — for **all** `llm_profile` models of this agent (default,
  `use_advanced_model`, auto-escalation). The shared `llm_system.models`
  entry is never mutated (a derived config is built per agent).
- **Not** applied to `llm_profile_fallbacks` (fallbacks are often a different
  provider and must run with their own robust tuning — e.g. Gemini rejects
  `thinking_level: max`) and not to explicit `--llm-profile` request overrides.
- Allowed keys: all `LLMModelConfig` fields **except** the identity fields
  `provider`, `model`, `api_key`, `base_url`, `batch_provider`, `ollama_mode`
  (those define WHICH model — that is what `llm_profile`/`llm.yaml` is for).
- Validated at config load (unknown keys and invalid values fail fast with a
  clear error, not at the first LLM call). Setting a key to `null` clears the
  base model's value.

## Implementation Details

### Configuration Model (`AgentConfig`)

The `AgentConfig` class in `src/agent_system/config/models.py` supports both string and list formats:

```python
class AgentConfig(BaseModel):
    llm_profile: str | List[str] = "normal"

    @property
    def default_llm_profile(self) -> str:
        """Get the default LLM profile (first in list if list)."""
        if isinstance(self.llm_profile, list):
            return self.llm_profile[0] if self.llm_profile else "normal"
        return self.llm_profile

    @property
    def available_llm_profiles(self) -> List[str]:
        """Get all available LLM profiles."""
        if isinstance(self.llm_profile, list):
            return self.llm_profile
        return [self.llm_profile]
```

### Schema Rendering

The `SchemaBasedAgent` class overrides `get_template_vars()` to inject available profiles:

```python
def get_template_vars(self) -> dict:
    vars = super().get_template_vars()
    if self.agent_config:
        vars['llm_profiles'] = self.agent_config.available_llm_profiles
    return vars
```

The `schema.yaml` uses Jinja2 to conditionally render the property:

```yaml
properties:
  task:
    type: string
    description: "The task or question to execute"
  {% if llm_profiles and llm_profiles|length > 1 %}llm_profile:
    type: string
    description: "Optional LLM profile to use..."
    enum: {{ llm_profiles | tojson }}
  {% endif %}
```

**Token Optimization**: The `llm_profile` property is only included when multiple profiles are configured. With a single profile (or string config), the property is omitted entirely, saving tokens.

**Examples:**

Multiple profiles → Property included:
```yaml
llm_profile: ["think", "normal", "turbo"]
# Renders: properties: {task: {...}, llm_profile: {...}}
```

Single profile → Property omitted:
```yaml
llm_profile: "think"
# Renders: properties: {task: {...}}
# (llm_profile not in schema = token savings!)
```

### Parameter Handling

1. The `llm_profile` parameter is extracted from the tool call parameters
2. Validated against `agent_config.available_llm_profiles` (agent-level validation)
3. Validated against `system_config.llm_system.profiles` (system-level validation)
4. A temporary `AgentConfig` is created with the requested profile
5. An LLM client override is created using `resolve_llm_config_for_agent()` and `make_llm()`
6. The override is passed to `run_events()` via the `llm_override` parameter

### Error Handling

If an invalid profile is specified, the tool returns an error with available profiles:

```json
{
  "status": "error",
  "error": "LLM profile 'invalid' not available for this agent. Available profiles: ['think', 'normal', 'turbo', 'chat']"
}
```

If the profile exists for the agent but not in the system configuration:

```json
{
  "status": "error",
  "error": "LLM profile 'missing' not found in system configuration. Available system profiles: ['turbo', 'normal', 'think', 'big', 'chat', 'code']"
}
```

### Status Updates

When using a custom profile, status updates include the profile information:

```
Using LLM profile: think:openai_httpx/gpt-5.1
```

## Auto-escalation when stuck

An agent can escalate itself from the standard to the advanced `llm_profile`
**mid-run** when the run loop objectively observes it is stuck — without the
agent having to admit it (weak models rarely do). Signals (no self-assessment):

- the tool-call **loop detector** fires (same call/sequence repeated), or
- **`escalate_error_streak`** consecutive steps whose tool calls ALL returned an
  error (catches near-loops the exact-match detector misses).

Escalation is time-boxed and budget-capped, not sticky-until-end: each trigger
opens a window of `escalate_rounds` steps on the advanced model, then the agent
drops back and only re-escalates on a fresh signal, until `escalate_max_calls`
advanced calls have been spent this run.

```yaml
agent_config:
  llm_profile: [normal, think]   # [standard, advanced]
  auto_escalate_on_stuck: true
  escalate_rounds: 2             # advanced steps per trigger
  escalate_max_calls: 6          # total advanced calls per run (budget)
  escalate_error_streak: 2       # trigger after N all-error tool steps
```

No-op unless `llm_profile` is a `[std, advanced]` list and the run isn't already
advanced (`use_advanced_model`). Each escalated step logs a warning and a status
line (`advanced — escalated: stuck`) — visible, never silent. Note this is
complementary to orchestrator escalation (`use_advanced_model` on a
`continue`): the orchestrator judges *between* runs, auto-escalation intervenes
*within* one.

## Example Use Cases

### Parent Agent Delegating Complex Task

```python
# Meta agent detects complex reasoning task
result = await basic_agent.execute_task({
    "task": "Analyze the philosophical implications of...",
    "llm_profile": "think"  # Use smarter model for complex reasoning
})
```

### Quick Fact Lookup

```python
# Simple factual query using fast model
result = await basic_agent.execute_task({
    "task": "What is the capital of France?",
    "llm_profile": "turbo"  # Fast, cost-effective model
})
```

### Default Behavior

```python
# No profile specified - uses agent's default (first in list)
result = await basic_agent.execute_task({
    "task": "Summarize this document..."
    # Uses "think" profile (first in ["think", "normal", "turbo", "chat"])
})
```

## Benefits

1. **Cost Optimization** - Use cheaper models for simple tasks, expensive models only when needed
2. **Performance Tuning** - Match model capabilities to task complexity
3. **Flexible Architecture** - Parent agents can make intelligent decisions about resource allocation
4. **Backward Compatible** - Existing code without `llm_profile` continues to work with defaults
5. **Schema Visibility** - LLMs can see available profiles via enum in tool schema
6. **Simple Configuration** - List-based config is more concise than mapping objects
7. **Token Optimization** - Single-profile configs omit the property entirely, saving context tokens

## Related Configuration Files

- `config/llm.yaml` - LLM model and profile definitions
- `config/plugins.yaml` - Plugin-specific profile mappings
- `src/plugins/basic_agent/schema.yaml` - Tool parameter definitions
- `src/plugins/basic_agent/server.py` - Implementation logic
