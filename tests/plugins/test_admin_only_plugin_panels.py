"""Plugin types whose panels must be admins' under ANY instance name.

config/security.yaml matches paths, that is instance names: a renamed or a
second instance falls to the default role, every user's. A type that declares
`min_role` in get_security_config keeps it (PluginWebRegistry.effective_policy).

- debate_forum: no owner per channel -- its API reads every user's direct
  messages and posts under any agent name into a running debate.
- agent_eval: a run's suite checks execute commands.
"""
import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig


@pytest.mark.parametrize("plugin_type", ["debate_forum", "agent_eval", "log_viewer"])
def test_the_type_declares_admin(plugin_type, tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    factory = importlib.import_module(f"plugins.{plugin_type}.plugin").PLUGIN_FACTORY
    plugin = factory("renamed_instance", AgentSystemConfig(),
                     ToolServerConfig(type=plugin_type, enabled=True))
    assert plugin.get_security_config().get("min_role") == "admin"
