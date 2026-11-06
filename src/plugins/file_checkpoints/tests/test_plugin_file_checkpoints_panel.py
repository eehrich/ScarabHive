"""The Files panel is the plugin's: its catalogue entry, read the way the catalogue reads it."""
from __future__ import annotations

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.file_rewind import unregister_file_rewinder
from agent_system.ui.catalog import plugin_panel
from agent_system.ui.resources import sprite_icons
from plugins.file_checkpoints.plugin import PLUGIN_FACTORY


def test_the_panel_entry_names_the_instance_it_belongs_to(tmp_path):
    plugin = PLUGIN_FACTORY("fc_second", AgentSystemConfig(), ToolServerConfig(
        type="file_checkpoints", enabled=True, config={"storage_path": str(tmp_path)}))
    unregister_file_rewinder(plugin)
    try:
        # What plugin_panels() reads: the schema the plugin hands out, through the catalogue's parser.
        panel = plugin_panel("fc_second", plugin.get_schema_data()["web_ui"]["panel"], sprite_icons())
    finally:
        unregister_file_rewinder(plugin)

    assert (panel.url, panel.category, panel.title) == ("/plugins/fc_second/", "session", "Files")
    assert panel.contexts == {"session": "/plugins/fc_second/?session_id={session_id}"}
    assert plugin.get_schema_data()["hooks"], "the hooks must still be in the schema the hook registry reads"
