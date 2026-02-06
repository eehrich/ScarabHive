# AgentSystem

A flexible, plugin-based AI agent framework built on Python and the Model Context Protocol (MCP). AgentSystem enables you to compose LLM-powered agents with modular tool servers, supporting multi-user sessions, real-time streaming, and extensible plugin architectures.

## Overview

AgentSystem is designed for developers who need:
- **Modular agent composition** with pluggable tool servers via MCP
- **Multi-agent orchestration** with persistent sub-agent hierarchies
- **Real-time streaming** of agent actions and LLM responses via Server-Sent Events
- **Configuration-driven agents** defined in YAML without writing code
- **Multi-user support** with JWT/API key authentication and role-based access control
- **Dual-mode operation** as both MCP client (consuming tools) and MCP server (exposing tools)

## Key Features

- **Plugin System**: 20+ built-in plugins (web research, terminal, SSH, database, script execution, etc.)
- **Schema-Based Agents**: Define custom agents in YAML with tool filtering, LLM profiles, and prompts
- **Sub-Agent Management**: Spawn persistent sub-agents with full conversation context and nested hierarchies
- **Session Management**: Multi-user sessions with automatic persistence and restore
- **Context Management**: Intelligent token budget handling with summarization strategies
- **Vision Support**: Multimodal image input via WebUI and API endpoints
- **Streaming Architecture**: Zero-overhead SSE streams for real-time updates
- **Security**: Tool access control with allow/deny patterns, authentication, rate limiting

## Architecture

```
┌─────────────┐
│   Web UI    │◄─── SSE Streaming
└──────┬──────┘
       │
┌──────▼──────────────────────────────┐
│         FastAPI Application             │
│  ┌────────────┐      ┌───────────────┐  │
│  │   Agent    │◄────►│ Plugin System │  │
│  │   Core     │      └───────┬───────┘  │
│  └─────┬──────┘              │          │
│        │              ┌──────▼────────┐ │
│        │              │  MCP Servers  │ │
│        │              │ (Local/Remote)│ │
│        │              └───────────────┘ │
└────────┼─────────────────────────────────┘
         │
    ┌────▼────┐
    │   LLM   │ (OpenAI, Anthropic, Google, etc.)
    └─────────┘
```

## Quick Links

- **[Installation Guide](INSTALLATION.md)** - Setup, configuration, and deployment
- **Documentation**: [`docs/`](docs/) - Architecture, plugin authoring, API reference
- **Plugins**: [`src/plugins/`](src/plugins/) - Built-in plugins with individual READMEs
- **Configuration**: [`config/`](config/) - YAML-based system and agent configuration
- **Tests**: [`tests/`](tests/) - Comprehensive test suite with pytest

## Use Cases

- **AI Research & Experimentation**: Rapid prototyping of agent behaviors with config-based agents
- **Multi-Step Workflows**: Orchestrate complex tasks across multiple specialized sub-agents
- **Web Automation**: Web scraping, research, and content extraction with built-in tools
- **System Administration**: Remote SSH management, terminal execution, log analysis
- **Trading & Finance**: Market data integration (IBKR, Yahoo Finance plugins)
- **Content Creation**: Interactive book writing system with writer-specific agents

## Getting Started

1. **Install**: Follow the [Installation Guide](INSTALLATION.md)
2. **Configure**: Edit `config/config.yaml` to set your LLM provider API keys
3. **Run**: Start the API server with `agent-api` or use the CLI with `agent-cli`
4. **Explore**: Open `http://localhost:8000` in your browser

```bash
# Quick start
python -m venv .venv
source .venv/Scripts/activate  # Windows Git Bash
pip install -e .

# Configure LLM provider (example)
export OPENAI_API_KEY="your-key-here"

# Start API
agent-api

# Or use CLI
agent-cli "What is the weather in Berlin?"
```

## Project Status

AgentSystem is actively developed and used in production for:
- Interactive book writing workflows (writer plugin suite)
- Financial market analysis (IBKR integration)
- Multi-agent research tasks (web research, sequential thinking)

**Requirements**: Python 3.11+

## Documentation

Core documentation in [`docs/`](docs/):

- **Architecture**: System design, plugin architecture, agent internals
- **Plugin Authoring**: Create custom plugins with schema-based tools
- **Configuration**: YAML-based agent and system configuration
- **API Reference**: REST endpoints, SSE streaming, authentication
- **Session Management**: Multi-user sessions and persistence

## Contributing

1. Follow test-first development (run `pytest -q` before committing)
2. Update `backlog.md` for feature planning and task tracking
3. Maintain plugin READMEs when adding/modifying plugins
4. Use `.prompts/developer_rules.md` for AI-assisted development guidelines

## License

See [LICENSE](LICENSE) file for details.

## Contact

Enrico Ehrich eehrich@googlemail.com