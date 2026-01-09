import subprocess
import sys
import shutil
import time
import logging
from pathlib import Path
import importlib.util


logger = logging.getLogger(__name__)

# Updated path: fixtures is now at tests/fixtures, not tests/mcp/fixtures
FIXTURE_DIR = Path(__file__).parent.parent / 'fixtures' / 'real_plugin'


_HAS_PIP = shutil.which('pip') is not None
_HAS_SETUPTOOLS = importlib.util.find_spec('setuptools') is not None
_HAS_WHEEL = importlib.util.find_spec('wheel') is not None
_HAS_BUILD = importlib.util.find_spec('build') is not None


def _run_with_retries(cmd, cwd=None, retries=3, delay=1):
    """Run a subprocess command with retries; raise last exception on final failure."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            subprocess.check_call(cmd, cwd=cwd)
            return True
        except subprocess.CalledProcessError as e:
            last_exc = e
            logger.warning("Command failed (attempt %s/%s): %s", attempt, retries, cmd)
            time.sleep(delay)
    # final failure
    raise last_exc


def _pip_install(wheel_path):
    return _run_with_retries([sys.executable, '-m', 'pip', 'install', str(wheel_path)])


def _pip_uninstall(package_name):
    # Try uninstall but do not raise; caller should handle failures as warnings
    try:
        _run_with_retries([sys.executable, '-m', 'pip', 'uninstall', '-y', package_name], retries=2, delay=1)
        return True
    except Exception as e:
        logger.warning('pip uninstall failed for %s: %s', package_name, e)
        return False



