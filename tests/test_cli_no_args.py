from __future__ import annotations

import sys
from importlib import reload

import pytest

from agent_system import agent_cli as cli


def test_cli_no_args_prints_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agent-cli"])
    # Reload to ensure any module-level changes from previous tests don't interfere
    reload(cli)
    cli.main()
    captured = capsys.readouterr()
    assert "Agent System CLI" in captured.out
