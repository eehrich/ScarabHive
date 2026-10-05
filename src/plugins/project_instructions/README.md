# project_instructions

Puts the project's `AGENTS.md` -- the instruction file Claude Code, Codex,
Gemini CLI and most other coding agents read -- in front of an agent that
switches it on. The file is read once per session from the directory the
agent's file tools work in and stands right behind the system prompt, the
same bytes on every call, so the prompt cache holds. The coder agents have it
on.

- **Hooks:** `inject_project_instructions` (off; an agent switches it on) puts
  the note in; `withdraw_project_instructions` (on everywhere, a no-op without
  a note) takes it out before compaction runs.
- **Tools:** none. **Panel:** none.

Enable it in an agent's YAML:

```yaml
agent_config:
  hooks:
    overrides:
      project_instructions.inject_project_instructions:
        enabled: true
```

A running session takes a new version of the file with
`/vars unset project_instructions`.

The full manual -- which file is read, links and size limits, what the model
sees, settings -- is `project_instructions.guide` in the Help panel.
