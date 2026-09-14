# JSON-Schemas für die AgentSystem-Konfiguration

VS Code validiert die Config-Dateien live gegen diese Schemas
(`.vscode/settings.json` → `yaml.schemas`): Autocomplete, Tippfehler und
tote Keys leuchten direkt im Editor auf.

## Abgeleitete Schemas (nicht von Hand editieren)

| Schema | validiert | Quelle (Pydantic) |
|---|---|---|
| `llm-config.schema.json` | `config/llm.yaml` | `LLMSystemConfig` |
| `main-config.schema.json` | `config/config.yaml` (vor Include-Merge) | `AgentSystemConfig` |
| `plugins-config.schema.json` | `config/plugins.yaml` | `PluginsConfig` + `GlobalHooksConfig` |
| `config-part.schema.json` | jede über `includes:` gezogene Datei: `config/agents/*.yaml`, `config/mcp_servers.yaml`, die ~85 `src/plugins*/*/agents/*.yaml` | `AgentSystemConfig`, auf die vier gemergten Sektionen beschränkt |

Warum die Agent-Dateien ein eigenes Schema bekommen: aus einer eingebundenen
Datei hebt `settings.py` nur `llm_system`, `plugins`, `external_servers` und
`hooks` heraus — alles andere (etwa `network:`) fällt still weg und wäre dort
tot, obwohl das Haupt-Schema es erlaubt.

Ein leerer Schlüssel unter `hooks:` (alle Zeilen darunter auskommentiert)
bedeutet „nichts gesetzt“; die Modelle verwerfen ihn vor der Validierung, und
das Schema lässt `null` dort deshalb zu.

**Grenze:** `MCPConfig` ist `extra="allow"` (die plugin-eigenen Keys wie
`max_nesting_depth` oder `allowed_agents` leben dort), deshalb bleibt ein
Tippfehler direkt unter einem Server-Eintrag unbemerkt. Innerhalb von
`agent_config:` greift die Strictness.

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
  `[plugin]`-Tabelle in `plugin.toml` (73 im Baum). Der Legacy-`plugin.yaml`
  ist seit `2181390d` (06.09.2026) restlos raus, auch aus
  `plugin_manifest.py`.
  Angewandt von `src/scripts/validate_plugin.py`; nirgends in
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
