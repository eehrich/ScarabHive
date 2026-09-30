# simple_prompt_inject

Puts one fixed piece of text in front of the model before every LLM call -- a
standing instruction or a reminder -- without editing any system prompt. The
text comes from the settings or a Markdown file, may use the agent's template
variables, and goes in as one message right behind the system prompt, just
before the last user message, or at the end -- or in front of the task,
inside the first user message. The shipped entry has it on for
every agent with a line against repeating itself, right behind the system
prompt.

- **Hook:** `inject_prompt` (`pre_llm_call`, on for every agent; an agent
  switches it off in `hooks.overrides`).
- **Tools:** none. **Panel:** none.

Enable it in `config/plugins.yaml`:

```yaml
plugins:
  servers:
    simple_prompt_inject:
      type: simple_prompt_inject
      enabled: true
      config:
        prompt_text: "Keep answers short."
        injection_position: "after_system"
```

The full manual -- positions and their prompt-cache cost, roles, template
variables, several texts, what the model sees -- is `simple_prompt_inject.guide`
in the Help panel.
