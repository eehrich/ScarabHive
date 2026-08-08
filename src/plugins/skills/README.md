# skills plugin

Read access to **skill bundles** — packaged agent knowledge. See
`docs/skills_design.md` for the concept and `skills/README.md` for how to author
a skill.

A skill is a bundle: `SKILL.md` is the index/instructions, and files next to it
(`references/…`) carry depth that is loaded only when a task needs it. This
plugin is what lets an agent browse and read that bundle.

## Tools

| Tool | Purpose |
|---|---|
| `skills_list()` | All available skills: name, version, description, bundled files |
| `skills_list(name)` | One skill in detail |
| `skills_read(name)` | The skill's `SKILL.md` |
| `skills_read(name, path)` | A bundled file, e.g. `references/catalog.md` |

## Enabling it

```yaml
agent_config:
  tools:
    allowed:
      - "+skills/*"
```

Agents that only use `skills.always` (body merged into the system prompt) do
**not** need this plugin — it is for reading bundled reference material on
demand.

## Notes

* The registry lives in `agent_system.skills` (core), because the prompt
  renderer needs it for `always` skills; this plugin is the tool surface over
  it — the same split as `agent_system.hooks` vs. hook plugins.
* Reads are confined to the skill directory: no `..`, no absolute paths,
  symlinks resolved. Content is truncated at 100k characters so a single file
  cannot flood the context (`truncated: true` is reported).
* Discovery roots come from `skills.skill_dirs` in `config/config.yaml`
  (fallback: `skills/`, or `$AGENT_SKILL_DIRS`).
