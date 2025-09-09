import json
import subprocess
from pathlib import Path


def test_cli_plugins_list_show_metadata(tmp_path, monkeypatch):
    """Integration-style test: run the CLI to list plugins with --show-metadata in JSON
    and assert that the output is valid JSON and contains metadata keys for discovered plugins.
    """
    # Run the CLI in a subprocess to exercise the full entrypoint behavior.
    cmd = [
        str(Path(".venv") / "Scripts" / "python.exe"),
        "-m",
        "agent_system.cli",
        "plugins",
        "list",
        "--format",
        "json",
        "--show-metadata",
    ]
    # Use the repository root as cwd so discovery of filesystem plugins works
    proc = subprocess.run(cmd, cwd=Path.cwd(), capture_output=True, text=True)
    assert proc.returncode == 0, f"CLI failed: {proc.stderr}"
    out = proc.stdout.strip()
    data = json.loads(out)
    assert isinstance(data, list)
    # If any plugins are discovered, they should include a 'metadata' key
    if data:
        assert "metadata" in data[0]
