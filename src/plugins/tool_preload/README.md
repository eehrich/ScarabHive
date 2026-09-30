# tool_preload

Makes an agent's predictable opening tool calls before the first LLM call of a
turn. A rule in the agent's YAML matches the user's message with a regular
expression; the tools it names run through the agent's own tool dispatch (same
allowlist as the model), and their results go into the conversation exactly as
if the model had called them -- one LLM round trip saved per call.

- **Hook:** `preload` (`pre_llm_call`, off by default; an agent switches it on
  and gives it rules in `hooks.overrides."tool_preload.preload"`).
- **Tools:** none. **Panel:** none.

Enable it in `config/plugins.yaml`, then per agent:

```yaml
agent_config:
  hooks:
    overrides:
      tool_preload.preload:
        enabled: true
        rules:
          - match: "document\\s+(?P<doc>[\\w-]+)"
            tool: notes_manage_json
            params: {operation: read, doc: "{doc}"}
```

The full manual -- rules and placeholders, chains, limits, failures, what the
model sees and the prompt-cache effect -- is `tool_preload.guide` in the Help
panel.
