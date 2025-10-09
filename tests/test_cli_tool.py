from pathlib import Path
import subprocess
import sys
import yaml


def run_cli(args):
    cmd = [sys.executable, "-m", "agent_system.cli"] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return proc.returncode, proc.stdout, proc.stderr


def test_tool_help():
    code, out, err = run_cli(["mcp", "tool", "-h"])
    assert code == 0
    assert "Tool action: list, allow, or block" in out or "Tool action" in out


def test_allow_block_updates(tmp_path, monkeypatch):
    # Copy original config to tmp dir and run commands with working dir set
    # Use a temp workspace so the repository config isn't modified
    tmp_repo = tmp_path
    repo_root = Path(__file__).resolve().parents[1]
    
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
    
    # Copy MCP config (contains external servers configuration)
    mcp_orig = repo_root / "config" / "mcp.yaml"
    cfg_copy = tmp_config_dir / "mcp.yaml"
    cfg_copy.write_text(mcp_orig.read_text(encoding="utf-8"), encoding="utf-8")

    # Change CWD to the temp workspace so CLI reads config/mcp.yaml from there
    monkeypatch.chdir(tmp_repo)

    # Ensure the copied mcp.yaml has the target server enabled so CLI registers it
    import yaml as _yaml
    data = _yaml.safe_load(cfg_copy.read_text(encoding='utf-8')) or {}
    mcp_block = data.get('mcp', data)
    external = mcp_block.get('external_servers', {})
    remote = external.get('remote_servers', {})
    if 'localhost' in remote:
        remote['localhost']['enabled'] = True
    else:
        # If remote not present, create a minimal entry
        remote['localhost'] = {'url': 'http://127.0.0.1:8081', 'enabled': True, 'tools': {'allowed': [], 'blocked': []}}
    external['remote_servers'] = remote
    mcp_block['external_servers'] = external
    if 'mcp' in data:
        data['mcp'] = mcp_block
    else:
        data = mcp_block
    cfg_copy.write_text(_yaml.safe_dump(data, sort_keys=False), encoding='utf-8')

    # Instead of invoking a subprocess (which complicates config resolution),
    # call the CLI helper directly with a mock MCPIntegration so we can control
    # configured_external_servers and run the async helper to update the file.
    import asyncio
    from agent_system.cli import _allow_server_tool, _block_server_tool
    from agent_system.services import ToolService

    class DummyIntegration:
        def __init__(self, configured):
            self.configured_external_servers = configured

    # Build configured_external_servers mapping based on the copied config file
    data = yaml.safe_load(cfg_copy.read_text(encoding='utf-8')) or {}
    mcp_block = data.get('mcp', data)
    external = mcp_block.get('external_servers', {})
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
    asyncio.run(_allow_server_tool(tool_service, 'localhost', 'hello'))

    target = tmp_config_dir / 'mcp.yaml'
    data = yaml.safe_load(target.read_text(encoding='utf-8'))
    mcp_block = data.get('mcp', data)
    servers_block = mcp_block.get('external_servers', {})
    remote = servers_block.get('remote_servers', {}) or {}
    server_cfg = remote.get('localhost') or {}
    tools_dict = server_cfg.get('tools', {})
    allowed = tools_dict.get('allowed') or server_cfg.get('allowed_tools') or []
    assert 'hello' in allowed

    asyncio.run(_block_server_tool(tool_service, 'localhost', 'hello'))

    data = yaml.safe_load(target.read_text(encoding='utf-8'))
    mcp_block = data.get('mcp', data)
    servers_block = mcp_block.get('external_servers', {})
    remote = servers_block.get('remote_servers', {}) or {}
    server_cfg = remote.get('localhost') or {}
    blocked = (server_cfg.get('tools', {}) or {}).get('blocked') or server_cfg.get('blocked_tools') or []
    assert 'hello' in blocked
