from pathlib import Path
import subprocess
import sys


def run_cli(args):
    cmd = [sys.executable, "-m", "agent_system.agent_cli"] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return proc.returncode, proc.stdout, proc.stderr


def test_tool_help():
    code, out, err = run_cli(["mcp", "tool", "-h"])
    assert code == 0
    assert "Tool action: list, allow, or block" in out or "Tool action" in out


def test_allow_block_updates(tmp_path, monkeypatch):
    # Copy original config to tmp dir and run commands with working dir set
    # Use a temp workspace so the repository config isn't modified
    # Each test gets unique tmp_path from pytest, avoiding parallel conflicts
    import uuid
    tmp_repo = tmp_path / f"test_{uuid.uuid4().hex[:8]}"
    tmp_repo.mkdir()
    # repo_root is now tests/cli, need to go up two levels to project root
    repo_root = Path(__file__).resolve().parents[2]

    # Copy config files to temp directory
    tmp_config_dir = tmp_repo / "config"
    tmp_config_dir.mkdir()

    # Copy agent config (main config file that CLI loads by default)
    agent_orig = repo_root / "config" / "config.yaml"
    agent_copy = tmp_config_dir / "config.yaml"
    agent_copy.write_text(agent_orig.read_text(encoding="utf-8"), encoding="utf-8")

    # Copy LLM config (included by agent.yaml)
    llm_orig = repo_root / "config" / "llm.yaml"
    llm_copy = tmp_config_dir / "llm.yaml"
    llm_copy.write_text(llm_orig.read_text(encoding="utf-8"), encoding="utf-8")

    # Copy MCP servers config (new structure - contains external servers configuration)
    mcp_servers_orig = repo_root / "config" / "mcp_servers.yaml"
    mcp_servers_copy = tmp_config_dir / "mcp_servers.yaml"
    mcp_servers_copy.write_text(mcp_servers_orig.read_text(encoding="utf-8"), encoding="utf-8")

    # Change CWD to the temp workspace so CLI reads config files from there
    # Save original directory to restore later
    import os
    original_cwd = os.getcwd()
    try:
        monkeypatch.chdir(tmp_repo)

        # Ensure the copied mcp_servers.yaml has the target server enabled so CLI registers it
        import yaml as _yaml
        data = _yaml.safe_load(mcp_servers_copy.read_text(encoding='utf-8')) or {}
        external = data.get('external_servers', {})
        remote = external.get('remote_servers', {})
        if 'localhost' in remote:
            remote['localhost']['enabled'] = True
        else:
            # If remote not present, create a minimal entry
            remote['localhost'] = {'url': 'http://127.0.0.1:8081', 'enabled': True, 'tools': {'allowed': [], 'blocked': []}}
        external['remote_servers'] = remote
        data['external_servers'] = external
        mcp_servers_copy.write_text(_yaml.safe_dump(data, sort_keys=False), encoding='utf-8')

        # Instead of invoking a subprocess (which complicates config resolution),
        # call the CLI helper directly with a mock MCPIntegration so we can control
        # configured_external_servers and run the async helper to update the file.
        import asyncio
        from agent_system.agent_cli import _allow_server_tool, _block_server_tool
        from agent_system.services import ToolService

        class DummyIntegration:
            def __init__(self, configured):
                self.configured_external_servers = configured

        # Build configured_external_servers mapping based on the copied config file
        data = _yaml.safe_load(mcp_servers_copy.read_text(encoding='utf-8')) or {}
        external = data.get('external_servers', {})
        remote = external.get('remote_servers', {})
        configured = {}
        for name, cfg in (remote or {}).items():
            configured[name] = type('RemoteMCPConfig', (), cfg)

        integration = DummyIntegration(configured)

        # Construct a ToolService using the dummy integration and pass it to the
        # CLI helpers. The CLI helpers expect a service exposing allow_tool/block_tool
        # (the production code uses ToolService), so tests should mirror that.
        tool_service = ToolService(integration, None)

    # Run allow and block helpers directly
        # Run allow command and verify result
        asyncio.run(_allow_server_tool(tool_service, 'localhost', 'hello'))

        target = tmp_config_dir / 'mcp_servers.yaml'
        # Add small delay to ensure file write completes (atomic_write might buffer)
        import time
        time.sleep(0.01)

        data = _yaml.safe_load(target.read_text(encoding='utf-8'))
        servers_block = data.get('external_servers', {})
        remote = servers_block.get('remote_servers', {}) or {}
        server_cfg = remote.get('localhost') or {}
        tools_dict = server_cfg.get('tools', {})
        allowed = tools_dict.get('allowed', [])
        assert 'hello' in allowed

        # Run block command and verify result
        asyncio.run(_block_server_tool(tool_service, 'localhost', 'hello'))

        # Add small delay to ensure file write completes
        time.sleep(0.01)

        data = _yaml.safe_load(target.read_text(encoding='utf-8'))
        servers_block = data.get('external_servers', {})
        remote = servers_block.get('remote_servers', {}) or {}
        server_cfg = remote.get('localhost') or {}
        tools_dict = server_cfg.get('tools', {})
        blocked = tools_dict.get('blocked', [])
        assert 'hello' in blocked
    finally:
        # Restore original directory
        os.chdir(original_cwd)
