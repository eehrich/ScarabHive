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
```

You can override values using environment variables. For OpenAI, set `llm.provider: openai` and provide `OPENAI_API_KEY`.

## VS Code
- Python 3.11+
- Recommended: Install Microsoft Python and Pylance extensions
- Use provided tasks to run API and tests

## License
MIT