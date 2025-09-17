import textwrap

from agent_system.plugins import discover_plugins
from agent_system.mcp.base import MCPRegistry, MCPServer
from agent_system.servers.bootstrap import bootstrap_servers
from agent_system.config.models import AgentConfig


def test_discover_plugins_and_bootstrap(tmp_path, monkeypatch):
    # Create a fake plugin file
    plugin_code = textwrap.dedent('''
    from agent_system.mcp.base import MCPServer

    class FakeServer(MCPServer):
        async def call(self, tool, params):
            return {"tool": tool, "params": params}
        def get_schema(self):
            return {"type": "function", "function": {"name": "fake"}}
        def get_default_action(self):
            return "test"

    def register():
        return ("fake_plugin", lambda name, cfg, ssl_verify=True: FakeServer(name, cfg, ssl_verify))
    ''')

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    plugin_file = plugins_dir / "fake_plugin.py"
    plugin_file.write_text(plugin_code, encoding="utf-8")

    # Discover plugins from plugins dir
    plugins = discover_plugins(plugins_dir)
    assert "fake_plugin" in plugins

    # Bootstrap with a config that enables the plugin
    cfg = AgentConfig()
    cfg.mcp.enabled_servers = ["fake_plugin"]
    cfg.servers = {"fake_plugin": {"type": "fake_plugin"}}

    registry = MCPRegistry()
    # Temporarily change cwd so bootstrap finds plugin dir via relative path
    monkeypatch.chdir(tmp_path)
    bootstrap_servers(cfg, registry)

    assert registry.list() == ["fake_plugin"]
    srv = registry.get("fake_plugin")
    assert isinstance(srv, MCPServer)


def test_bootstrap_respects_plugin_dirs(tmp_path, monkeypatch):
    # create a plugins dir at a custom path
    custom_dir = tmp_path / "my_plugins"
    custom_dir.mkdir()
    plugin_code = textwrap.dedent('''
    from agent_system.mcp.base import MCPServer

    class CustomServer(MCPServer):
        async def call(self, tool, params):
            return {"ok": True}
        def get_schema(self):
            return {}
        def get_default_action(self):
            return "run"

    def register():
        return ("custom_plugin", lambda name, cfg, ssl_verify=True: CustomServer(name, cfg, ssl_verify))
    ''')
    (custom_dir / "custom_plugin.py").write_text(plugin_code, encoding="utf-8")

    cfg = AgentConfig()
    cfg.mcp.enabled_servers = ["custom_plugin"]
    cfg.mcp.plugin_dirs = [str(custom_dir)]
    cfg.servers = {"custom_plugin": {"type": "custom_plugin"}}

    registry = MCPRegistry()
    bootstrap_servers(cfg, registry)
    assert registry.list() == ["custom_plugin"]
