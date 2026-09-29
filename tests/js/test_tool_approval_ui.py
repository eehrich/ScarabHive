"""Run the browser-side tool_approval harness from the Python suite.

The harness is a node script next to this file: the functions the chat uses to
answer a tool_approval question and to show a call that did not run, taken out
of chat_module.js and run against a DOM stub.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "tool_approval_ui.test.js"


def test_tool_approval_ui():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
