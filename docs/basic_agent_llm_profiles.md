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

- `turbo` - Fast, lightweight profile (gemini-3.5-flash-lite via OpenRouter)
- `normal` - Balanced model for standard tasks (DeepSeek V4 Flash via OpenRouter)
- `think` - Advanced model for complex reasoning (DeepSeek Flash latest via OpenRouter) - **Often used as default**
- `chat` - Cheap and fast model (DeepSeek V4 Flash via OpenRouter; `chat-nostream` is the non-streaming deepseek-chat)
- `code` - Specialized for coding tasks (Kimi K3 via OpenRouter)
- `structured` - Structured building work: workflows, configs, schemas (DeepSeek V4 Flash via OpenRouter)

### Configuration

#### Plugin Configuration (`config/plugins.yaml`)

The agent's LLM profiles are configured as **chains** (since 2026-07):

```yaml
basic_agent:
  type: basic_agent
  enabled: true
  agent_config:
    llm_profile: [think, normal, turbo]      # chain: [primary, fallback1, fallback2, ...]
    llm_profile_advanced: [chat]             # advanced chain (use_advanced_model=True)
```

**Configuration Rules (chain semantics):**
- `llm_profile`: **[primary, fallback1, fallback2, ...]** — position 0 is the
  default model; ALL further entries are fallbacks tried in order on rate
  limits / upstream errors — an LLM blocked for every agent (rate limit, quota,
  refused key) is skipped while a free one is left, see
  `docs/_arch_agent_architecture.md`, „LLM-Fallback und Sperren". A **string** means: primary only, no fallbacks.
- `llm_profile_advanced`: same structure for `use_advanced_model=True` —
  `[primary_adv, fallback1_adv, ...]`. Empty/missing = no advanced model
  (`use_advanced_model` is a no-op and runs the normal chain). The chains are
  each other's **final safety net (symmetric)**: when a run's own chain is
  exhausted, the retry loop continues with the complete other chain — a run
  never dead-ends while ANY chain still has a model (deduplicated, active
  primary excluded).
- The schema enum (selectable `llm_profile` tool parameter) exposes the union
  of both chains (`available_llm_profiles`).
- The legacy field `llm_profile_fallbacks` (old positional `[std, adv]`
  semantics) was removed and **fails loudly** at config load — migrate with
  `python scripts/migrate_llm_profiles.py`.

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
    llm_profile: [deepseek-chat]
    llm_profile_advanced: [or-gpt-full-unlimited]
    # Flat form: applies to every model of both chains (same as "*")
    llm_params:
      max_tokens: 8000
```

Different models know different parameter keys (a GPT `thinking_level` would
break on deepseek). For that, key the params **by profile name** — params
stick to the *model*, not the slot:

```yaml
    llm_params:
      "*":                          # optional: every model of both chains
        max_tokens: 8000
      or-gpt-full-unlimited:        # only when exactly this profile runs
        thinking_level: high
      deepseek-chat:
        include_thoughts: true
```

**Semantics:**
- Applied centrally in `resolve_llm_config_for_agent()` over the resolved
  model config — for every member of both chains (primary, `use_advanced_model`,
  auto-escalation and the fallbacks). The shared `llm_system.models`
  entry is never mutated (a derived config is built per agent).
- Keyed form resolves per profile as `merge("*", params[profile])` — the
  specific entry wins. Flat form behaves like a single `"*"` entry. The two
  forms are auto-detected (flat keys are `LLMModelConfig` field names);
  mixing them in one dict fails at config load.
- Valid profile keys: every entry of `llm_profile` and `llm_profile_advanced`, `"*"`.
  A key in neither chain (or a typo) would be a silent no-op and is
  dropped at load with a warning.
- **Type inheritance** (`type: <parent_server>`) deep-merges the parent's
  `agent_config` into the child, and `null` cannot clear inherited keys. Two
  traps are named at load with a fix hint: parent *flat* + child *keyed*
  → mixed-form error (fix: express the parent's flat params as `"*"` — same
  semantics); parent *keyed by profile* + child overriding the chains
  → stale profile key, dropped with a warning (fix: parent uses `"*"`, or move the keyed
  params down into the child).
- Also applied to fallback entries of the chains and to an explicit profile
  switch (`--llm`, API `llm_profile`, the tool's `llm_profile`): flat and `"*"`
  params reach every model, so key cross-provider values by profile (e.g. Gemini
  rejects `thinking_level: max`).
- Allowed keys inside a params dict: all `LLMModelConfig` fields **except**
  the identity fields `provider`, `model`, `api_key`, `base_url`,
  `batch_provider`, `ollama_mode`
  (those define WHICH model — that is what `llm_profile`/`llm.yaml` is for).
- Validated at config load (unknown keys and invalid values fail fast with a
  clear error, not at the first LLM call). Setting a key to `null` clears the
  base model's value.

## Implementation Details

### Configuration Model (`AgentConfig`)

The `AgentConfig` class in `src/agent_system/config/models.py` supports both string and list formats:

```python
class AgentConfig(BaseModel):
    llm_profile: str | List[str] = "normal"          # [primary, fallback1, ...]
    llm_profile_advanced: Optional[List[str]] = None  # [primary_adv, fallback1_adv, ...]

    # default_llm_profile        → llm_profile[0]
    # advanced_llm_profile       → llm_profile_advanced[0] or None
    # fallback_profiles          → llm_profile[1:]
    # fallback_chain(advanced)   → retry order (symmetric safety net):
    #                              normal:   llm_profile[1:] + full advanced chain
    #                              advanced: llm_profile_advanced[1:] + full normal chain
    # available_llm_profiles     → union of both chains (schema enum / validation)
```

### Schema Rendering

The `SchemaBasedAgent` class overrides `get_template_vars()` to inject available profiles:

```python
def get_template_vars(self) -> dict:
    vars = super().get_template_vars()
    if self.agent_config:
        vars['llm_profiles'] = self.agent_config.available_llm_profiles
        # True only for a REAL upgrade (advanced exists and differs from default)
        vars['has_advanced'] = bool(
            self.agent_config.advanced_llm_profile
            and self.agent_config.advanced_llm_profile != self.agent_config.default_llm_profile)
    return vars
```

The `schema.yaml` uses Jinja2 to conditionally render the properties:

```yaml
properties:
  task:
    type: string
    description: "The task or question to execute"
  {% if llm_profiles and llm_profiles|length > 1 %}llm_profile:
    type: string
    description: "Optional LLM profile to use..."
    enum: {{ llm_profiles | tojson }}
  {% endif %}{% if has_advanced %}use_advanced_model:
    type: boolean
    description: "Use the agent's advanced LLM profile..."
  {% endif %}
```

**Token Optimization**: The `llm_profile` property is only included when multiple profiles are configured. With a single profile (or string config), the property is omitted entirely, saving tokens. `use_advanced_model` is only advertised as an upgrade when an advanced chain exists (`has_advanced`); without one it stays in the schema (callers may always pass it) but its description says it has no effect.

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
4. `override_for_profile()` applies the agent's `llm_params` for the requested profile
5. An LLM client override is created using `create_llm_from_profile()` (resolver + provider registry)
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
  llm_profile: [normal]            # normal chain
  llm_profile_advanced: [think]    # escalation target = advanced chain primary
  auto_escalate_on_stuck: true
  escalate_rounds: 2             # advanced steps per trigger
  escalate_max_calls: 6          # total advanced calls per run (budget)
  escalate_error_streak: 2       # trigger after N all-error tool steps
```

No-op unless `llm_profile_advanced` has an entry (distinct from the default
profile) and the run isn't already advanced (`use_advanced_model`). Each escalated step logs a warning and a status
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
