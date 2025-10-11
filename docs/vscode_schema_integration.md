# VS Code Schema Integration - Quick Start

## ✅ Already Configured!

Die JSON Schemas sind bereits in VS Code eingebunden. Du musst nichts manuell konfigurieren.

## Was funktioniert automatisch

### 1. Autocomplete / IntelliSense

Öffne eine Config-Datei und tippe los:

```yaml
# In config/llm.yaml
llm_system:
  models:
    my-model:
      provider: |  # ← Hier erscheint Autocomplete mit: openai, anthropic, deepseek, etc.
```

**Tastenkombination**: `Ctrl+Space` (erzwingt Autocomplete-Menü)

### 2. Hover-Dokumentation

Bewege die Maus über ein Feld:

```yaml
context_window: 128000  # ← Hover zeigt: "Maximum context window size in tokens"
```

### 3. Echtzeit-Validierung

Fehler werden sofort rot unterstrichen:

```yaml
llm_profile: "gpt4"  # ← Rot unterstrichen, wenn Pattern nicht stimmt
                      #   Pattern: ^[a-z][a-z0-9_-]*$
```

### 4. Enum-Validierung

Bei vordefinierten Werten zeigt VS Code die erlaubten Optionen:

```yaml
provider: |  # Autocomplete: openai, openai_httpx, anthropic, deepseek, custom
```

## Verfügbare Schemas

| Config-Datei | Schema | Was wird validiert |
|--------------|--------|-------------------|
| `config/llm.yaml` | `schemas/llm-config.schema.json` | LLM-Modelle, Provider, Timeouts, Capabilities |
| `config/config.yaml` | `schemas/mcp-config.schema.json` | System-Config, Auth, Logging, Network |
| `config/mcp.yaml` | `schemas/config-agents.schema.json` | Config-Based Agents (Epic 0043) |

## Beispiel: Neues LLM-Modell hinzufügen

1. Öffne `config/llm.yaml`
2. Gehe zu `llm_system.models`
3. Füge neuen Key hinzu:

```yaml
llm_system:
  models:
    my-custom-model:  # ← Start typing here
```

4. Drücke `Ctrl+Space` → VS Code schlägt alle Felder vor:
   - `provider` (required)
   - `model` (required)
   - `context_window`
   - `request_timeout`
   - `capabilities`
   - etc.

5. Bei `provider:` → Autocomplete zeigt erlaubte Werte

## Beispiel: Config-Based Agent erstellen

1. Öffne `config/mcp.yaml`
2. Gehe zu `mcp_system.config_agents`
3. Füge neuen Agent hinzu:

```yaml
mcp_system:
  config_agents:
    my_agent:  # ← Agent name (lowercase, alphanumeric + underscore)
      enabled: |  # Autocomplete: true/false
      description: ""  # Min 10 chars
      base_type: |  # Autocomplete: agent/server
      agent_config:
        llm_profile: |  # Pattern: ^[a-z][a-z0-9_-]*$
        max_steps: |  # Min: 1, Max: 100
```

## Troubleshooting

### Schema wird nicht aktiviert

1. **YAML Extension installiert?**
   - Extension: "YAML" von Red Hat
   - Install: `Ctrl+Shift+X` → Suche "YAML"

2. **Settings überprüfen:**
   ```bash
   # Öffne .vscode/settings.json
   code .vscode/settings.json
   ```
   
   Sollte enthalten:
   ```json
   {
     "yaml.schemas": {
       "./schemas/llm-config.schema.json": ["config/llm.yaml"],
       "./schemas/mcp-config.schema.json": ["config/config.yaml"]
     }
   }
   ```

3. **VS Code neu laden:**
   - `Ctrl+Shift+P` → "Developer: Reload Window"

### Schema-Fehler ignorieren

Wenn ein Schema einen Fehler meldet, der korrekt ist:

```yaml
# YAML-Kommentar mit Ignore-Marker
# yaml-language-server: $schema=
my_field: value  # Schema validation disabled for this file
```

## CLI Validation (Optional)

Für CI/CD oder manuelle Prüfung:

```bash
# Config-Based Agents validieren
python scripts/validate_config_agents_schema.py

# Mit Details
python scripts/validate_config_agents_schema.py --verbose

# Strict Mode (warnings = errors)
python scripts/validate_config_agents_schema.py --strict
```

## Schema-Dateien

Alle Schemas liegen in `schemas/`:

```
schemas/
├── config-agents.schema.json   # Config-based agents (mcp.yaml)
├── llm-config.schema.json       # LLM models (llm.yaml)
├── mcp-config.schema.json       # Main config (config.yaml)
└── README.md                    # Diese Anleitung
```

## Nächste Schritte

1. ✅ Schemas sind aktiviert
2. ✅ VS Code Extension installiert (YAML von Red Hat)
3. ✅ Öffne eine Config-Datei und teste Autocomplete
4. ✅ Hover über Felder für Dokumentation

**Fertig!** Viel Spaß beim Konfigurieren! 🚀
