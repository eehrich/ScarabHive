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
    
    # Copy both agent.yaml and mcp.yaml to temp directory
    tmp_config_dir = tmp_repo / "config"
    tmp_config_dir.mkdir()
    
    # Copy agent config (main config file that CLI loads by default)
    agent_orig = repo_root / "config" / "agent.yaml"
    agent_copy = tmp_config_dir / "agent.yaml"
    agent_copy.write_text(agent_orig.read_text(encoding="utf-8"), encoding="utf-8")
    
    # Copy MCP config (contains external servers configuration)
    mcp_orig = repo_root / "config" / "mcp.yaml"
    cfg_copy = tmp_config_dir / "mcp.yaml"
    cfg_copy.write_text(mcp_orig.read_text(encoding="utf-8"), encoding="utf-8")

    # Change CWD to the temp workspace so CLI reads config/mcp.yaml from there
    monkeypatch.chdir(tmp_repo)

    # Run allow
    code, out, err = run_cli(["mcp", "tool", "localhost", "allow", "hello"])
    assert code == 0
    target = tmp_config_dir / "mcp.yaml"
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    mcp_block = data.get("mcp", data)
    servers = mcp_block.get("external_servers", {})
    server_cfg = servers.get("localhost") or {}
    assert "hello" in (server_cfg.get("allowed_tools") or [])

    # Run block
    code, out, err = run_cli(["mcp", "tool", "localhost", "block", "hello"])
    assert code == 0
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    mcp_block = data.get("mcp", data)
    servers = mcp_block.get("external_servers", {})
    server_cfg = servers.get("localhost") or {}
    assert "hello" in (server_cfg.get("blocked_tools") or [])
