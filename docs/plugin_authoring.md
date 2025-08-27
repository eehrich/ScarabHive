Plugin authoring guide

This short guide shows how to create a plugin (MCP server) for AgentSystem.

Entry-point group

- Default entry-point group for packaged plugins: `agent_system.mcp_plugins`

Plugin contract

- A plugin must expose either:
  - a `register()` function that returns `(name, factory)` where `factory` is a callable that returns an `MCPServer` instance, or
  - module-level constants `PLUGIN_NAME` and `PLUGIN_FACTORY`.

- The factory signature should accept `(name: str, config: dict, ssl_verify: bool=True)` and return an instance of `MCPServer` or a compatible object.

Minimal example (filesystem plugin)

Create `plugins/example_plugin.py` with:

```python
PLUGIN_NAME = "example"

class ExampleServer:
    def __init__(self, name, cfg, ssl_verify=True):
        self.name = name
        self.cfg = cfg
        self.ssl_verify = ssl_verify

    def call(self, *args, **kwargs):
        return {"status": "ok", "name": self.name}

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

Security

- Treat third-party plugins as untrusted. Prefer sandboxing or process isolation for executing them in production.
