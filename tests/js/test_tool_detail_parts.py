"""Run the node harness for the chat's tool detail renderer (toolPartNodes) from the Python suite."""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "tool_detail_parts.test.js"


def test_tool_detail_parts():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
