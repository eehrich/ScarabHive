"""The API's console: a terminal that stops reading must not stop the server (2026-10-03: a VS Code
terminal held the event loop in a log line for 40 minutes)."""
import logging
import sys
import threading
import time

from agent_system.utils import logging as L


class Stalled:
    """A console that does not read until released."""

    def __init__(self):
        self.entered, self.release, self.got = threading.Event(), threading.Event(), []

    def write(self, text):
        self.entered.set()
        self.release.wait()
        self.got.append(text)

    def flush(self):
        pass

    def isatty(self):
        return False


def test_a_console_that_stops_reading_holds_no_one():
    stalled = Stalled()
    console = L._UnblockedConsole(stalled, limit=3)
    try:
        console.write("0\n")
        assert stalled.entered.wait(5), "the writer thread takes the first line"
        start = time.monotonic()
        for i in range(1, 10):
            assert console.write(f"{i}\n") == 2
        assert time.monotonic() - start < 1, "no write waits for the console"
        assert not console.isatty(), "the console's own attributes"
    finally:
        stalled.release.set()
    console._settle(5)
    assert stalled.got[:4] == ["0\n", "1\n", "2\n", "3\n"], "the first ones in order, the rest dropped"
    assert len(stalled.got) == 5 and stalled.got[4].startswith("[console] 6 Ausgaben ausgelassen"), stalled.got


def test_the_servers_handlers_and_streams_go_through_it(monkeypatch):
    monkeypatch.setattr(L, "_unblocked", False)
    out, err = Stalled(), Stalled()
    for name, stream in (("stdout", out), ("__stdout__", out), ("stderr", err), ("__stderr__", err)):
        monkeypatch.setattr(sys, name, stream)
    logger = logging.getLogger("test_unblocked_console")
    handler, raw = logging.StreamHandler(err), logging.StreamHandler(out)
    for h in (handler, raw):
        logger.addHandler(h)
    monkeypatch.setattr(logger, "propagate", False)
    try:
        L.unblock_console()
        assert isinstance(sys.stdout, L._UnblockedConsole) and isinstance(sys.stderr, L._UnblockedConsole)
        assert handler.stream is sys.stderr and raw.stream is sys.stdout, "uvicorn's handlers too"
        start = time.monotonic()
        logger.warning("ein Satz")
        print("noch einer")
        assert time.monotonic() - start < 1, "the log line and the print did not wait"
        first = sys.stderr
        L.unblock_console()
        assert sys.stderr is first and handler.stream is first, "once"
    finally:
        out.release.set(), err.release.set()
        for h in (handler, raw):
            logger.removeHandler(h)
