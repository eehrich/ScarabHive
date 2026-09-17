# Basic Agent

The plainest agent the system has: take a task, run the LLM loop with every
tool the agent is allowed to use, return the answer. It is also the reference
implementation for `SchemaBasedAgent` — new agent plugins are easiest to start
by reading these ~200 lines.

## What it provides

| Tool | Purpose |
|---|---|
| `basic_agent_execute_task` | Run one task through the LLM loop |
| `basic_agent_list_available_tools` | Names + descriptions of the tools this agent may call |

`type = ["tool-server"]`, no pip dependencies. The factory is one line —
`make_agent_plugin_factory(BasicAgent)` — because the boilerplate lives in
`agent_system.plugins.factory_utils`.

## Method routing

There is no dispatch table. `SchemaBasedAgent` maps a tool name to a method by
stripping the instance name:

```
basic_agent_execute_task          → execute_task(params)
basic_agent_list_available_tools  → list_available_tools(params)
```

So the tool name in `schema.yaml` and the method name in `server.py` must stay
in sync — a renamed tool with no matching method fails at call time, not at
startup.

## Choosing the model per call

Both knobs are runtime tool arguments, and they do **not** do the same thing:

* `llm_profile` — an explicit profile name. Validated twice: it must be in the
  agent's `available_llm_profiles` *and* in `llm_system.profiles`. Built with
  `create_llm_from_profile`, never by hand-listing factory arguments — that
  path used to drop `thinking_level`, `max_tokens`, `safety_settings`,
  `service_tier` and `provider_routing` silently.
* `use_advanced_model` — a flag, passed through to `run_events` untouched. The
  advanced mapping happens centrally there, so the fallback chain matches the
  advanced chain. Mapping it here would produce a fallback order for the wrong
  profile.

`llm_profile` wins when both are set — the flag is then forwarded as `False`,
so the explicit profile is not overridden downstream.
`schema.yaml` renders both conditionally: the `llm_profile` enum only appears
when the agent has more than one profile, and `use_advanced_model` documents
itself as ineffective when no advanced profile is configured.

## Configuration

```yaml
basic_agent:
  type: basic_agent
  enabled: true
  agent_config:
    # [0] is the default, the rest is the fallback order. All entries stay
    # selectable as the llm_profile tool parameter.
    llm_profile: [think, normal, turbo, or-deepseek-flash, chat]
    llm_profile_advanced: []
```

An empty `llm_profile_advanced` is a decision, not an omission: when the
default is already the strongest profile, `use_advanced_model` would be a
downgrade.

## Result shape

The answer comes from the `final` event's `summary` (falling back to
`message`). `tool_calls` collects every `tool_call` with the framework's
underscore-prefixed params filtered out, and `steps` counts loop iterations.
Errors are returned as `{"status": "error", ...}` — the tool does not raise.

## Tests

`tests/test_plugin_basic_agent_basic.py`,
`test_plugin_basic_agent_integration.py`,
`test_plugin_basic_agent_llm_profiles.py`.

## License

Apache-2.0 — see `LICENSE`.
