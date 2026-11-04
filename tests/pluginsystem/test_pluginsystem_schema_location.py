"""The registry finds a plugin's schema.yaml next to the plugin's factory.

It used to look only in ``src/plugins/<instance name>/`` below the working
directory: an instance whose name differs from its type, or any other working
directory, ended up with no schema at all.
"""

import importlib.util
import sys

import pytest

from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
from agent_system.plugins.tool_adapter import PluginToolRegistry, _load_plugin_schema


class _Server:
    """No get_schema_data(): the schema has to come from the file."""

    def __init__(self, name):
        self.name = name

    async def call(self, action, params):
        return {}


@pytest.fixture
def factory(tmp_path):
    plugin_dir = tmp_path / "plugins_elsewhere" / "schemaplug"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "schema.yaml").write_text(
        "name: '{{ name }}'\nfunctions:\n  - name: '{{ name }}_ping'\n"
        "    description: ping\n    parameters: {type: object}\n")
    (plugin_dir / "plugin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, server_config):\n"
        "    return SERVER(name)\n")
    module_name = "_schema_location_test_plugin"
    spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.SERVER = _Server
    yield module.PLUGIN_FACTORY
    sys.modules.pop(module_name, None)


def test_schema_comes_from_the_factorys_folder(factory, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no src/plugins here
    schema = _load_plugin_schema("my_instance", _Server("my_instance"), factory, "schemaplug")
    assert schema["name"] == "my_instance"


@pytest.mark.asyncio
async def test_instance_named_unlike_its_type_gets_its_schema(factory, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = PluginToolRegistry()
    registry.plugin_factories["schemaplug"] = factory
    await registry.register_plugin_simple(
        "my_instance", AgentSystemConfig(),
        ToolServerConfig(type="schemaplug", enabled=True, agent_config=AgentConfig()))
    tools = await registry.get_server("my_instance").list_tools()
    assert [tool.name for tool in tools] == ["my_instance_ping"]


def test_without_a_factory_the_type_folder_below_cwd_is_used(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "src" / "plugins" / "typed"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "schema.yaml").write_text("name: '{{ name }}'\n")
    monkeypatch.chdir(tmp_path)
    assert _load_plugin_schema("inst", _Server("inst"), None, "typed") == {"name": "inst"}
