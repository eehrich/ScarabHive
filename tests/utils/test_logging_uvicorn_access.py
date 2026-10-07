"""uvicorn's access log reaches the terminal, never the logfile, however the server was started.

uvicorn writes it only where uvicorn.access has a handler (h11_impl: access_logger.hasHandlers()). Its CLI
gives it one before the app is built; uvicorn.run(log_config=None) -- agent-api, the VS Code tasks -- gives it
none, and setup_logging keeps it off the root (a line per request in api.log): the access log went silent.
"""
from __future__ import annotations

import io
import logging
import sys

import pytest

from agent_system.utils.logging import setup_logging

_TOUCHED = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpcore", "httpx", "asyncio")


@pytest.fixture
def access(tmp_path):
    """uvicorn.access without a handler, as uvicorn.run(log_config=None) leaves it; the logging restored after."""
    root = logging.getLogger()
    before = list(root.handlers), root.level
    touched = {name: (logging.getLogger(name).level, logging.getLogger(name).propagate,
                      list(logging.getLogger(name).filters), list(logging.getLogger(name).handlers))
               for name in _TOUCHED}
    logger = logging.getLogger("uvicorn.access")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    yield logger, tmp_path / "api.log"
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in before[0]:
            handler.close()
    for handler in before[0]:
        root.addHandler(handler)
    root.setLevel(before[1])
    for name, (level, propagate, filters, handlers) in touched.items():
        restored = logging.getLogger(name)
        restored.level, restored.propagate, restored.filters, restored.handlers = level, propagate, filters, handlers


def _request_line(logger):
    logger.info('%s - "%s %s HTTP/%s" %d', "192.0.2.7:5000", "GET", "/health", "1.1", 200)
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_without_a_handler_of_its_own_the_access_log_goes_to_the_terminal_only(access, capsys):
    logger, path = access
    setup_logging(True, "INFO", str(path))
    logging.getLogger("agent_system.probe").info("an own line")
    _request_line(logger)

    assert logger.hasHandlers(), "uvicorn would turn the access log off"
    assert "GET /health" in capsys.readouterr().out
    written = path.read_text(encoding="utf-8")
    assert "an own line" in written, "the logfile got nothing: this test would look at nothing"
    assert "GET /health" not in written


def test_a_later_setup_s_console_takes_the_access_log_over(access, monkeypatch):
    logger, path = access
    setup_logging(True, "INFO", str(path))
    later = io.StringIO()
    monkeypatch.setattr(sys, "stdout", later)
    setup_logging(True, "INFO", str(path))
    _request_line(logger)

    assert later.getvalue().count("GET /health") == 1


def test_uvicorn_s_own_handler_stays_alone(access, capsys):
    # started as `uvicorn --factory`: uvicorn's dictConfig hung its handler there before the app was built
    logger, path = access
    own = logging.StreamHandler()
    logger.addHandler(own)
    setup_logging(True, "INFO", str(path))

    assert logger.handlers == [own]
