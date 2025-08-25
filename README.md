# Agent System (MCP, Python)

Flexible AI Agent System using the Model Context Protocol (MCP). Runs locally on Windows 11 without Docker. Provides a FastAPI UI and a CLI. MCP servers are configured via YAML and decoupled behind a simple service interface.

## Features
- Object-oriented Python 3.11+
- Pluggable MCP servers via YAML
- Default LLM: Ollama `gpt-oss:20b` (configurable). OpenAI supported.
- Web search servers: AbstractWebSearch + DuckDuckGo, Yahoo Finance, Twitter scrapes
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

mcp:
  enabled_servers:
    - websearch_abstract
    - websearch_google
    - yahoo_finance
    - twitter_search
    - llm_router

servers:
  websearch_abstract:
    type: websearch_abstract
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
```

You can override values using environment variables. For OpenAI, set `llm.provider: openai` and provide `OPENAI_API_KEY`.

## VS Code
- Python 3.11+
- Recommended: Install Microsoft Python and Pylance extensions
- Use provided tasks to run API and tests

## License
MIT