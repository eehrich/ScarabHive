import json
import io
import sys
from contextlib import redirect_stdout, redirect_stderr
import agent_system.cli as cli


def test_cli_plugins_list_show_metadata(tmp_path, monkeypatch):
    """Integration-style test: run the CLI to list plugins with --show-metadata in JSON
    and assert that the output is valid JSON and contains metadata keys for discovered plugins.
    """
    # Run the CLI in-process to exercise the entrypoint behavior and capture output.
    argv_backup = sys.argv[:] if hasattr(sys, 'argv') else None
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        sys.argv = [sys.executable, 'plugins', 'list', '--format', 'json', '--show-metadata']
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            try:
                cli.main()
            except SystemExit:
                # main may call sys.exit(); ignore
                pass
    finally:
        if argv_backup is not None:
            sys.argv = argv_backup

    out = out_buf.getvalue().strip()
    data = json.loads(out)
    assert isinstance(data, list)
    # If any plugins are discovered, they should include a 'metadata' key
    if data:
        assert "metadata" in data[0]
