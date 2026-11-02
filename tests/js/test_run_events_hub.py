"""Run the run-events harnesses from the Python suite: the tab's stream (static/kit/run-events.js) and the origin's
(static/kit/run-events-worker.js)."""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "run_events_hub.test.mjs"
WORKER_HARNESS = Path(__file__).parent / "run_events_worker.test.mjs"


def test_run_events_hub():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_run_events_worker():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run([node, str(WORKER_HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
