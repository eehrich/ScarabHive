# JSON-Schemas für die AgentSystem-Konfiguration

VS Code validiert die Config-Dateien live gegen diese Schemas
(`.vscode/settings.json` → `yaml.schemas`): Autocomplete, Tippfehler und
tote Keys leuchten direkt im Editor auf.

## Abgeleitete Schemas (nicht von Hand editieren)

| Schema | validiert | Quelle (Pydantic) |
|---|---|---|
| `llm-config.schema.json` | `config/llm.yaml` | `LLMSystemConfig` |
| `main-config.schema.json` | `config/config.yaml` (vor Include-Merge) | `AgentSystemConfig` |
| `plugins-config.schema.json` | `config/plugins.yaml` | `PluginsConfig` + `hooks/config.py::HooksConfig` |

Diese drei werden **generiert** — die Modelle in
`src/agent_system/config/models.py` sind die einzige Quelle. Nach jeder
Modell-Änderung neu erzeugen:

```bash
.venv/Scripts/python.exe src/scripts/generate_config_schemas.py
```

Der Anti-Drift-Test `tests/config/test_config_schemas.py` wird rot, wenn
eine Datei veraltet ist, die echte YAML nicht mehr validiert oder die
Strictness verloren geht.

**Strictness:** Jedes Objekt mit deklarierten Feldern trägt
`additionalProperties: false`. Die Laufzeit ignoriert unbekannte Keys
(pydantic `extra="ignore"`) — genau deshalb überlebte die Klasse stiller
toter Config-Keys (`ollama_url`, `include_thinking`) monatelang; der Editor
ist der Ort, an dem sie auffallen sollen. Modelle mit `extra="allow"`
(z. B. `MCPConfig`: plugin-spezifische Keys) bleiben durchlässig.

## Handgepflegte Schemas (kein Modell dahinter)

- **`plugin-config.schema.json`**: Format der Plugin-Manifeste — der
  `[plugin]`-Tabelle in `plugin.toml` (73 im Baum) ebenso wie der Legacy-
  `plugin.yaml` (0 im Baum), die `plugin_manifest.py` nur noch als Fallback
  liest. Angewandt von `src/scripts/validate_plugin.py`; nirgends in
  `.vscode/settings.json` gemappt, im Editor wirkt es also nicht.
  ⚠️ `additionalProperties: false` — ein neuer Manifest-Schlüssel muss hier
  eingetragen werden, sonst weist der Validator das Plugin ab.
- **`session-schema.json`**: Dokumentiert das Session-JSON auf der Platte.
  Der `SessionManager` arbeitet dict-basiert ohne Pydantic-Modell — das
  Schema ist reine Dokumentation und kann veraltet sein.

## Historie

Bis 2026-08 waren alle Schemas handgeschrieben und weit gedriftet
(erfundene Felder, fehlende Provider). `mcp-config.schema.json` (validierte
irreführenderweise die Haupt-Config) und `hooks-config.schema.json`
(Teilmenge von `plugins.yaml`) sind in `main-config.schema.json` bzw. der
`hooks:`-Sektion von `plugins-config.schema.json` aufgegangen.
