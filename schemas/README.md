# JSON schemas for the ScarabHive configuration

VS Code validates the config files live against these schemas
(`.vscode/settings.json` → `yaml.schemas`): autocomplete, typos and dead keys
show up right in the editor.

## Derived schemas (do not edit by hand)

| Schema | Validates | Source (Pydantic) |
|---|---|---|
| `llm-config.schema.json` | `config/llm.yaml` | `LLMSystemConfig` |
| `main-config.schema.json` | `config/config.yaml` (before the include merge) | `AgentSystemConfig` |
| `plugins-config.schema.json` | `config/plugins.yaml` | `PluginsConfig` + `GlobalHooksConfig` |
| `config-part.schema.json` | every file pulled in via `includes:`: `config/agents/*.yaml`, `config/mcp_servers.yaml`, the `src/plugins*/*/agents/*.yaml` | `AgentSystemConfig`, limited to the four merged sections |

Why the agent files get a schema of their own: from an included file,
`settings.py` takes only `llm_system`, `plugins`, `external_servers` and
`hooks` -- everything else (such as `network:`) is silently dropped and would
be dead there, although the main schema allows it.

An empty key under `hooks:` (all lines below it commented out) means "nothing
set"; the models discard it before validation, so the schema allows `null`
there.

**Limit:** `ToolServerConfig` is `extra="allow"` (the plugin's own keys such
as `max_nesting_depth` or `allowed_agents` live there), so a typo directly
under a server entry goes unnoticed. Inside `agent_config:` the strictness
applies.

These four are **generated** -- the models in
`src/agent_system/config/models.py` are the only source. Regenerate them after
every model change:

```bash
.venv/Scripts/python.exe src/scripts/generate_config_schemas.py
```

The anti-drift test `tests/config/test_config_schemas.py` fails when a file
is out of date, when the real YAML no longer validates, or when the
strictness is lost.

**Strictness:** every object with declared fields carries
`additionalProperties: false`. The runtime ignores unknown keys (pydantic
`extra="ignore"`), so a dead config key would never show up there; the editor
is where it should be caught. Models with `extra="allow"` (e.g.
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
