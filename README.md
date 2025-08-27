AgentSystem — Flexible MCP-based Agent Framework (Python)

AgentSystem is a lightweight, pluggable agent framework that uses the Model Context Protocol (MCP) architecture to integrate LLMs, search APIs, and specialized tool servers (e.g., web search, finance, weather, web scraping). The project aims to be runnable locally, test-driven, and accessible via both a CLI and a small FastAPI web UI.



## Features
- Platform: Python 3.11+ (PowerShell examples for Windows), cross-platform compatible
- Pluggable MCP servers configured via YAML (`config/agent.yaml`)
- **Sub-Agent Architecture**: Agents can use other agents as tools, enabling hierarchical architectures
- **Specialized Agents**: Pre-built agents for specific domains (WebResearchAgent, etc.)
- LLM adapters: Ollama, OpenAI (configurable)
- Built-in servers: web search, Yahoo Finance, weather, web scraping, Twitter, and more
- Interfaces: CLI and FastAPI HTTP API
- Test-first development: unit tests with pytest

## Quickstart (Windows PowerShell)
```powershell
# 1) Create and activate virtual environment
python -m venv .venv; . .venv/Scripts/Activate.ps1

# 2) Install the project (editable)
pip install -U pip; pip install -e .

# 3) Run tests
python -m pytest -q

# 4) Start the API (use a separate terminal)
agent-api

# or: CLI
agent-cli --help

Tip: you can disable colored output with the global flag `--no-color` (or force it with `--color always`).
```

## Configuration
Edit the master manifest `config/agent.yaml` which lists included YAML files to load:
```yaml
llm:
  provider: ollama   # ollama | openai
  model: gpt-oss:20b
  openai_api_key: ${OPENAI_API_KEY}
  ollama_url: http://127.0.0.1:11434  # set to remote Ollama instance if needed

mcp:
  enabled_servers:
  - google_search
  - duckduckgo_search
  - google_search
    - yahoo_finance
    - twitter_search
    - llm_router

servers:
  google_search:
    type: google_search
    api_key: ${GOOGLE_API_KEY}  # set to your Google API key (Custom Search JSON API)
    cx: ${GOOGLE_CX}            # set to your Custom Search Engine ID
  duckduckgo_search:
    type: duckduckgo_search
  yahoo_finance:
    type: yahoo_finance
  twitter_search:
    type: twitter_search
  llm_router:
    type: llm_router
    default_provider: ollama
  # Sub-agents enable hierarchical agent architectures
  helper_agent:
    type: sub_agent
    description: "A specialized helper agent"
  # Specialized agents for specific domains
  web_researcher:
    type: web_research_agent
    description: "Advanced web research with search and scraping"

network:
  ssl_verify: false  # set to false if your corporate network has untrusted SSL interception

logging:
  enabled: true
  level: INFO
  file: logs/agent.log
  as_json: false

prompts:
  system_template: config/prompts/system_prompt.yaml

Configuration manifest behavior:

- The file `config/agent.yaml` acts as a manifest and may list other YAML files to include via an `includes:` (or `files:`) key. Example:

```yaml
includes:
  - general.yaml
  - mcp.yaml
```

- CLI operations that modify MCP settings (enable/disable) will only write into included files (for example `mcp.yaml`) and will not overwrite the master manifest `config/agent.yaml`.
```

You can override values using environment variables. For OpenAI, set `llm.provider: openai` and provide `OPENAI_API_KEY`.

## Settings loader and dependency injection

- The project exposes `load_settings()` in `agent_system.config.settings` which loads `config/agent.yaml` (or the path from `AGENT_CONFIG_PATH`) and expands `${VAR}` placeholders using environment variables.
- `Agent` now accepts an optional `llm` or `llm_factory` parameter for dependency injection. This makes it easy to pass a mocked LLM in tests or wire a factory in bootstrap code.

Example (CLI/bootstrap will use `load_settings()` automatically):

```python
from agent_system.config.settings import load_settings
from agent_system.servers.agent.server import Agent

config = load_settings()
# Optionally inject pre-created llm
agent = Agent("my_agent", config, registry, llm=None)
```

CI: A GitHub Actions workflow (`.github/workflows/ci.yml`) runs the test suite on push/PR.

- Use provided tasks to run API and tests
## Copilot / assistant prompts

This repository includes reusable prompt templates you can load into Copilot Chat or other assistant sessions to act like a persistent "system prompt".

 - Files: `.prompts/developer_rules.md`, `.prompts/project_objectives.md`, `.prompts/master_system_prompt.md`
- Intended usage: load `developer_rules.md` and `project_objectives.md` first, then run `master_system_prompt.md` as the primary system prompt. The master prompt enforces running tests, updating `README.md` and tests when behavior changes, and never leaving failing tests.

How to use (Copilot Chat):

1. Open the Copilot Chat prompt file action (e.g., "Chat: New Untitled Prompt File") and paste the contents, or save the files into your `.prompts` folder and use Copilot Chat's prompt file loader if available.
2. The backlog document has moved to `backlog.md` at the repo root; scripts for validating it live under `scripts/update_backlog.py`.
2. Run the master prompt at the start of a session so the assistant follows the repository rules.

Quick test command (Windows PowerShell):
```powershell
python -m pytest -q
```


## Plugin authoring

See `docs/plugin_authoring.md` for a short guide and examples on writing MCP plugins. Also check `.prompts/lessons_learned.md` for repository-specific notes and guidance for maintainers and assistants.

Developer setup (dev extras)

To install development dependencies (packaging/test tools) into your venv, run:

```powershell
# from project root, with venv activated
pip install -e '.[dev]'
```

This installs `wheel`, `build`, `setuptools`, and test helpers specified in `pyproject.toml` so you can run the packaging integration tests locally.

Plugin examples

`plugin.py` can expose either a `register()` function or `PLUGIN_NAME`/`PLUGIN_FACTORY` constants. Example:

register() example (in `plugins/foo/plugin.py`):

```python
def register():
  def factory(name, cfg, ssl_verify=True):
    return MyServer(name, cfg, ssl_verify)
  return "foo", factory
```

PLUGIN_* example:

```python
PLUGIN_NAME = "foo"

class MyServer:
  def __init__(self, name, cfg=None, ssl_verify=True):
    self.name = name

PLUGIN_FACTORY = MyServer
```


## License
MIT