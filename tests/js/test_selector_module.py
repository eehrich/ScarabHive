"""Run the composer selector's node harness (selector_module.test.js) from the Python suite."""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "selector_module.test.js"


def test_selector_module():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
