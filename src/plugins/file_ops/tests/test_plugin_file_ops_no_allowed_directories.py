"""An instance without allowed_directories opens nothing.

The missing key used to open src, docs, tests and tmp of the project, writable:
an instance an operator or a plugin defined without it let the agent write a
plugin under src/plugins/, which the next start loads (plugin_dirs: src/plugins*).
"""
from __future__ import annotations

import gc
from unittest.mock import Mock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.file_ops.security import SecurityError
from plugins.file_ops.server import FileOpsServer


@pytest.fixture
async def unconfigured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "plugins").mkdir(parents=True)
    config = ToolServerConfig(type="file_ops", enabled=True)
    config.search = {"enable_indexing": False, "enable_semantic_search": False}
    assert getattr(config, "allowed_directories", None) is None
    server = FileOpsServer("file_ops", Mock(spec=AgentSystemConfig), config)
    yield server, tmp_path
    await server.search_engine.stop()
    gc.collect()


async def test_no_folder_is_allowed(unconfigured):
    server, _ = unconfigured
    assert server.validator.allowed_dirs == []


async def test_nothing_is_written_into_src(unconfigured):
    server, root = unconfigured
    with pytest.raises(SecurityError):
        server.validator.validate_path(str(root / "src" / "plugins" / "planted" / "plugin.py"))
    assert not (root / "src" / "plugins" / "planted").exists()
