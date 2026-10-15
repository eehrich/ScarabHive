# JSON schemas for the ScarabHive configuration

VS Code validates the config files live against these schemas
(`.vscode/settings.json` → `yaml.schemas`): autocomplete, typos and dead keys
show up right in the editor.

## Derived schemas (do not edit by hand)

| Schema | Validates | Source (Pydantic) |
|---|---|---|
| `llm-config.schema.json` | `config/llm.yaml`, `config/llm_openrouter.yaml` | `LLMSystemConfig` |
| `main-config.schema.json` | `config/config.yaml` (before the include merge) | `AgentSystemConfig` |
| `plugins-config.schema.json` | `config/plugins.yaml` | `PluginsConfig` + `GlobalHooksConfig` |
| `config-part.schema.json` | every file pulled in via `includes:`: `config/agents/*.yaml`, `config/mcp_servers.yaml`, the `src/plugins*/*/agents/*.yaml` | `AgentSystemConfig` without the master's own sections |

Why the agent files get a schema of their own: the loader merges every
section of an included file except the master's own -- `paths`, `auth`,
`includes` and `files` (`MASTER_ONLY_SECTIONS` in
`src/agent_system/config/layers.py`). Those it ignores with a warning (only
`auth`'s route rules are taken, from an include the master names by path such
as `config/security.yaml`), so in a part they would be dead although the main
schema allows them.

An empty key under `hooks:` (all lines below it commented out) means "nothing
set"; the models discard it before validation, so the schema allows `null`
there.

**Limit:** `ToolServerConfig` is `extra="allow"` (the plugin's own keys such
as `max_nesting_depth` or `allowed_agents` live there), so a typo directly
under a server entry goes unnoticed. Only `type`, which every entry needs, is
checked there: a misspelled `typ:` shows as the missing `type`. Inside
`agent_config:` the strictness applies.

These four are **generated** -- the models in
`src/agent_system/config/models/` are the only source. Regenerate them after
every model change, in the activated virtual environment:

```bash
python src/scripts/generate_config_schemas.py
```

The anti-drift test `tests/config/test_config_schemas.py` fails when a file
is out of date, when the real YAML no longer validates, or when the
strictness is lost.

**Strictness:** every object with declared fields carries
`additionalProperties: false`. Most models ignore unknown keys at runtime
(pydantic's default `extra="ignore"`), so a dead config key would never show up
there; the editor is where it should be caught. A few refuse them at runtime
too (`extra="forbid"`: `agent_config`, the LLM model entries, `hooks`,
`auth.registration`). Models with `extra="allow"` (e.g.
`ToolServerConfig`: plugin-specific keys) stay open.

## Hand-maintained schemas (no model behind them)

- **`plugin-config.schema.json`**: format of the plugin manifests -- the
  `[plugin]` table in `plugin.toml`. Applied by
  `src/scripts/validate_plugin.py`; not mapped in `.vscode/settings.json`, so
  it has no effect in the editor.
  ⚠️ `additionalProperties: false` -- a new manifest key must be added here,
  or the validator rejects the plugin.
- **`session-schema.json`**: documents the session JSON on disk. The
  `SessionManager` works with plain dicts and no Pydantic model -- the schema
  is documentation only and may be out of date.
