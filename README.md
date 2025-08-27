AgentSystem — Flexible MCP-based Agent Framework (Python)
# AgentSystem — MCP-based Agent Framework

A lightweight, pluggable agent framework that composes LLMs and tool servers using the Model Context Protocol (MCP).

This README is a short, focused developer and user guide that matches the current repository layout and behavior.

## Quickstart (Windows PowerShell)

1) Create and activate a virtual environment

```powershell
python -m venv .venv; . .venv/Scripts/Activate.ps1
```

2) Install the project (editable)

```powershell
pip install -U pip
pip install -e .
```

3) Run tests

```powershell
python -m pytest -q
```

4) Start the API (separate terminal)

```powershell
agent-api
```

Or use the CLI

```powershell
agent-cli --help
agent-cli "What is the time in Nitra/Slovakia?"
```

## Configuration

The repo uses `config/agent.yaml` as the master manifest. The manifest may include other files using `includes:` (recommended for MCP-specific settings). The CLI will not overwrite the master manifest when managing MCP settings — it writes only to included managed files (for example `mcp.yaml`).

Minimal configuration snippet (important options):

```yaml
llm:
  provider: openai
  model: gpt-5-mini
  openai_api_key: ${OPENAI_API_KEY}
  context_window: 32768

includes:
  - mcp.yaml

network:
  ssl_verify: false
  host: 127.0.0.1
  port: 8000

logging:
  enabled: true
  level: DEBUG
  file: logs/agent.log       # legacy single-file config
  file_cli: logs/cli.log     # optional: explicit CLI log file
  file_api: logs/api.log     # optional: explicit API log file
  as_json: false

prompts:
  system_template: config/prompts/system_prompt.yaml

max_steps: 50
```

Notes on logging behavior
- Preferred: set `logging.file_cli` and/or `logging.file_api` to control where the CLI and API write logs.
- Backward-compatible: if the per-role fields are absent, the system falls back to `logging.file` and derives role-specific filenames (e.g., `agent-cli` / `agent-api`) to avoid clobbering a single log file when running both processes.
- Consider adding log rotation or an external log collector for production workloads.

## CLI

Key flags and behavior:
- `--config PATH` — load a different manifest
- `--no-stream` — disable live streaming of MCP calls/results
- `--raw` — print final result as raw JSON instead of pretty printing
- `--color/--no-color` — control ANSI color output

Commands:
- `agent-cli run "task"` — run a task (default when no subcommand is given)
- `agent-cli plugins list|info|enable|disable|search|status` — manage plugins

The CLI streams MCP CALL and MCP RESULT events by default and prints a human-readable, colorized summary at the end.

## API

A small FastAPI app provides endpoints:
- `GET /health` — health check
- `GET /config` — returns loaded configuration
- `POST /run?task=...` — run a task and return final result
- `GET /events?task=...` — SSE stream of MCP events

Start with `agent-api`.

## Plugins

Plugins live under the repository `plugins/` directory (package-style `plugins/<name>/plugin.py` with optional `plugin.yaml` for metadata). The loader also discovers legacy single-file plugins and entrypoints.

Important: plugin packages are loaded under the `plugins.<name>` namespace in-memory to avoid collisions with stdlib module names (e.g., `datetime`).

See `docs/plugin_authoring.md` for authoring guidance and `plugins/example` for a sample plugin.

## Development notes
- Tests are in `tests/` and run with pytest. The project includes tests that exercise plugin discovery, CLI streaming, and config loader behavior.
- The repository includes `.prompts/` templates used by developer-assistants for consistent behavior (see `.prompts/developer_rules.md`).

## Contributing
- Follow the `backlog.md` for task tracking and add entries when implementing project-relevant changes.
- Run tests and keep them green before pushing changes.

## License
MIT
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