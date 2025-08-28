# AgentSystem — Flexible MCP-based Agent Framework (Python)

A lightweight, pluggable agent framework that composes LLMs and tool servers using the Model Context Protocol (MCP).

This README is a concise developer and user guide matching this repository layout.

## Quick links
- Code: `src/agent_system`
- Config: all yaml files in `config/`
- Docs: `docs/`
- Logfiles: `logs/` - AgentSystem logfiles. cli and api
- Plugins: `plugins/`
- Tests: `tests/` (pytest)
- Prompts for assistant sessions: `.prompts/` and `.github/`
- Helper Scripts: `scripts/`
- Ticketsystem/Backlog: `backlog.md`
- Python-ProjectSetup: `pyenvironment.toml`

## Requirements
- Python 3.11+
- Git (for development)
- Optional: API key(s) for LLM providers (configured via env vars or `config/agent.yaml`)

## Recommended shell on Windows
On Windows we recommend Git Bash for interactive debugging and venv activation. PowerShell examples remain supported but may require setting ExecutionPolicy.

## Quickstart (Git Bash — recommended on Windows)
1. Create and activate a virtualenv (Git Bash):

```bash
python -m venv .venv
source .venv/Scripts/activate
```

(PowerShell alternative)

```powershell
python -m venv .venv
. .venv/Scripts/Activate.ps1    # may require changing ExecutionPolicy
```

(CMD alternative)

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

2. Install the project (editable):

```bash
pip install -U pip
pip install -e .
```

3. Run tests:

```bash
python -m pytest -q
```

## How to use
- CLI: `agent-cli "Your question or task"` or `agent-cli --help`
- API: start the HTTP API in a separate, activated terminal and call the endpoints below.

Start the API (recommended in a second terminal):

```bash
# with venv activated in the terminal
python -m agent_system.agent.interface_api
# or the convenience wrapper (if installed in PATH)
agent-api
```

API endpoints (FastAPI):
- `GET /health` — health check
- `GET /config` — returns loaded configuration
- `POST /run?task=...` — run a task and return final result
- `GET /events?task=...` — SSE stream of MCP events

## How to debug
1. Use two terminals: one for the API, one for running tests / CLI commands. Do not run a long-lived server and tests in the same terminal.
2. Activate the venv in both terminals (see Quickstart).
3. Start the API in terminal A and leave it running.
4. Reproduce issues or run tests in terminal B.

Using VS Code launch configurations (recommended)
- Ensure VS Code uses the project venv: open the Command Palette -> `Python: Select Interpreter` and pick `.venv/Scripts/python.exe`.
- Open the Run and Debug view (Ctrl+Shift+D) and choose one of the launch configurations:
  - `Launch Agent API (module)` — starts the API module under the debugger.
  - `Agent CLI (module) — with args` — prompts for CLI args (for example: `run "What is the time in Nitra/Slovakia?"`).
  - `Debug pytest (module) — run single test or folder` — prompts for pytest target (for example `tests/test_cli.py::test_case` or `tests/`).
- When prompted for inputs, enter the desired args or pytest target and start debugging. Breakpoints will bind to your source code.

Tips:
- To run a single test with an interactive debugger (pytest + pdb fallback):

```bash
python -m pytest tests/test_example.py::test_case -q -s --maxfail=1 --pdb
```

- Increase logging for troubleshooting: edit `config/agent.yaml` and set `logging.level: DEBUG`. Logs are written to `logs/` (for example `logs/agent.log`, `logs/cli.log`, `logs/api.log`).
- If Windows PowerShell blocks activation, prefer Git Bash or CMD to avoid ExecutionPolicy issues.

## Configuration
Primary manifest: `config/agent.yaml`. The manifest may include other files (recommended for MCP-specific settings) via `includes:`. The CLI writes only to included managed files (for example `mcp.yaml`) when managing MCP settings.

Example minimal snippet:

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
  file: logs/agent.log
  file_cli: logs/cli.log
  file_api: logs/api.log
  as_json: false

prompts:
  system_template: config/prompts/system_prompt.yaml

max_steps: 50
```

## Plugins
Plugins live under `plugins/<name>/` and should expose a package-style layout with `plugin.py` and optional `plugin.yaml` for metadata. The loader also supports legacy single-file plugins.

Important: plugins are loaded in-memory under the `plugins.<name>` namespace to avoid collisions with stdlib modules (for example `datetime`).

See `docs/plugin_authoring.md` and `plugins/example` for examples.

Plugin-Management with:

```bash
agent-cli plugins list|info|enable|disable|search|status — manage plugins
```

## Development (with AI)
- Follow instructions and guidlines. For repository rules: venv activation, testing, and commit guidance.
- Run tests with `pytest -q` or `python -m pytest -q` and do not leave failing tests.
- To install dev dependencies (with venv activated):

```bash
pip install -e '.[dev]'
```

- When changing behavior, update `README.md` and tests accordingly.
- Maintain `backlog.md`

## Maintenance notes

- Keep `backlog.md` updated for project-relevant changes (new tasks, epics, decisions). The repository includes a conservative `backlog` CLI that supports dry-run and write modes; prefer dry-run first and use `--write` to persist.
- Record short "lessons learned" entries in `.prompts/lessons_learned.md` when a non-trivial architectural decision or incident occurs.

## Contributing
- Use `backlog.md` to track tasks and update it after finishing or documenting progress.
- Add tests for new or changed behavior (happy path + at least one edge case).
- Keep changes minimal and focused; prefer small, well-tested commits.

## Contact / Notes
- This repository includes assistant prompt templates in `.prompts/` (developer rules and project objectives). Load `developer_rules.md` and `project_objectives.md` first in any assistant session, then `master_system_prompt.md`.

## License
MIT

---
