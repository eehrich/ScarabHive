"""The API on a console of its own (agent_system.own_console), driven with real processes.

A stuck terminal console froze the whole API on 07.10.2026: CPython asked it, with the GIL held, whether a
pipe is a console. The server must therefore not share the terminal's console, a terminal that stops reading
must not hold it up through its pipe either, its output must still reach the terminal formatted for one,
Ctrl+C must reach it as before, and it must die with its launcher with all it started -- also a server that
holds its GIL, whose port a restarted task needs -- save what is meant to outlive it.
"""
from __future__ import annotations

import ast
import ctypes
import logging
import os
import signal
import subprocess
import sys
import threading
import time

import psutil
import pytest

from agent_system import own_console
from agent_system.utils.logging import setup_logging

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the console of its own is Windows'")

TARGETS = '''
import ctypes, os, subprocess, sys, time
from pathlib import Path

def mark(text, suffix=""):
    path = Path(os.environ["SERVING_MARK"] + suffix)
    path.with_name(path.name + ".tmp").write_text(text)
    os.replace(path.with_name(path.name + ".tmp"), path)  # whole, or not there

def report():
    from agent_system.utils.logging import stdout_is_terminal
    k32 = ctypes.windll.kernel32
    pids = (ctypes.c_uint32 * 64)()
    count = k32.GetConsoleProcessList(pids, 64)
    print(f"cp={k32.GetConsoleOutputCP()} console={sorted(pids[:count])} argv={sys.argv[1:]} "
          f"terminal={stdout_is_terminal()} leaks={os.environ.get('AGENT_OWN_CONSOLE_TERMINAL')} grüße", flush=True)
    sys.exit(3)

def wait_for_stop():
    mark(str(os.getpid()))
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("stopped as on Ctrl+C", flush=True)

def stop_slowly():
    import atexit
    atexit.register(lambda: print("atexit ran", flush=True))
    mark(str(os.getpid()))
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("stopping", flush=True)
        mark("", ".stopping")
    try:
        while True:  # a slow shutdown
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("hurried", flush=True)

def ignore_ctrl_c():
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    mark(str(os.getpid()))
    while True:
        time.sleep(0.1)

def hold_the_gil():
    mark(str(os.getpid()))
    ctypes.PyDLL("kernel32").Sleep(0xFFFFFFFF)  # as in GetConsoleMode on 07.10.: no thread of it runs again

def start_children():
    from agent_system import own_console
    sleep = [sys.executable, "-c", "import time; time.sleep(120)"]
    # as session_presence.spawn_wake and coding_cli start theirs
    woken = own_console.popen_outliving(sleep, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                                        | subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
    shell = subprocess.Popen(sleep, stdin=subprocess.DEVNULL)  # as the terminal plugin starts its shells
    mark(f"{woken.pid} {shell.pid}", ".children")
    mark(str(os.getpid()))
    while True:
        time.sleep(0.1)

def end_as_on_ctrl_c():
    os._exit(-1073741510)  # 0xC000013A, how Windows ends a process on Ctrl+C
'''


@pytest.fixture
def targets(tmp_path, monkeypatch):
    (tmp_path / "console_targets.py").write_text(TARGETS, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(tmp_path), os.environ.get("PYTHONPATH")])))
    mark = tmp_path / "serving"
    monkeypatch.setenv("SERVING_MARK", str(mark))
    return mark


def _our_console() -> list[int]:
    pids = (ctypes.c_uint32 * 64)()
    return list(pids[:ctypes.windll.kernel32.GetConsoleProcessList(pids, 64)])


def _report(capfd) -> tuple[int, str]:
    code = own_console.relaunch("console_targets:report", ["--port", "1"])
    out = capfd.readouterr().out
    line = next((line for line in out.splitlines() if line.startswith("cp=")), None)
    assert line, f"the server's output did not come here:\n{out}"
    return code, line


def test_the_server_runs_on_a_console_of_its_own_and_its_output_comes_here(targets, capfd):
    if os.getpid() not in _our_console():
        pytest.skip("this test process has no console to share")
    code, line = _report(capfd)
    assert code == 3, "its exit code"
    assert "argv=['--port', '1']" in line and line.endswith("grüße")
    assert line.startswith("cp=65001 "), "its console speaks UTF-8, as the terminal's did"
    console = ast.literal_eval(line.split("console=", 1)[1].split(" argv=", 1)[0])
    assert os.getpid() not in console, "the server shares this console"
    assert "terminal=False" in line, "this test's stdout is no terminal"


def test_the_working_directory_shadows_no_module_of_the_server(targets, capfd, tmp_path, monkeypatch):
    shadowing = tmp_path / "elsewhere"
    shadowing.mkdir()
    (shadowing / "console_targets.py").write_text("def report():\n    print('cp=SHADOWED', flush=True)\n")
    monkeypatch.chdir(shadowing)  # agent-api may be started anywhere (paths.enter_project)
    _, line = _report(capfd)
    assert "SHADOWED" not in line


def test_a_server_whose_output_ends_in_a_terminal_formats_for_one(targets, capfd, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    _, line = _report(capfd)
    assert "terminal=True" in line, "the log lines lose the logger's name and their colours"
    assert "leaks=None" in line, "the processes the server starts would format their files for a terminal"


@pytest.fixture
def setup_logging_restored():
    """setup_logging, with the root's handlers and the loggers it sets given back afterwards."""
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    touched = {name: (logging.getLogger(name).level, logging.getLogger(name).propagate,
                      list(logging.getLogger(name).filters))
               for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpcore", "httpx", "asyncio")}
    yield setup_logging
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in before:
            handler.close()
    for handler in before:
        root.addHandler(handler)
    root.setLevel(level)
    for name, (lvl, propagate, filters) in touched.items():
        logger = logging.getLogger(name)
        logger.level, logger.propagate, logger.filters = lvl, propagate, filters


def test_the_server_logs_for_the_terminal_its_output_reaches(setup_logging_restored, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)  # a pipe, as the server's stdout is
    monkeypatch.setattr(own_console, "_terminal", True)  # what serve() takes from the launcher
    setup_logging_restored(True, "INFO", str(tmp_path / "api.log"), rotation_enabled=False)
    console = [handler for handler in logging.getLogger().handlers
               if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)]
    assert len(console) == 1, "setup_logging's console handler"
    line = console[0].format(logging.makeLogRecord(
        {"name": "agent_system.runtime", "levelno": logging.INFO, "levelname": "INFO", "msg": "hello"}))
    assert "agent_system.runtime" in line, "the logger's name is gone"
    assert "\x1b[" in line, "no colours"


class _Pipe:
    """The server's end of the pipe: hands out ``chunks``; with ``hold``, waits after the first one."""

    def __init__(self, chunks, hold: threading.Event | None = None):
        self.chunks, self.hold, self.handed = list(chunks), hold, 0

    def read1(self, _size):
        if self.hold is not None and self.handed == 1:
            self.hold.wait(10)
        self.handed += 1
        return self.chunks.pop(0) if self.chunks else b""


class _Terminal:
    def __init__(self, stuck: threading.Event | None = None):
        self.writes: list[bytes] = []
        self.stuck = stuck

    def write(self, data):
        if self.stuck is not None:
            self.stuck.wait(10)  # a console host that does not answer
        self.writes.append(data)

    def flush(self):
        pass


def test_output_is_passed_on_at_each_line_end_never_inside_a_character():
    terminal = _Terminal()
    own_console._copy(_Pipe([b"100%\r\ngr\xc3", b"\xbc\xc3\x9fe\n", b"no end"]), terminal)
    assert b"".join(terminal.writes) == b"100%\r\ngr\xc3\xbc\xc3\x9fe\nno end"
    assert all(write.endswith((b"\n", b"\r")) for write in terminal.writes[:-1]), terminal.writes
    assert terminal.writes[-1] == b"no end", "a line is written before its end"


def test_a_progress_bar_moves_before_its_line_ends():
    hold, terminal = threading.Event(), _Terminal()
    copier = threading.Thread(target=own_console._copy, args=(_Pipe([b"50%\r", b"100%\n"], hold), terminal))
    copier.start()
    deadline = time.monotonic() + 5
    while not terminal.writes and time.monotonic() < deadline:
        time.sleep(0.01)
    seen = list(terminal.writes)
    hold.set()
    copier.join(5)
    assert seen == [b"50%\r"]


def test_a_terminal_that_stops_reading_never_fills_the_servers_pipe():
    stuck = threading.Event()
    terminal = _Terminal(stuck)
    pipe = _Pipe([b"x" * 999 + b"\n"] * 3000)  # 3 MB, while the terminal reads nothing
    copier = threading.Thread(target=own_console._copy, args=(pipe, terminal), daemon=True)
    copier.start()
    deadline = time.monotonic() + 5
    while pipe.chunks and time.monotonic() < deadline:
        time.sleep(0.01)
    drained = not pipe.chunks
    stuck.set()
    copier.join(10)
    assert drained, "the pipe was not read while the terminal did not: the server would block on it"
    out = b"".join(terminal.writes)
    assert b"bytes dropped while the terminal did not read" in out
    assert len(out) < 2 * own_console.BUFFER_BYTES and out.endswith(b"x\n")


def _relaunch_pressing_ctrl_c(target: str, mark, *waits, gap: float = 1) -> tuple[int, dict]:
    """relaunch(target), with one Ctrl+C here once each of ``waits`` (a file) exists, ``gap`` after the one
    before. A server still running after 25 s is killed, and says so."""
    relaunched, seen = threading.Event(), {"presses": 0}

    def press() -> None:
        for number, wait in enumerate(waits, 1):
            deadline = time.monotonic() + 30
            while not wait.exists() and not relaunched.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(3 if number == 1 else gap)  # a server stopping by itself is not one Ctrl+C stopped
            seen.setdefault("serving", not relaunched.is_set())
            if relaunched.is_set():
                return
            signal.raise_signal(signal.SIGINT)  # as the console's Ctrl+C: sets the main thread's SIGINT event
            seen["presses"] = number

    def safety() -> None:
        if not relaunched.wait(25) and mark.exists():
            seen["killed by the test"] = True
            psutil.Process(int(mark.read_text())).kill()

    pytests = signal.signal(signal.SIGINT, lambda *_: None)  # a Ctrl+C after relaunch returned hits nothing
    presser = threading.Thread(target=press, daemon=True)
    presser.start()
    threading.Thread(target=safety, daemon=True).start()
    try:
        code = own_console.relaunch(target, [])
    finally:
        relaunched.set()
        presser.join(5)
        signal.signal(signal.SIGINT, pytests)
    return code, seen


def test_ctrl_c_stops_the_server_as_ctrl_c_would(targets, capfd):
    code, seen = _relaunch_pressing_ctrl_c("console_targets:wait_for_stop", targets, targets)
    out = capfd.readouterr().out
    assert seen.get("serving"), f"the server did not stay up until Ctrl+C:\n{out}"
    assert "stopped as on Ctrl+C" in out and not seen.get("killed by the test")
    assert code == 0


def test_a_second_ctrl_c_hurries_the_server_and_it_still_exits_on_its_own(targets, capfd):
    code, seen = _relaunch_pressing_ctrl_c("console_targets:stop_slowly", targets,
                                           targets, targets.with_name("serving.stopping"))
    out = capfd.readouterr().out
    assert "stopping" in out and "hurried" in out, out
    assert "atexit ran" in out, "ended outright: what atexit flushes is lost"
    assert not seen.get("killed by the test") and code == 0


def test_two_quick_ctrl_c_stay_two(targets, capfd):
    code, seen = _relaunch_pressing_ctrl_c("console_targets:stop_slowly", targets, targets, targets, gap=0.1)
    out = capfd.readouterr().out
    assert "hurried" in out, f"the second Ctrl+C was lost:\n{out}"
    assert not seen.get("killed by the test") and code == 0


def test_two_ctrl_c_passed_on_at_once_reach_the_server_as_two():
    """Both already waiting when the server's thread wakes -- it was held up, say: raised back to back, Python
    would run its handler once."""
    k32 = ctypes.windll.kernel32
    k32.CreateSemaphoreW.restype = ctypes.c_void_p
    name = f"Local\\agent-own-console-test-{os.getpid()}"
    semaphore = k32.CreateSemaphoreW(None, 2, 2, name)
    received = []
    pytests = signal.signal(signal.SIGINT, lambda *_: received.append(1))
    try:
        threading.Thread(target=own_console._ctrl_c_from, args=(name,), daemon=True).start()
        deadline = time.monotonic() + 3
        while len(received) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
    finally:
        k32.WaitForSingleObject(ctypes.c_void_p(semaphore), 0)  # what it did not take raises nothing later
        signal.signal(signal.SIGINT, pytests)
        k32.CloseHandle(ctypes.c_void_p(semaphore))
    assert len(received) == 2


def test_a_third_ctrl_c_ends_a_server_that_does_not_stop(targets, capfd):
    code, seen = _relaunch_pressing_ctrl_c("console_targets:ignore_ctrl_c", targets, targets, targets, targets)
    assert seen["presses"] == 3
    assert not seen.get("killed by the test"), "the third Ctrl+C did not end it"
    assert code != 0


def test_a_windows_exit_code_is_passed_on_signed(targets, capfd):
    assert own_console.relaunch("console_targets:end_as_on_ctrl_c", []) == -1073741510


def _killing_the_launcher(target: str, mark, within: float, then=None) -> None:
    """Starts ``target`` through a launcher of its own, kills that launcher outright, as VS Code ends a task
    (every process on the terminal's console at once), and waits for the server to die."""
    launcher = subprocess.Popen([sys.executable, "-m", "agent_system.own_console", target],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    server = None
    try:
        deadline = time.monotonic() + 30
        while not mark.exists():
            assert time.monotonic() < deadline and launcher.poll() is None, "the server never served"
            time.sleep(0.05)
        server = psutil.Process(int(mark.read_text()))
        time.sleep(3)
        assert server.is_running(), "the server stopped by itself"
        launcher.kill()
        server.wait(timeout=within)
        if then:
            then()
    except psutil.TimeoutExpired:
        pytest.fail("the server outlived its launcher")
    finally:
        launcher.kill()
        if server is not None and server.is_running():
            server.kill()


def test_a_launcher_killed_outright_takes_the_server_down(targets):
    _killing_the_launcher("console_targets:wait_for_stop", targets, within=15)


def test_a_server_holding_its_gil_dies_with_its_launcher_too(targets):
    _killing_the_launcher("console_targets:hold_the_gil", targets, within=10)


def test_what_the_server_starts_dies_with_it_save_what_is_meant_to_outlive_it(targets):
    children = targets.with_name("serving.children")
    pids: list[int] = []

    def check() -> None:
        pids.extend(int(pid) for pid in children.read_text().split())
        time.sleep(1)
        woken, shell = (psutil.pid_exists(pid) for pid in pids)
        assert woken, "a woken run died with the server"
        assert not shell, "a terminal shell outlived the server it belongs to"

    try:
        _killing_the_launcher("console_targets:start_children", targets, within=10, then=check)
        assert pids, "the children were never checked"
    finally:
        for pid in pids:
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()


def test_the_runs_meant_to_outlive_the_server_start_through_popen_outliving(monkeypatch, tmp_path):
    from plugins.coding_cli import run as coding_cli_run

    started = []
    monkeypatch.setattr(own_console, "popen_outliving", lambda *args, **kwargs: started.append(args[0]))
    for name in ("stdin", "stdout", "stderr"):
        (tmp_path / name).write_text("")
    coding_cli_run.launch(["claude"], tmp_path, {}, tmp_path / "stdin", tmp_path / "stdout", tmp_path / "stderr")
    assert started == [["claude"]], "a coding run dies with the server"
    # spawn_wake in a process of its own: the suite's conftest refuses to wake for real
    probe = ("import subprocess, sys; from agent_system import own_console; "
             "from agent_system.core import session_presence as presence; "
             "presence.process._wake_log = lambda: subprocess.DEVNULL; "
             "own_console.popen_outliving = lambda *args, **kwargs: print('outliving') or sys.exit(0); "
             "presence.spawn_wake('s', 'u', 0)")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert result.stdout.split() == ["outliving"], f"a woken run dies with the server:\n{result.stderr}"


def test_agent_api_relaunches_before_it_builds_the_app(monkeypatch):
    from agent_system import app as app_mod
    started = []
    monkeypatch.setattr(own_console, "relaunch", lambda target, args: started.append((target, args)) or 0)
    monkeypatch.setattr(app_mod, "build_app", lambda *args, **kwargs: pytest.fail("the launcher built the app"))
    with pytest.raises(SystemExit) as exited:
        app_mod.run()  # `python -m agent_system.app`
    assert exited.value.code == 0
    assert started == [("agent_system.app:run", [])]


def test_agent_api_leaves_the_app_to_the_server():
    probe = ("import sys; from agent_system import own_console; "
             "own_console.relaunch = lambda target, args: print(target, 'agent_system.app' in sys.modules) or 0; "
             "own_console.api()")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert result.stdout.split() == ["agent_system.app:run", "False"], result.stderr
