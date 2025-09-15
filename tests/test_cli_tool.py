from pathlib import Path
import shutil
import subprocess
import sys
import yaml


def run_cli(args):
    cmd = [sys.executable, "-m", "agent_system.cli"] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def test_tool_help():
    code, out, err = run_cli(["mcp", "tool", "-h"])
    assert code == 0
    assert "Tool action: list, allow, or block" in out or "Tool action" in out


def test_allow_block_updates(tmp_path, monkeypatch):
    # Copy original config to tmp dir and run commands with working dir set
    repo_root = Path(__file__).resolve().parents[1]
    orig = repo_root / "config" / "mcp.yaml"
    tmp_config_dir = tmp_path / "config"
    tmp_config_dir.mkdir()
    cfg_copy = tmp_config_dir / "mcp.yaml"
    cfg_copy.write_text(orig.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.chdir(repo_root)
    # Point to temp config by setting environment or changing path; CLI uses config/mcp.yaml directly
    # So replace config/mcp.yaml with our copy
    target = repo_root / "config" / "mcp.yaml"
    backup = target.with_suffix(target.suffix + ".testbak")
    target.rename(backup)
    try:
        shutil.copy2(cfg_copy, target)
        # Run allow
        code, out, err = run_cli(["mcp", "tool", "localhost", "allow", "hello"])
        assert code == 0
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
    finally:
        # restore original; use copy2 to avoid cross-device rename issues
        try:
            if target.exists():
                target.unlink()
            shutil.copy2(backup, target)
        finally:
            try:
                backup.unlink()
            except Exception:
                pass
