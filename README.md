# ScarabHive

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg)](https://www.python.org/)

A flexible, plugin-based AI agent framework built on Python using Vibe-Coding. ScarabHive enables you to compose LLM-powered agents with modular tool servers, supporting multi-user sessions, real-time streaming, and extensible plugin architectures.

## Overview

ScarabHive is designed for developers who need:
- **Modular agent composition** with pluggable tool servers
- **Multi-agent orchestration** with persistent sub-agent hierarchies
- **Real-time streaming** of agent actions and LLM responses via Server-Sent Events
- **Configuration-driven agents** defined in YAML without writing code
- **Multi-user support** with JWT/API key authentication and role-based access control
- **MCP client** consuming tools from external MCP servers

## Key Features
- **Plugin System**: 30+ built-in plugins (web research, terminal, SSH, database, script execution, media generation, etc.)
- **Schema-Based Agents**: Define custom agents in YAML with tool filtering, LLM profiles, and prompts
- **Multi-LLM Support**: OpenAI, Anthropic Claude, Google Gemini, Ollama, OpenRouter, Batch-Support
- **Sub-Agent Management**: Spawn persistent sub-agents with full conversation context and nested hierarchies
- **Session Management**: Multi-user sessions with automatic persistence and restore
- **Context Management**: Intelligent token budget handling with summarization strategies
- **Vision/Audio Support**: Multimodal image and audio input via WebUI and API endpoints
- **Streaming Architecture**: Zero-overhead SSE streams for real-time updates
- **Mid-Run Steering**: Inject user messages into a running agent — it picks them up at the next step and reacts (see `docs/mid_run_message_injection.md`)
- **Security**: Tool access control with allow/deny patterns, authentication, rate limiting
- **CLI App**: use agent-cli or agent-run to run Agents from CLI instead of WebUI
- **Powerful WebUI**: analyse your llm requests, context optimizations in the WebUI. Profile Speed and memory consumtion.

## Architecture

```
┌─────────────┐
│   Web UI    │◄─── SSE Streaming
└──────┬──────┘
       │
┌──────▼──────────────────────────────────┐
│         FastAPI Application             │
│  ┌────────────┐      ┌───────────────┐  │
│  │   Agent    │◄────►│ Plugin System │  │
│  │   Core     │      └───────┬───────┘  │
│  └─────┬──────┘              │          │
│        │              ┌──────▼────────┐ │
│        │              │  Servers      │ │
│        │              │ (Local/Remote)│ │
│        │              └───────────────┘ │
└────────┼────────────────────────────────┘
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

## Built-in Plugins

| Category | Plugins |
|----------|---------|
| **Agent & Workflow** | basic_agent, sub_agent_manager, agent_continuation, task_switch, sequential_thinking, cognitive_stack,  memory,  todo, lessons_learned |
| **Web & Search** | web_scraper, duckduckgo_search, tavily_search |
| **System & Files** | terminal, file_ops, ssh_control, script_interpreter, sqlite_query |
| **Context** | context_engineer, context_summarizer, context_usage_tracker |
| **Media** | audio_ops, comfyui |
| **Monitoring & Debug** | log_viewer, batch_monitor, message_debugger, message_validator, request_logger |
| **Utilities** | basic_operations, datetime, weather, http_server, markdown_formatter, user_management, llm_router, example |

## Use Cases

- **AI Research & Experimentation**: Rapid prototyping of agent behaviors with config-based agents
- **Multi-Step Workflows**: Orchestrate complex tasks across multiple specialized sub-agents
- **Web Automation**: Web scraping, research, and content extraction with built-in tools
- **System Administration**: Remote SSH management, terminal execution, log analysis
- **Media Generation**: Image/audio/video generation via ComfyUI integration
- **Knowledge Management**: Persistent memory, lessons learned, and context optimization

## Getting Started

1. **Install**: Follow the [Installation Guide](INSTALLATION.md)
2. **Configure**: Edit `config/config.yaml` to set your LLM provider API keys
3. **Run**: Start the API server with `agent-api` or use the CLI with `agent-cli`
4. **Explore**: Open `http://localhost:8000` in your browser

```bash
# Quick start
python -m venv .venv 
source .venv/Scripts/activate  # Windows Git Bash
pip install -e . # optional [dev,test,gpu]

# Configure LLM provider (example)
export OPENAI_API_KEY="your-key-here"

# Start API
agent-api

# Or use CLI
agent-cli "What is the weather in Berlin?"
```

## Project Status

ScarabHive is actively developed and used in production for:
- Multi-agent research tasks (web research, sequential thinking)
- Automated system administration workflows
- Media generation pipelines (ComfyUI, audio processing)
- Custom domain-specific agent systems via plugin extensions
- Automated Content Creation

**Requirements**: Python 3.11+ (recommended 3.12)

## Documentation

Core documentation in [`docs/`](docs/):

- **Architecture**: System design, plugin architecture, agent internals
- **Plugin Authoring**: Create custom plugins with schema-based tools
- **Configuration**: YAML-based agent and system configuration
- **API Reference**: REST endpoints, SSE streaming, authentication
- **Session Management**: Multi-user sessions and persistence

## Contributing

1. Follow test-first development (run `pytest -q` before committing)
2. Maintain plugin READMEs when adding/modifying plugins

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full license text.

## Author

Enrico Ehrich