"""The API on a console of its own, out of sight, its output passed on to this one (Windows).

Started in a VS Code terminal, the API shared the terminal's console. When that console's host stopped
answering (07.10.2026, 01:04), the API froze whole: starting a process with pipes, CPython asks the console,
with the GIL held, whether a pipe is a console (io.open -> GetConsoleMode) -- the call never returned, and no
request, loop or thread ran again until the process was killed. A console of its own, which nobody has to
read, always answers; the server's output is read here as it comes, so a terminal that stops reading holds
up only the thread that writes to it.

    python -m agent_system.own_console uvicorn.main:main agent_system.app:build_app --factory --port 8000

agent-api goes this way by itself. Ctrl+C reaches the server as before: the first stops it, a second hurries
it; a third ends it outright. The server and what it starts die with this process (a job object), as they
did on the terminal's console when the task ended: closing the terminal, ending or restarting the task end
them at once -- a hung server too, whose port a restarted task needs. What is meant to outlive the server
(a woken run, a coding run) starts through popen_outliving. Elsewhere than on Windows there is no such
console to escape: the target runs in this process.
"""
from __future__ import annotations

import collections
import ctypes
import importlib
import os
import signal
import subprocess
import sys
import threading
import time

#: For the server: the pid of the process that started it on its console of its own.
LAUNCHER_ENV = "AGENT_OWN_CONSOLE_LAUNCHER"
#: For the server: "1" when what it writes ends in a terminal (the launcher's), so it formats for one.
TERMINAL_ENV = "AGENT_OWN_CONSOLE_TERMINAL"
#: How much of the server's output waits while the terminal does not read; older output is dropped.
BUFFER_BYTES = 1 << 20
_UTF8 = 65001
_SYNCHRONIZE, _INFINITE, _WAIT_TIMEOUT = 0x00100000, 0xFFFFFFFF, 0x102
_KILL_ON_JOB_CLOSE, _BREAKAWAY_OK, _ASSIGN_PROCESS = 0x2000, 0x0800, 0x0001
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_serving = False
_terminal = False


def needed() -> bool:
    """Whether a server about to start belongs on a console of its own first."""
    return os.name == "nt" and not _serving


def output_reaches_a_terminal() -> bool:
    """For the server: whether its stdout, a pipe, ends in the launcher's terminal."""
    return _terminal


def api() -> None:
    """agent-api: off to a console of its own before the app is even imported -- the launcher stays small."""
    if needed():
        sys.exit(relaunch("agent_system.app:run", []))
    from agent_system.app import run
    run()


def popen_outliving(*args, creationflags: int = 0, **kwargs) -> subprocess.Popen:
    """subprocess.Popen for a process meant to outlive the server: on Windows it leaves the job the server dies
    with; where a job above it forbids leaving, it starts as it did before there was one."""
    if os.name != "nt":
        return subprocess.Popen(*args, **kwargs)
    try:
        return subprocess.Popen(*args, creationflags=creationflags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except PermissionError:
        return subprocess.Popen(*args, creationflags=creationflags, **kwargs)


def _names(launcher_pid: int) -> tuple[str, str]:
    """The launcher's job and the semaphore it passes each Ctrl+C on with."""
    return f"Local\\agent-own-console-job-{launcher_pid}", f"Local\\agent-own-console-ctrl-c-{launcher_pid}"


class _JobLimits(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION; only LimitFlags is set."""
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def relaunch(target: str, args: list[str]) -> int:
    """Runs ``target`` ("module:function", called without arguments) with ``args`` as its command line, on a
    console of its own; returns its exit code."""
    import _winapi

    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = k32.CreateSemaphoreW.restype = ctypes.c_void_p
    job_name, ctrl_c_name = _names(os.getpid())
    # Joined by the server itself, before it starts anything: what it starts is then in it too.
    job = k32.CreateJobObjectW(None, job_name)
    limits = _JobLimits(LimitFlags=_KILL_ON_JOB_CLOSE | _BREAKAWAY_OK)
    if job:
        k32.SetInformationJobObject(ctypes.c_void_p(job), 9, ctypes.byref(limits), ctypes.sizeof(limits))
    ctrl_c = k32.CreateSemaphoreW(None, 0, 2, ctrl_c_name)  # counts: two Ctrl+C at once stay two
    terminal = sys.stdout.isatty()
    if terminal:
        import colorama
        colorama.just_fix_windows_console()  # the server's colours arrive as ANSI, as colorama.init() made them
    # -c, not -m: run as __main__, this module would exist twice, and its _serving with it. -P: -c puts the
    # working directory first on sys.path, where a yaml.py or a plugins/ of its own would shadow ours.
    server = subprocess.Popen(
        [sys.executable, "-P", "-c", "from agent_system.own_console import serve; serve()", target, *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
        # its output is passed on as bytes, to a console that reads them as UTF-8
        env={"PYTHONIOENCODING": "utf-8:replace", **os.environ, LAUNCHER_ENV: str(os.getpid()),
             TERMINAL_ENV: "1" if terminal else "0"})
    copier = threading.Thread(target=_copy, args=(server.stdout, sys.stdout.buffer), daemon=True)
    copier.start()
    presses = 0

    def on_ctrl_c(_signum, _frame) -> None:  # a handler, not KeyboardInterrupt: that one may land outside any try
        nonlocal presses
        presses += 1
        if presses <= 2:
            k32.ReleaseSemaphore(ctypes.c_void_p(ctrl_c), 1, None)  # stops it, then hurries it: uvicorn's two
        else:
            server.kill()  # the third: now

    previous = {sig: signal.signal(sig, on_ctrl_c) for sig in (signal.SIGINT, signal.SIGBREAK)}
    try:
        while True:
            try:  # wakes at once on Ctrl+C (the main thread's SIGINT event), at the latest every 0.5 s
                if _winapi.WaitForMultipleObjects([int(server._handle)], False, 500) != _WAIT_TIMEOUT:
                    break
            except InterruptedError:
                pass
        code = server.wait()
        copier.join(5)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        for handle in (ctrl_c, job):  # the job's last handle: whatever of the server is left goes with it
            if handle:
                k32.CloseHandle(ctypes.c_void_p(handle))
    return ctypes.c_int32(code).value  # Windows hands it over unsigned: 0xC000013A, not -1073741510


def _copy(source, target, limit: int = BUFFER_BYTES) -> None:
    """The server's output, read as it comes -- its pipe never fills, whatever the terminal does -- and written
    by this thread up to each line's end (a \\r too, so a progress bar moves), never into a character. While the
    terminal does not read, the newest ``limit`` bytes wait; older ones are dropped, and the terminal is told."""
    chunks: collections.deque[bytes] = collections.deque()
    waiting = dropped = 0
    ended = False
    ready = threading.Condition()

    def read() -> None:
        nonlocal waiting, dropped, ended
        for chunk in iter(lambda: source.read1(65536), b""):
            with ready:
                chunks.append(chunk)
                waiting += len(chunk)
                while waiting > limit and len(chunks) > 1:
                    old = chunks.popleft()
                    waiting -= len(old)
                    dropped += len(old)
                ready.notify()
        with ready:
            ended = True
            ready.notify()

    threading.Thread(target=read, name="own-console-read", daemon=True).start()
    pending = bytearray()
    while True:
        with ready:
            ready.wait_for(lambda: chunks or ended)
            data, lost, last = b"".join(chunks), dropped, ended
            chunks.clear()
            waiting = dropped = 0
        if lost:  # what is left starts somewhere in a line: from its next line end on
            start = min((i for i in (data.find(b"\n"), data.find(b"\r")) if i >= 0), default=len(data) - 1) + 1
            lost += len(pending) + start
            pending, data = bytearray(), data[start:]
            _write(target, f"[own_console: {lost} bytes dropped while the terminal did not read]\n".encode())
        cut = max(data.rfind(b"\n"), data.rfind(b"\r")) + 1
        if cut:
            _write(target, bytes(pending + data[:cut]))
            pending = bytearray(data[cut:])
        else:
            pending += data
        if last:
            if pending:
                _write(target, bytes(pending))
            return


def _write(target, data: bytes) -> None:
    try:
        target.write(data)
        target.flush()
    except (OSError, ValueError):
        pass  # the terminal is gone: read on, the server must never wait on a full pipe


def serve() -> None:
    """The server's side: in the launcher's job before anything starts, its console speaking UTF-8 like the
    terminal's did (what the programs it starts write to their pipes depends on it), each Ctrl+C the launcher
    passes on arriving as one; then the target runs."""
    global _serving, _terminal
    _serving = True
    _terminal = os.environ.pop(TERMINAL_ENV, "") == "1"  # its own business: not for the processes it starts
    launcher = int(os.environ.pop(LAUNCHER_ENV))
    job_name, ctrl_c_name = _names(launcher)
    k32 = ctypes.windll.kernel32
    k32.OpenJobObjectW.restype = k32.GetCurrentProcess.restype = ctypes.c_void_p
    job = k32.OpenJobObjectW(_ASSIGN_PROCESS, False, job_name)
    if not (job and k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(k32.GetCurrentProcess()))):
        print(f"own_console: this server may outlive its launcher (error {ctypes.GetLastError()})",
              file=sys.stderr, flush=True)
    if job:
        k32.CloseHandle(ctypes.c_void_p(job))  # the launcher holds the job; it ends when the launcher does
    k32.SetConsoleCP(_UTF8)
    k32.SetConsoleOutputCP(_UTF8)
    threading.Thread(target=_ctrl_c_from, args=(ctrl_c_name,), name="own-console-ctrl-c", daemon=True).start()
    target, args = sys.argv[1], sys.argv[2:]
    module, _, function = target.partition(":")
    sys.argv = [module, *args]
    getattr(importlib.import_module(module), function)()


def _ctrl_c_from(name: str) -> None:
    k32 = ctypes.windll.kernel32
    k32.OpenSemaphoreW.restype = ctypes.c_void_p
    semaphore = k32.OpenSemaphoreW(_SYNCHRONIZE, False, name)
    if not semaphore:  # the launcher is gone already
        signal.raise_signal(signal.SIGINT)
        return
    while k32.WaitForSingleObject(ctypes.c_void_p(semaphore), _INFINITE) == 0:
        signal.raise_signal(signal.SIGINT)
        time.sleep(0.1)  # apart, as a console's arrive: two at once would reach the server's handler as one


def main() -> None:
    if len(sys.argv) < 2 or ":" not in sys.argv[1]:
        sys.exit("usage: python -m agent_system.own_console module:function [args...]")
    target, args = sys.argv[1], sys.argv[2:]
    if needed():
        sys.exit(relaunch(target, args))
    module, _, function = target.partition(":")
    sys.argv = [module, *args]
    getattr(importlib.import_module(module), function)()


if __name__ == "__main__":
    main()
