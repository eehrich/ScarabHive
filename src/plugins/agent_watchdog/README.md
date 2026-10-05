# agent_watchdog

Watches a running agent. Every few steps -- and, if wanted, every so many characters of thinking inside one long
call -- a separate, cheap model reads a bounded excerpt of the run and judges whether the recent stretch made a
decision. The verdict (`continue`, `steer`, `abort`) goes to a JSONL log and a status line in the chat. It only
watches: nothing is told to the agent, nothing is stopped, the agent's history and prompt cache are untouched.

- **Hooks:** `observe_step` (`post_llm_call`), `remember_task` (`pre_llm_call`) and `observe_reasoning`
  (`llm_progress`), all off by default; an agent switches them on in its `hooks.overrides`.
- **Tools:** none. **Panel:** none.

Enable it in `config/plugins.yaml`:

```yaml
plugins:
  servers:
    agent_watchdog:
      type: agent_watchdog
      enabled: true
      config:
        llm_profile: "or-deepseek-flash"
```

and per agent:

```yaml
agent_config:
  hooks:
    overrides:
      agent_watchdog.observe_step:
        enabled: true
```

The full manual -- the hooks and their settings, what the judge sees, how its answer is read, the log fields and
what it costs -- is `agent_watchdog.guide` in the Help panel.
