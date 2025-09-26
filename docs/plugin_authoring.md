Plugin authoring guide

This short guide shows how to create a plugin (MCP server) for AgentSystem.

Entry-point group

- Default entry-point group for packaged plugins: `agent_system.mcp_plugins`

Plugin contract

- A plugin must expose either:
  - a `register()` function that returns `(name, factory)` where `factory` is a callable that returns an `MCPServer` instance, or
  - module-level constants `PLUGIN_NAME` and `PLUGIN_FACTORY`.

- The factory signature should accept `(name: str, config: dict, ssl_verify: bool=True)` and return an instance of `MCPServer` or a compatible object.

Multi-Tool Architecture (Required)

All AgentSystem plugins must follow the **Multi-Tool format**:

- **Schema**: Define tools using `tools:` array in `schema.yaml`:
  ```yaml
  tools:
    - type: function
      function:
        name: my_tool
        description: "Tool description"
        parameters:
          type: object
          properties: {...}
  ```

- **Server Implementation**: Implement `async list_tools()` method:
  ```python
  async def list_tools(self) -> list[dict[str, Any]]:
      """Return the MCP tools list."""
      schema = load_schema_from_dir(Path(__file__).parent)
      return schema["tools"]
  ```

- **Tool Routing**: Route calls directly by tool name in `call()` method:
  ```python
  async def call(self, tool: str, params: dict[str, Any]) -> Any:
      if tool == "my_tool":
          return await self.handle_my_tool(params)
      else:
          return {"error": f"Unknown tool: {tool}"}
  ```

Minimal example (filesystem plugin)

Recommended folder layout

Each plugin should live in its own folder under `plugins/` with the
entrypoint file named `plugin.py`. This allows the plugin to include
auxiliary modules, resources, or data files.

Example layout:

```
plugins/
    example/
        plugin.py
        utils.py
        templates/
            ...
```

Create `plugins/example/schema.yaml` with Multi-Tool format:

```yaml
tools:
  - type: function
    function:
      name: example_tool
      description: "Example tool demonstration"
      parameters:
        type: object
        properties:
          message:
            type: string
            description: "Message to process"
        required: ["message"]
```

Create `plugins/example/plugin.py` with:

```python
from pathlib import Path
from typing import Any
from agent_system.plugins.schema_loader import load_schema_from_dir

PLUGIN_NAME = "example"

class ExampleServer:
    def __init__(self, name, cfg, ssl_verify=True):
        self.name = name
        self.cfg = cfg
        self.ssl_verify = ssl_verify

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the MCP tools list."""
        schema = load_schema_from_dir(Path(__file__).parent)
        if not schema:
            raise RuntimeError("Missing required schema.yaml")
        return schema["tools"]

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Handle tool calls with direct routing."""
        if tool == "example_tool":
            message = params.get("message", "")
            return {"status": "ok", "processed_message": f"Processed: {message}"}
        else:
            return {"error": f"Unknown tool: {tool}"}

PLUGIN_FACTORY = ExampleServer
```

Packaging example (entry-point)

- In `setup.py` or `pyproject.toml` define an entry point:

```toml
[project.entry-points]
agent_system.mcp_plugins = [
    "example = mypackage.plugins:factory"
]
```

- The target callable `mypackage.plugins:factory` must return a factory callable as above.

Testing

- Use `discover_all_plugins()` to find both filesystem and entry-point plugins.
- Unit tests can monkeypatch `importlib.metadata.entry_points()` to simulate installed plugins.

CLI helper: show metadata

- The CLI includes a convenience flag for operators and tests: when running
    `agent_system.cli plugins list --format json --show-metadata` the CLI will
    include the discovered plugin's parsed `plugin.yaml` contents under a
    top-level `metadata` key for each plugin in the JSON output. This is
    helpful for debugging discovery and for automated systems that need
    to inspect plugin metadata without importing plugin modules directly.


Testing notes

- For quick unit tests, monkeypatching `importlib.metadata.entry_points()` or
    `importlib.metadata.distributions()` is fast and deterministic (see
    `tests/test_mcp_entrypoints.py` and `tests/test_mcp_entrypoint_integration.py`).

- For higher-fidelity integration tests, build and install a small test
    package (wheel) into the test venv and assert real entry-point
    resolution. This is slower but exercises packaging metadata and real
    importlib.metadata behavior.

- The integration tests may be async. Use `pytest-asyncio` and mark tests
    with `@pytest.mark.asyncio` to `await` async plugin `call()` methods.

Security

- Treat third-party plugins as untrusted. Prefer sandboxing or process isolation for executing them in production.
