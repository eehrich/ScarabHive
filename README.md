# Agent System (MCP, Python)

Flexible AI Agent System using the Model Context Protocol (MCP). Runs locally on Windows 11 without Docker. Provides a FastAPI UI and a CLI. MCP servers are configured via YAML and decoupled behind a simple service interface.

## Features
- Object-oriented Python 3.11+
- Pluggable MCP servers via YAML
- Default LLM: Ollama `gpt-oss:20b` (configurable). OpenAI supported.
- Web search servers: AbstractWebSearch + DuckDuckGo, Yahoo Finance, Twitter scrapes
- Web search servers: Google Custom Search (optional), AbstractWebSearch + DuckDuckGo, Yahoo Finance, Twitter scrapes
- LLM Router MCP to other AI models (Ollama/OpenAI)
- FastAPI agent interface at http://127.0.0.1:8000
- No Docker required

## Quickstart (Windows PowerShell)
```powershell
# Create and activate venv
python -m venv .venv; . .venv/Scripts/Activate.ps1

# Install
pip install -U pip; pip install -e .

# Run API
agent-api

# Or run CLI
agent-cli --help
```

## Configuration
Edit `config/agent.yaml`:
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

network:
  ssl_verify: false  # set to false if your corporate network has untrusted SSL interception

logging:
  enabled: true
  level: INFO
  file: logs/agent.log
  as_json: false

prompts:
  system_template: config/prompts/system_prompt.yaml
```

You can override values using environment variables. For OpenAI, set `llm.provider: openai` and provide `OPENAI_API_KEY`.

- Use provided tasks to run API and tests
## Copilot / assistant prompts

This repository includes reusable prompt templates you can load into Copilot Chat or other assistant sessions to act like a persistent "system prompt".

- Files: `.prompts/developer_rules.md`, `.prompts/project_objectives.md`, `.prompts/master_system_prompt.md`
- Intended usage: load `developer_rules.md` and `project_objectives.md` first, then run `master_system_prompt.md` as the primary system prompt. The master prompt enforces running tests, updating `README.md` and tests when behavior changes, and never leaving failing tests.

How to use (Copilot Chat):

1. Open the Copilot Chat prompt file action (e.g., "Chat: New Untitled Prompt File") and paste the contents, or save the files into your `.prompts` folder and use Copilot Chat's prompt file loader if available.
2. Run the master prompt at the start of a session so the assistant follows the repository rules.

Quick test command (Windows PowerShell):
```powershell
python -m pytest -q
```


## License
MIT