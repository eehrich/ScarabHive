"""Run the run-events hub harness (one stream per tab, static/kit/run-events.js) from the Python suite."""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "run_events_hub.test.mjs"


def test_run_events_hub():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
