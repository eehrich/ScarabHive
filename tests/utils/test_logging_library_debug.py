"""A library's DEBUG never reaches the log; ScarabHive's own DEBUG does -- also a plugin's from a folder of its own.

With logging.level DEBUG the MCP client wrote every SSE message whole -- a screenshot tool's answer is a line of
900 KB -- and httpcore and filelock every step: 56 MB of api.log in five hours, 65 % of it libraries' DEBUG.
Driven through the real setup_logging; whose a line is, the filter reads from where it was logged.
"""
from __future__ import annotations

import asyncio.base_events
import logging

import filelock
import mcp.client.streamable_http
import pytest

import agent_system.agent_cli
import agent_system.runtime
from agent_system.utils.logging import setup_logging

# setup_logging sets these; the fixture gives the rest of the suite its own settings back
_TOUCHED = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpcore", "httpx", "asyncio")


@pytest.fixture
def log(tmp_path):
    """The log file and the handlers setup_logging hung at the root."""
    path = tmp_path / "api.log"
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    touched = {name: (logging.getLogger(name).level, logging.getLogger(name).propagate,
                      list(logging.getLogger(name).filters), list(logging.getLogger(name).handlers))
               for name in _TOUCHED}
    setup_logging(True, "DEBUG", str(path))
    added = [handler for handler in root.handlers if handler not in before]
    yield path, added
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in before:
            handler.close()
    for handler in before:
        root.addHandler(handler)
    root.setLevel(level)
    for name, (lvl, propagate, filters, handlers) in touched.items():
        logger = logging.getLogger(name)
        logger.level, logger.propagate, logger.filters, logger.handlers = lvl, propagate, filters, handlers


def _record(name: str, path: str | None, level: int, msg: str) -> logging.LogRecord:
    """A record as logging.<level>() in the code at ``path`` makes it."""
    return logging.getLogger(name).makeRecord(name, level, path, 1, msg, (), None)


def _emit(name: str, path: str | None, level: int, msg: str) -> logging.LogRecord:
    """The record, handed to the root's handlers."""
    record = _record(name, path, level, msg)
    logging.getLogger(name).handle(record)
    return record


def _written(path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name, where", [
    ("agent_system.runtime", agent_system.runtime.__file__),
    ("__main__", agent_system.agent_cli.__file__),
    ("path.my_tool.plugin", "custom/plugin/path/my_tool/plugin.py"),  # a plugin folder of the user's (pkg = folder)
    ("stategraph_machine_3_helpers", "data/stategraph/machines/3/helpers.py"),  # a machine's companion module
])
def test_our_own_debug_is_written(log, name, where):
    path, _ = log
    _emit(name, where, logging.DEBUG, "own debug line")
    assert "own debug line" in _written(path)


LIBRARIES = [
    (mcp.client.streamable_http.__name__, mcp.client.streamable_http.__file__),
    (filelock.__name__, filelock.__file__),
    (asyncio.base_events.__name__, asyncio.base_events.__file__),
    # Rust that primp hands to logging (pyo3-log), as measured: the .rs file it was built from
    ("hickory_net::udp::udp_client_stream", "C:\\Users\\runneradmin\\.cargo\\registry\\src\\"
     "index.crates.io-1949cf8c6b5b557f\\hickory-net-0.26.1\\src\\udp\\udp_client_stream.rs"),
    ("h2::codec::framed_read", "crates\\primp-h2\\src\\codec\\framed_read.rs"),
]


@pytest.mark.parametrize("name, where", LIBRARIES)
def test_a_librarys_debug_is_not(log, name, where):
    path, added = log
    _emit(name, where, logging.INFO, "a library's info line")
    record = _emit(name, where, logging.DEBUG, "SSE message: " + "iVBORw0KGgo" * 1000)
    written = _written(path)
    assert "a library's info line" in written, "the fixture wrote nothing at all"
    assert "SSE message" not in written
    assert len(added) == 2, "setup_logging hangs a file and a console handler"
    assert not any(handler.filter(record) for handler in added), "the console writes it"


def test_a_record_without_a_file_does_not_break_the_call_that_logs_it(log):
    path, _ = log
    _emit("primp", None, logging.DEBUG, "no file to tell whose")  # raised TypeError into the logging call
    assert "no file to tell whose" in _written(path)


def test_agent_run_verbose_drops_a_librarys_debug_too():
    from agent_system.agent_run import setup_basic_logging

    root = logging.getLogger()
    before, level = root.handlers[:], root.level
    root.handlers.clear()  # basicConfig sets up nothing on a root that has handlers already (pytest's)
    try:
        setup_basic_logging(verbose=True)
        added = root.handlers[:]
    finally:
        root.handlers[:] = before
        root.setLevel(level)
    assert len(added) == 1, "agent-run's one handler"
    assert not added[0].filter(_record(*LIBRARIES[0], logging.DEBUG, "SSE message"))
    assert added[0].filter(_record("agent_system.runtime", agent_system.runtime.__file__, logging.DEBUG, "own"))


def test_a_librarys_own_call_is_recognised(log):
    """Not a record made up here: filelock's own logger.debug, where logging itself finds who called."""
    path, _ = log
    logger = logging.getLogger("filelock")
    level = logger.level
    logger.setLevel(logging.NOTSET)  # the suite's conftest quiets it itself; the filter is under test
    try:
        with filelock.FileLock(str(path.parent / "probe.lock")):
            pass
        _emit("agent_system.runtime", agent_system.runtime.__file__, logging.DEBUG, "own line after the lock")
    finally:
        logger.setLevel(level)
    written = _written(path)
    assert "own line after the lock" in written, "the fixture wrote nothing at all"
    assert "probe.lock" not in written
