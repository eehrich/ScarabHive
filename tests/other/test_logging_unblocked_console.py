"""The API's console: a terminal that stops reading must not stop the server (2026-10-03: a VS Code
terminal held the event loop in a log line for 40 minutes), and must lose no crash."""
import logging
import sys
import threading
import time
import warnings

from agent_system.utils import logging as L


class Stalled:
    """A console that does not read until released."""

    def __init__(self, log=None, refuse=""):
        self.entered, self.release, self.got = threading.Event(), threading.Event(), []
        self.log, self.refuse = self.got if log is None else log, refuse

    def write(self, text):
        self.entered.set()
        self.release.wait()
        if self.refuse and self.refuse in text:
            raise UnicodeEncodeError("ascii", text, 0, 1, "ordinal not in range(128)")
        self.log.append(text)

    def flush(self):
        pass

    def isatty(self):
        return False


def test_a_console_that_stops_reading_holds_no_one():
    stalled = Stalled()
    console = L._UnblockedConsole(stalled, L._ConsoleQueue(limit=3))
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
    console._pending.settle(5)
    assert "".join(stalled.got).startswith("0\n1\n2\n3\n[console] 6 outputs dropped (the console did not read"), \
        ("the first ones in order, the rest dropped and counted", stalled.got)
    who = []
    stalled.write = lambda text: who.append(threading.current_thread().name)
    console.write("nach dem Ende\n")
    assert who == [threading.current_thread().name], "settled and drained: the rest of the exit goes straight out"


def test_stdout_and_stderr_keep_their_order_and_a_failed_write_is_named():
    lines = []
    out, err = Stalled(lines), Stalled(lines, refuse="═")
    out.release.set(), err.release.set()
    pending = L._ConsoleQueue()
    o, e = L._UnblockedConsole(out, pending), L._UnblockedConsole(err, pending)
    for console, text in ((o, "a\n"), (e, "b\n"), (o, "c\n"), (e, "═\n"), (o, "d\n")):
        console.write(text)
    pending.settle(5)
    told = [t for t in lines if t.startswith("[console] 1 outputs dropped (UnicodeEncodeError")]
    assert [t for t in lines if t not in told] == ["a\n", "b\n", "c\n", "d\n"] and len(told) == 1, \
        ("one queue: written in order, shown in order; the failed one named", lines)


def test_a_text_the_console_cannot_take_costs_only_itself():
    stalled = Stalled(refuse="═")
    console = L._UnblockedConsole(stalled, L._ConsoleQueue())
    console.write("0\n")
    assert stalled.entered.wait(5)
    for text in ("x\n", "═\n", "y\n"):                         # one batch, written as one
        console.write(text)
    stalled.release.set()
    console._pending.settle(5)
    assert "".join(stalled.got).startswith("0\nx\ny\n[console] 1 outputs dropped (UnicodeEncodeError"), \
        stalled.got


def test_a_broken_console_is_told_what_it_lost_once_it_works_again():
    class Broken:
        def __init__(self):
            self.healed, self.got = False, []

        def write(self, text):
            if not self.healed:
                raise OSError("Pipe kaputt")
            self.got.append(text)

        def flush(self):
            pass

    broken = Broken()
    console = L._UnblockedConsole(broken, L._ConsoleQueue())
    for text in ("a\n", "b\n", "c\n"):                          # each its own batch, each marker failing
        console.write(text)
        while console._pending._queue.unfinished_tasks:
            time.sleep(0.01)
    broken.healed = True
    console.write("d\n")
    console._pending.settle(5)
    assert broken.got[:2] == ["d\n", "[console] 3 outputs dropped (OSError: Pipe kaputt); the log lines"
                              " are in the log file under logs/\n"], ("the lines, not the failed markers", broken.got)


def test_a_busy_server_is_kept_up_with():
    """One write per batch, not per line: a writer that wrote and flushed each line kept up with some
    200 a second while the server was busy, behind the thousands it logs at its peaks."""
    class Counting:
        writes = 0

        def write(self, text):
            Counting.writes += 1
            time.sleep(0.005)                                    # a console's write: a few ms

        def flush(self):
            pass

    console = L._UnblockedConsole(Counting(), L._ConsoleQueue())
    start = time.monotonic()
    for i in range(3000):
        console.write(f"Zeile {i}\n")
    console._pending.settle(10)
    assert not console._pending._queue.unfinished_tasks and time.monotonic() - start < 5, Counting.writes
    assert Counting.writes < 300, ("batched", Counting.writes)


def test_the_servers_handlers_streams_and_crashes_go_through_it(monkeypatch):
    monkeypatch.setattr(L, "_unblocked", False)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)     # as under uvicorn, not pytest's
    monkeypatch.setattr(threading, "excepthook", threading.__excepthook__)
    monkeypatch.setattr(logging, "_warnings_showwarning", None)    # an earlier build_app's capture
    monkeypatch.setattr(warnings, "showwarning", warnings.showwarning)
    out, err = Stalled(), Stalled()
    for name, stream in (("stdout", out), ("__stdout__", out), ("stderr", err), ("__stderr__", err)):
        monkeypatch.setattr(sys, name, stream)
    logger = logging.getLogger("test_unblocked_console")
    handler, raw = logging.StreamHandler(err), logging.StreamHandler(out)
    for h in (handler, raw):
        logger.addHandler(h)
    monkeypatch.setattr(logger, "propagate", False)
    caught = []
    seen = logging.Handler(level=logging.WARNING)
    seen.emit = caught.append
    logging.getLogger().addHandler(seen)
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
        try:
            raise RuntimeError("der Absturz")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        crash = threading.Thread(target=lambda: 1 / 0, name="kaputt")
        crash.start(), crash.join()
        quiet = threading.Thread(target=sys.exit, name="endet")
        quiet.start(), quiet.join()
        with warnings.catch_warnings():                         # pytest's filters make it an error
            warnings.simplefilter("always")
            warnings.warn("veraltet", UserWarning, stacklevel=1)
        said = [(r.levelname, r.getMessage(), r.exc_info and r.exc_info[0]) for r in caught]
        assert ("CRITICAL", "Uncaught exception", RuntimeError) in said, "a crash reaches the log file"
        assert ("CRITICAL", "Uncaught exception in thread kaputt", ZeroDivisionError) in said, said
        assert not any("endet" in m for _, m, _ in said), "a thread's exit is no crash"
        assert any(r.name == "py.warnings" and "veraltet" in r.getMessage() for r in caught), said
    finally:
        logging.captureWarnings(False)
        logging.getLogger().removeHandler(seen)
        out.release.set(), err.release.set()
        for h in (handler, raw):
            logger.removeHandler(h)


def test_a_hook_of_someones_own_is_left_alone(monkeypatch):
    """pytest's thread hook fails a test whose thread crashed: a build_app in one test must not take
    that from all the tests after it."""
    monkeypatch.setattr(L, "_unblocked", False)
    mine = Stalled()
    mine.release.set()
    for name in ("stdout", "__stdout__", "stderr", "__stderr__"):
        monkeypatch.setattr(sys, name, mine)
    theirs = lambda *a: None                                     # noqa: E731
    monkeypatch.setattr(sys, "excepthook", theirs)
    monkeypatch.setattr(threading, "excepthook", theirs)
    try:
        L.unblock_console()
        assert sys.excepthook is theirs and threading.excepthook is theirs
    finally:
        logging.captureWarnings(False)
