import subprocess
import sys
import shutil
import time
import logging
from pathlib import Path
import importlib.util
import pytest

from agent_system.plugins import discovery as plugins

logger = logging.getLogger(__name__)

FIXTURE_DIR = Path(__file__).parent / 'fixtures' / 'real_plugin'


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


@pytest.mark.skipif(not (_HAS_PIP and _HAS_SETUPTOOLS and _HAS_WHEEL and _HAS_BUILD), reason='packaging tools (pip/setuptools/wheel/build) not available')
def test_build_and_install_real_plugin(tmp_path):
    # Build wheel using PEP 517 build tool
    dist_dir = tmp_path / 'dist'
    dist_dir.mkdir()
    cmd_build = [sys.executable, '-m', 'build', '--wheel', '--outdir', str(dist_dir)]
    _run_with_retries(cmd_build, cwd=str(FIXTURE_DIR))

    wheels = list(dist_dir.glob('*.whl'))
    assert wheels, 'wheel not built'
    wheel = wheels[0]

    installed = False
    try:
        # Install wheel into current venv (with retries)
        _pip_install(wheel)
        installed = True

        # Wait a short time for importlib.metadata to notice the new distribution
        import importlib.metadata as _md
        found = False
        for _ in range(10):
            try:
                dists = list(_md.distributions())
                if any(d.metadata.get('Name', '').lower() == 'test_plugin_real' for d in dists):
                    found = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        assert found, 'installed distribution not found by importlib.metadata'

        plugins_map = plugins.discover_all_plugins(dirs=[Path('plugins')])
        # Should include the real_example entrypoint
        assert 'real_example' in plugins_map
        factory = plugins_map['real_example']
        server = factory('real_example', {})
        import asyncio
        res = asyncio.run(server.call())
        assert res['status'] == 'real'
    finally:
        # Always attempt uninstall if we installed; log failures but don't raise
        if installed:
            _pip_uninstall('test_plugin_real')
        # remove built artifacts from tmp dist dir
        try:
            for f in dist_dir.glob('*'):
                try:
                    f.unlink()
                except Exception:
                    logger.debug('Failed to remove %s during cleanup', f)
        except Exception:
            logger.debug('Failed to cleanup dist directory %s', dist_dir)
