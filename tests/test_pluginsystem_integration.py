#!/usr/bin/env python3
"""Integration tests for plugin discovery and CLI functionality.

Tests the complete plugin system end-to-end, including:
- Plugin discovery from filesystem
- agent-cli plugins commands
- Individual plugin CLIs (mcp-<name>)
- Configuration integration
- Enable/disable workflow
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

# Temporarily removed sys.path manipulation to avoid conflicts
# sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for integration testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Create basic directory structure
        (temp_path / "config").mkdir()
        (temp_path / "logs").mkdir()
        (temp_path / "src").mkdir()
        (temp_path / "plugins").mkdir()

        # Copy our actual plugins for testing
        import shutil
        source_plugins = Path(__file__).parent.parent / "src" / "plugins"
        if source_plugins.exists():
            for plugin_dir in source_plugins.iterdir():
                if plugin_dir.is_dir():
                    # Copy to both locations for compatibility
                    shutil.copytree(plugin_dir, temp_path / "plugins" / plugin_dir.name)
                    shutil.copytree(plugin_dir, temp_path / "src" / "plugins" / plugin_dir.name)

        # Create test configuration files
        create_test_config(temp_path)

        yield temp_path


def create_test_config(workspace_path: Path):
    """Create test configuration files."""
    # Main agent config
    agent_config = {
        "includes": ["mcp.yaml"],
        "logging": {
            "enabled": True,
            "level": "INFO",
            "file": "logs/agent.log"
        },
        "network": {
            "ssl_verify": True
        }
    }

    with open(workspace_path / "config" / "agent.yaml", "w") as f:
        yaml.safe_dump(agent_config, f, allow_unicode=True, sort_keys=False)

    # MCP config
    mcp_config = {
        "plugin_dirs": ["plugins"],
        "enabled_servers": ["llm_router", "web_scraper", "http_server"]
    }

    with open(workspace_path / "config" / "mcp.yaml", "w") as f:
        yaml.safe_dump(mcp_config, f, allow_unicode=True, sort_keys=False)


def run_cli_command(workspace_path: Path, command: List[str], env: Dict[str, str] = None) -> subprocess.CompletedProcess:
    """Run a CLI command in the test workspace."""
    cmd_env = os.environ.copy()
    # Include both the workspace src and the original src directory
    original_src = Path(__file__).parent.parent / "src"
    path_sep = ";" if os.name == "nt" else ":"
    cmd_env["PYTHONPATH"] = f"{workspace_path / 'src'}{path_sep}{original_src}"
    # Ensure we're using the test workspace and not the main project
    cmd_env["PWD"] = str(workspace_path)
    if env:
        cmd_env.update(env)

    # Use the same Python executable that's running pytest
    python_exe = sys.executable
    if command[0] == "python":
        command[0] = python_exe

    # Change to workspace directory
    completed = subprocess.run(
        command,
        cwd=workspace_path,
        env=cmd_env,
        capture_output=True,
        text=False,
        timeout=30
    )

    # Decode outputs safely to preserve previous callers expecting text
    stdout = completed.stdout.decode(errors='replace') if isinstance(completed.stdout, (bytes, bytearray)) else (completed.stdout or "")
    stderr = completed.stderr.decode(errors='replace') if isinstance(completed.stderr, (bytes, bytearray)) else (completed.stderr or "")

    # Build a CompletedProcess-like result with decoded text
    return subprocess.CompletedProcess(
        args=completed.args,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr
    )


class TestPluginDiscoveryIntegration:
    """Test plugin discovery and basic functionality."""

    def test_plugin_discovery_finds_all_plugins(self, temp_workspace):
        """Test that plugin discovery finds all expected plugins."""
        # Import the discovery function
        from agent_system.plugins import discover_all_plugins

        # Discover plugins
        plugins = discover_all_plugins([temp_workspace / "plugins"])

        # Should find our converted plugins
        expected_plugins = ["llm_router", "web_scraper", "http_server", "yahoo_finance", "twitter_search"]

        for plugin_name in expected_plugins:
            assert plugin_name in plugins, f"Plugin {plugin_name} not discovered"
            assert plugins[plugin_name] is not None, f"Plugin {plugin_name} factory is None"

    def test_plugin_metadata_loading(self, temp_workspace):
        """Test that plugin metadata is loaded correctly."""
        from agent_system.plugins import discover_all_plugins

        plugins = discover_all_plugins([temp_workspace / "plugins"])

        # Check that metadata is attached
        for plugin_name, factory in plugins.items():
            metadata = getattr(factory, "_plugin_metadata", None)
            assert metadata is not None, f"Plugin {plugin_name} missing metadata"

            # Check required metadata fields
            assert "name" in metadata, f"Plugin {plugin_name} missing name in metadata"
            assert "description" in metadata, f"Plugin {plugin_name} missing description"
            assert "version" in metadata, f"Plugin {plugin_name} missing version"


class TestAgentCliPlugins:
    """Test agent-cli plugins commands."""

    def test_cli_plugins_list_table(self, temp_workspace):
        """Test agent-cli plugins list in table format."""
        config_path = temp_workspace / "config" / "agent.yaml"
        result = run_cli_command(
            temp_workspace,
            ["python", "-m", "agent_system.cli", "--config", str(config_path), "plugins", "list"]
        )

        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        assert "NAME" in result.stdout, "Table header not found"
        assert "llm_router" in result.stdout, "llm_router plugin not listed"

    def test_cli_plugins_list_json(self, temp_workspace):
        """Test agent-cli plugins list in JSON format."""
        config_path = temp_workspace / "config" / "agent.yaml"
        result = run_cli_command(
            temp_workspace,
            ["python", "-m", "agent_system.cli", "--config", str(config_path), "plugins", "list", "--format", "json"]
        )

        assert result.returncode == 0, f"CLI failed: {result.stderr}"

        # Parse JSON output
        try:
            plugin_list = json.loads(result.stdout)
            assert isinstance(plugin_list, list), "Expected list of plugins"
            assert len(plugin_list) > 0, "No plugins found"

            # Check that we have expected plugins
            plugin_names = [p["name"] for p in plugin_list]
            assert "llm_router" in plugin_names, "llm_router not in plugin list"

        except json.JSONDecodeError as e:
            pytest.fail(f"Invalid JSON output: {e}")

    def test_cli_plugins_info(self, temp_workspace):
        """Test agent-cli plugins info command."""
        config_path = temp_workspace / "config" / "agent.yaml"
        result = run_cli_command(
            temp_workspace,
            ["python", "-m", "agent_system.cli", "--config", str(config_path), "plugins", "info", "llm_router"]
        )

        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        assert "NAME: llm_router" in result.stdout, "Plugin info not displayed"

    def test_cli_plugins_enable_disable(self, temp_workspace):
        """Test enabling and disabling plugins via CLI."""
        config_path = temp_workspace / "config" / "agent.yaml"
        mcp_config_path = temp_workspace / "config" / "mcp.yaml"
        
        # First check initial state
        with open(mcp_config_path) as f:
            initial_config = yaml.safe_load(f)
        
        # First disable a plugin that's currently enabled
        result = run_cli_command(
            temp_workspace,
            ["python", "-m", "agent_system.cli", "--config", str(config_path), "plugins", "disable", "web_scraper", "--yes"]
        )

        assert result.returncode == 0, f"Disable command failed: {result.stderr}"

        # Verify it's disabled by checking the managed config file
        managed_config_path = temp_workspace / "config" / "agent.managed.yaml"
        if managed_config_path.exists():
            with open(managed_config_path) as f:
                config = yaml.safe_load(f)
            enabled_servers = config.get("mcp", {}).get("enabled_servers", [])
        else:
            # Fall back to mcp.yaml if no managed file
            with open(mcp_config_path) as f:
                config = yaml.safe_load(f)
            enabled_servers = config.get("mcp", {}).get("enabled_servers", [])
        
        assert "web_scraper" not in enabled_servers, "web_scraper should be disabled in config"

        # Re-enable the plugin
        result = run_cli_command(
            temp_workspace,
            ["python", "-m", "agent_system.cli", "--config", str(config_path), "plugins", "enable", "web_scraper", "--yes"]
        )

        assert result.returncode == 0, f"Enable command failed: {result.stderr}"

        # Verify it's enabled in the managed config
        if managed_config_path.exists():
            with open(managed_config_path) as f:
                config = yaml.safe_load(f)
            enabled_servers = config.get("mcp", {}).get("enabled_servers", [])
        else:
            with open(mcp_config_path) as f:
                config = yaml.safe_load(f)
            enabled_servers = config.get("mcp", {}).get("enabled_servers", [])
        
        assert "web_scraper" in enabled_servers, "web_scraper should be enabled in config"


class TestIndividualPluginClis:
    """Test individual plugin CLI entry points."""

    def test_llm_router_cli_help(self, temp_workspace):
        """Test llm_router plugin CLI help."""
        result = run_cli_command(
            temp_workspace,
            ["python", "-m", "plugins.llm_router", "--help"]
        )

        assert result.returncode == 0, f"CLI help failed: {result.stderr}"
        assert "LLM Router MCP Server" in result.stdout, "Help text not found"

    def test_web_scraper_cli_help(self, temp_workspace):
        """Test web_scraper plugin CLI help."""
        result = run_cli_command(
            temp_workspace,
            ["python", "-m", "plugins.web_scraper", "--help"]
        )

        assert result.returncode == 0, f"CLI help failed: {result.stderr}"
        assert "Web Scraper MCP Server" in result.stdout, "Help text not found"

    def test_http_server_cli_help(self, temp_workspace):
        """Test http_server plugin CLI help."""
        result = run_cli_command(
            temp_workspace,
            ["python", "-m", "plugins.http_server", "--help"]
        )

        assert result.returncode == 0, f"CLI help failed: {result.stderr}"
        assert "HTTP Server MCP Plugin" in result.stdout, "Help text not found"

    @pytest.mark.skipif(os.name == 'nt', reason="Windows asyncio issue with _overlapped module")
    def test_plugin_server_startup_shutdown(self, temp_workspace):
        """Test that a plugin server can start up and shut down cleanly."""
        import time
        import signal

        # Start the server
        proc = subprocess.Popen(
            ["python", "-m", "plugins.llm_router", "--server", "--port", "8999"],
            cwd=temp_workspace,
            env={"PYTHONPATH": str(temp_workspace / "src")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace'
        )

        # Give it more time to start
        time.sleep(5)

        # Check if it's still running
        if proc.poll() is not None:
            # Server exited, check stderr for error message
            stdout, stderr = proc.communicate()
            stdout = stdout or ""
            stderr = stderr or ""
            pytest.fail(f"Server exited early with code {proc.returncode}. Stdout: {stdout}. Stderr: {stderr}")

        # Send SIGTERM to shut it down
        proc.terminate()

        # Wait for it to exit
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()  # Force kill if it doesn't respond to SIGTERM
            pytest.fail("Server didn't respond to SIGTERM, had to force kill")

        # Should have exited cleanly
        assert proc.returncode == 0 or proc.returncode == -signal.SIGTERM, f"Server didn't exit cleanly: {proc.returncode}"


class TestPluginConfigurationIntegration:
    """Test plugin configuration integration."""

    def test_plugin_respects_config(self, temp_workspace):
        """Test that plugins respect configuration settings."""
        config_path = temp_workspace / "config" / "agent.yaml"
        
        # Modify config to only enable llm_router
        mcp_config_path = temp_workspace / "config" / "mcp.yaml"
        with open(mcp_config_path) as f:
            config = yaml.safe_load(f)

        # The config should have mcp at the top level
        if "mcp" not in config:
            config = {"mcp": config}
        
        config["mcp"]["enabled_servers"] = ["llm_router"]  # Only enable llm_router

        with open(mcp_config_path, "w") as f:
            yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)

        # Test that the CLI reflects the config changes
        result = run_cli_command(
            temp_workspace,
            ["python", "-m", "agent_system.cli", "--config", str(config_path), "plugins", "list", "--format", "json"]
        )

        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        plugin_list = json.loads(result.stdout)

        for plugin in plugin_list:
            if plugin["name"] == "llm_router":
                assert plugin["enabled"], "llm_router should be enabled"
            elif plugin["name"] in ["web_scraper", "http_server"]:  # These were in original config
                assert not plugin["enabled"], f"Plugin {plugin['name']} should be disabled"

    def test_plugin_directory_configuration(self, temp_workspace):
        """Test that plugin directory configuration works."""
        # Modify config to use a different plugin directory
        mcp_config_path = temp_workspace / "config" / "mcp.yaml"
        with open(mcp_config_path) as f:
            config = yaml.safe_load(f)

        # Create a subdirectory for plugins
        plugin_subdir = temp_workspace / "custom_plugins"
        plugin_subdir.mkdir()

        # Move one plugin to the custom directory
        import shutil
        shutil.move(temp_workspace / "plugins" / "llm_router", plugin_subdir / "llm_router")

        config["plugin_dirs"] = ["custom_plugins", "plugins"]

        with open(mcp_config_path, "w") as f:
            yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)

        # Test that plugins are still discovered
        result = run_cli_command(
            temp_workspace,
            ["python", "-m", "agent_system.cli", "--config", "config/agent.yaml", "plugins", "list", "--format", "json"]
        )

        plugin_list = json.loads(result.stdout)
        plugin_names = [p["name"] for p in plugin_list]
        assert "llm_router" in plugin_names, "llm_router should still be discovered from custom directory"


if __name__ == "__main__":
    pytest.main([__file__])
