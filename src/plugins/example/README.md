# Example MCP Plugin

The Example plugin is a reference implementation showing best practices for building Model Context Protocol (MCP) plugins in this repository. It demonstrates multi-tool routing, schema-driven tool definitions, configuration, testing, and CLI usage.

## Overview

The plugin exposes several demonstration tools to illustrate common patterns:

- Calculator: Basic arithmetic operations
- Text Formatter: Simple text transformations
- Status Reporter: Returns plugin/server status and configuration

The implementation lives in `src/plugins/example/` and loads tool definitions from `schema.yaml` when applicable.

## Files

```
src/plugins/example/
├── __init__.py
├── plugin.py         # Plugin factory and registration
├── plugin.yaml       # Plugin metadata used by discovery
├── schema.yaml       # Tool schema definitions (loaded by the server)
├── server.py         # Core server implementation (tool handlers)
├── cli.py            # Small CLI for manual testing/debugging
└── README.md         # This file
```

## Configuration

Enable and configure the plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
    - example

servers:
  example:
    type: example
    # Optional plugin-specific configuration keys
    # precision: 2
    # max_text_length: 1000
    # enable_debug: false
```

Configuration values may also be exposed as environment variables depending on the plugin implementation.

## Usage Examples

These examples assume you have a server instance created via the plugin factory or are calling the server object directly in-process.

### Calculator
```py
# Async call to the calculator tool
result = await server.call("calculator", {"operation": "add", "a": 10, "b": 5})
# Example result: {"operation": "add", "operands": [10.0, 5.0], "result": 15.0}
```

### Text Formatter
```py
result = await server.call("formatter", {"text": "hello world", "format": "uppercase"})
# Example result: {"original": "hello world", "format": "uppercase", "formatted": "HELLO WORLD"}
```

### Status Reporter
```py
result = await server.call("status", {"verbose": True})
# Returns server metadata, config, and available tools
```

## CLI

A lightweight CLI is provided for manual testing and debugging. Run it from the repository root:

```bash
# Calculator example
.venv/Scripts/python.exe -m plugins.example.cli calculator --operation add --a 10 --b 5

# Formatter example
.venv/Scripts/python.exe -m plugins.example.cli formatter --text "hello world" --format uppercase

# Status
.venv/Scripts/python.exe -m plugins.example.cli status --verbose
```

Adjust the python executable path to match your virtual environment on your system.

## Schema

Tool schemas are defined in `schema.yaml` and are loaded by the server using template variables (for example, plugin name or numeric config values). Keep numeric template variables unquoted in the YAML so they render with proper numeric types.

When extending the plugin, add new tool entries to `schema.yaml` and implement the corresponding handler in `server.py`.

## Testing

Plugin tests live in the repository `tests/` directory. To run only example plugin tests, use the pytest selection for the plugin module or test files. Example:

```bash
# Run all tests related to the example plugin
.venv/Scripts/python.exe -m pytest -q tests -k example

# Run specific test file if present
.venv/Scripts/python.exe -m pytest tests/test_example_calculator.py -q
```

Note: The repository tests use mocked external calls where appropriate; run the full test suite in CI to validate integration.

## Contributing

When modifying or extending this plugin:

- Update `schema.yaml` for any new tool or parameter changes.
- Add unit tests covering edge cases and expected behaviors.
- Keep numeric template variables unquoted to preserve types during rendering.
- Run formatting and type checks (`ruff`, `mypy`) before opening a PR.

## Extension Example

To add a new tool:
1. Add the tool entry to `schema.yaml` with parameters and descriptions.
2. Implement the handler in `server.py` and route calls in `call()`.
3. Add unit tests and update documentation.

This README provides a concise, up-to-date reference for the example plugin. If you want, I can also add more concrete CLI usage samples or a short integration example showing how to call the plugin via the running MCP server API.