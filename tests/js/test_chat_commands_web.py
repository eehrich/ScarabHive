"""Run the browser-side command harness from the Python suite.

The module under test is plain JS with no framework, so the harness is a node
script with a DOM stub next to this file. Wiring it in here means it runs with
everything else instead of only when someone remembers it.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "chat_commands_web.test.js"


def test_web_chat_commands():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
