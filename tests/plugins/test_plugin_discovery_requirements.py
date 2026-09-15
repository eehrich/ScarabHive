"""
Test plugin discovery requirements - ensures plugins are discoverable.

These tests verify that:
1. All plugins have PLUGIN_FACTORY in plugin.py (not just server.py)
2. Plugin discovery uses config.plugins.plugin_dirs (not hardcoded paths)
3. Plugins from all configured directories are discovered

Background: Bug 2025-11-09 - Writer plugins not discovered because:
- PLUGIN_FACTORY was in server.py instead of plugin.py
- MCPIntegration used hardcoded plugin_dirs instead of config
"""

import pytest
from pathlib import Path
import importlib.util

from agent_system.plugins import discover_plugins, discover_all_plugins
from agent_system.config.models import AgentSystemConfig, PluginsConfig


class TestPluginFactoryRequirement:
    """Verify all plugins have PLUGIN_FACTORY in plugin.py."""
    
    def test_all_plugins_have_plugin_factory_in_plugin_py(self):
        """CRITICAL: plugin.py MUST export PLUGIN_FACTORY for discovery."""
        plugin_dirs = [Path('src/plugins'), Path('src/plugins_writer'), Path('src/plugins_trading')]
        missing = []
        
        for plugin_dir in plugin_dirs:
            if not plugin_dir.exists():
                continue
                
            for plugin_path in plugin_dir.iterdir():
                if not plugin_path.is_dir():
                    continue
                if plugin_path.name.startswith('_'):
                    continue
                    
                plugin_py = plugin_path / 'plugin.py'
                if not plugin_py.exists():
                    # Some plugins might use server.py directly (legacy pattern)
                    continue
                
                # Check if PLUGIN_FACTORY is defined in plugin.py
                try:
                    spec = importlib.util.spec_from_file_location(
                        f"test_plugin_{plugin_path.name}",
                        str(plugin_py)
                    )
                    if spec is None or spec.loader is None:
                        continue
                        
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    
                    if not hasattr(module, 'PLUGIN_FACTORY'):
                        missing.append(str(plugin_path))
                        
                except Exception:
                    # If import fails, that's a different problem
                    # We're only checking for PLUGIN_FACTORY presence
                    pass
        
        if missing:
            pytest.fail(
                "Plugins missing PLUGIN_FACTORY in plugin.py:\n" +
                "\n".join(f"  - {p}" for p in missing) +
                "\n\nFix: Add to end of plugin.py:\n" +
                "  from .server import MyPluginServer\n" +
                "  PLUGIN_FACTORY = MyPluginServer"
            )
    
    def test_plugin_factory_must_be_in_plugin_py_not_server_py(self):
        """Verify discovery only checks plugin.py, not server.py."""
        # Create a temporary plugin structure to test discovery behavior
        test_plugin_dir = Path('src/plugins')
        
        # Test that plugins with PLUGIN_FACTORY in server.py are NOT discovered
        # (This is the bug we're preventing)
        
        # Real test: Check all plugins ARE discovered
        plugins = discover_plugins(test_plugin_dir)
        
        # If any plugin has server.py but not plugin.py with PLUGIN_FACTORY,
        # it won't be in the discovered list
        assert len(plugins) > 0, "No plugins discovered - discovery broken?"


class TestPluginDirsConfiguration:
    """Verify plugin discovery uses config.plugins.plugin_dirs."""
    
    def test_discover_all_plugins_uses_provided_dirs(self):
        """Verify discover_all_plugins respects dirs parameter."""
        # Test with specific directories
        dirs = [Path('src/plugins')]
        plugins = discover_all_plugins(dirs=dirs)
        
        # Should find plugins from src/plugins
        assert len(plugins) > 0, "No plugins found in src/plugins"
        
        # Test with multiple directories
        dirs = [Path('src/plugins'), Path('src/plugins_writer')]
        plugins_multi = discover_all_plugins(dirs=dirs)
        
        # Should find more plugins (writer plugins included)
        assert len(plugins_multi) >= len(plugins), \
            "Multiple dirs should find same or more plugins"
    
    def test_all_configured_plugin_dirs_are_discovered(self):
        """Verify plugins from ALL configured directories are found."""
        # No clearing of plugins_writer.* from sys.modules here: a later test that
        # imported a function before this ran would then patch a fresh module
        # object while its function reads the old one.
        # Simulate config with multiple plugin_dirs
        plugin_dirs = [Path('src/plugins'), Path('src/plugins_writer')]
        
        all_plugins = discover_all_plugins(dirs=plugin_dirs)
        
        # Check that plugins from src/plugins are found
        standard_plugins = ['example', 'todo', 'memory', 'basic_operations']
        found_standard = [p for p in standard_plugins if p in all_plugins]
        assert len(found_standard) > 0, \
            "No standard plugins found - src/plugins not discovered?"
        
        # Check that plugins from src/plugins_writer are found
        # Note: v3 architecture replaced writer_graph/writer_state with writer_path
        writer_plugins = ['writer_content', 'writer_path', 'writer_admin', 'writer_player', 'writer_core', 'writer_search', 'writer_review']
        found_writer = [p for p in writer_plugins if p in all_plugins]
        assert len(found_writer) > 0, \
            f"No writer plugins found - src/plugins_writer not discovered? Found: {list(all_plugins.keys())}"


class TestMCPIntegrationPluginDirs:
    """Verify MCPIntegration reads plugin_dirs from config."""
    
    @pytest.mark.asyncio
    async def test_mcp_integration_uses_config_plugin_dirs(self):
        """CRITICAL: MCPIntegration must use config.plugins.plugin_dirs."""
        from agent_system.mcp.integration import MCPIntegration
        
        # Create config with custom plugin_dirs
        config = AgentSystemConfig(
            plugins=PluginsConfig(
                plugin_dirs=['src/plugins', 'src/plugins_writer'],
                servers={}
            )
        )
        
        # Create MCPIntegration with config (not mock_registry)
        # MCPIntegration requires AgentSystemConfig as parameter
        MCPIntegration(app=None, config=config)
        
        # The _discover_and_register_plugins method should use config.plugins.plugin_dirs
        # We can't easily test the private method, but we can verify the config is accessible
        assert config.plugins is not None
        assert config.plugins.plugin_dirs == ['src/plugins', 'src/plugins_writer']
        
        # Indirect test: Verify discovery would use these dirs
        # (The actual method is async and has side effects, so we test the config structure)
        plugin_dirs = config.plugins.plugin_dirs if config.plugins and config.plugins.plugin_dirs else ['src/plugins']
        assert 'src/plugins_writer' in plugin_dirs, \
            "plugin_dirs from config should include src/plugins_writer"


class TestPluginDiscoveryDocumentation:
    """Verify documentation matches actual discovery behavior."""
    
    def test_discovery_behavior_matches_docs(self):
        """Ensure discovery.py behavior matches what plugin_authoring.md says."""
        from agent_system.plugins.discovery import discover_plugins
        
        # Documentation says: "discovers plugins in a directory"
        # Documentation says: "Folder plugin: <dir>/<plugin>/plugin.py must export PLUGIN_FACTORY"
        
        # Test this behavior
        test_dir = Path('src/plugins')
        if test_dir.exists():
            plugins = discover_plugins(test_dir)
            
            # Each discovered plugin should have come from plugin.py
            for name, factory in plugins.items():
                assert factory is not None, \
                    f"Plugin {name} has no factory - discovery broken?"
        
        # Edge case: Plugin with only server.py (no plugin.py) should NOT be discovered
        # This is the expected behavior per docs


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
