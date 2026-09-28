"""Run the browser-side ask_user harness from the Python suite.

The harness is a node script next to this file: the functions the chat uses to
show the model's question and send the answer, taken out of chat_module.js and
run against a DOM stub.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "ask_user_ui.test.js"


def test_ask_user_ui():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
