"""The machine stays awake while this process runs agent work.

A laptop that goes to standby while an agent works stops the work with it: no step, no heartbeat, a lease that runs
out -- and to its person the run looks slow, or hung, for as long as the lid was shut or the screen was off. On
Windows with Modern Standby the screen turning off for idleness is enough: the machine sleeps as it goes dark.

Every run this process holds (``run_events.RunReporter``: a panel's task, a chat's turn, a sub-agent, background
work) holds the machine awake from its start to its end; when the last one ends the machine may sleep again. Done on
Windows through ``SetThreadExecutionState``, by one thread of its own (the state belongs to the thread that set it).
Elsewhere nothing is done.

``AGENT_KEEP_AWAKE``:
  ``display`` (the default on Windows) -- the system and the screen stay on: on a Modern Standby machine the screen
  going dark is what puts it to sleep. The screen still locks as configured.
  ``system`` -- the system stays on, the screen may go dark (enough where standby is S3, not Modern Standby).
  ``off`` -- nothing is held.

Nothing keeps a machine awake whose lid is shut (as its power settings say) or whose battery saver puts it to sleep.
A keeper never raises into the run that holds it: what fails is logged once, and the run goes on.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from typing import Callable, Hashable, Optional

logger = logging.getLogger(__name__)

ENV = "AGENT_KEEP_AWAKE"
OFF, SYSTEM, DISPLAY = "off", "system", "display"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def mode() -> str:
    """What is held while a run works, as ``AGENT_KEEP_AWAKE`` says: off, system or display -- display on Windows
    when it says nothing (or something it does not know), off elsewhere."""
    if sys.platform != "win32":
        return OFF
    said = os.environ.get(ENV, "").strip().lower()
    if said in ("0", "off", "false", "no"):
        return OFF
    if said == SYSTEM:
        return SYSTEM
    return DISPLAY


def flags_for(held: bool, how: str) -> int:
    """The execution state for ``held`` runs in mode ``how``: ES_CONTINUOUS alone lets the machine sleep again."""
    if not held or how == OFF:
        return ES_CONTINUOUS
    return ES_CONTINUOUS | ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if how == DISPLAY else 0)


def _windows_setter() -> Optional[Callable[[int], int]]:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        call = ctypes.windll.kernel32.SetThreadExecutionState
        call.argtypes = [ctypes.c_uint]
        call.restype = ctypes.c_uint
        return call
    except Exception:  # noqa: BLE001 - no such call here: nothing is held
        logger.warning("keep_awake: SetThreadExecutionState is not available; the machine may sleep during runs")
        return None


class Keeper:
    """Counts the runs that hold the machine awake and says so to the system -- from one thread of its own, which
    lives as long as the process does once something was held. ``setter``: what sets the state (tests)."""

    def __init__(self, setter: Optional[Callable[[int], int]] = None, how: Optional[Callable[[], str]] = None):
        self._setter = setter
        self._setter_known = setter is not None
        self._how = how or mode
        self._holders: set[Hashable] = set()
        self._lock = threading.Lock()
        self._wanted: "queue.Queue[int]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._said = ES_CONTINUOUS
        self._failed = False

    @property
    def held(self) -> int:
        with self._lock:
            return len(self._holders)

    def hold(self, key: Hashable) -> None:
        """``key`` works: the machine stays awake until it is released. Holding twice holds once."""
        self._change(key, add=True)

    def release(self, key: Hashable) -> None:
        """``key`` is through; the last one released lets the machine sleep again. Unknown keys change nothing."""
        self._change(key, add=False)

    def _change(self, key: Hashable, *, add: bool) -> None:
        try:
            with self._lock:
                before = bool(self._holders)
                if add:
                    self._holders.add(key)
                else:
                    self._holders.discard(key)
                after = bool(self._holders)
                if before == after:
                    return
                how = self._how()
                if how == OFF and after:
                    return
                self._start()
                self._wanted.put(flags_for(after, how))
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("keep_awake: the machine's state was not changed")

    def _start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._serve, name="keep-awake", daemon=True)
            self._thread.start()

    def _serve(self) -> None:
        if not self._setter_known:
            self._setter, self._setter_known = _windows_setter(), True
        while True:
            flags = self._wanted.get()
            try:
                if flags != self._said and self._setter is not None:
                    if not self._setter(flags) and not self._failed:
                        self._failed = True
                        logger.warning("keep_awake: SetThreadExecutionState(%#x) refused", flags)
                    self._said = flags
            except Exception:  # noqa: BLE001 - see the module docstring
                if not self._failed:
                    self._failed = True
                    logger.exception("keep_awake: the machine's state was not set")
            finally:
                self._wanted.task_done()

    def settle(self) -> None:
        """Wait until the system was told what is wanted now (tests)."""
        if self._thread is not None:
            self._wanted.join()


_KEEPER = Keeper()


def keeper() -> Keeper:
    return _KEEPER


def use_keeper(other: Optional[Keeper]) -> None:
    """Point the process at another keeper -- for tests; None makes a new default one."""
    global _KEEPER
    _KEEPER = other if other is not None else Keeper()


def hold(key: Hashable) -> None:
    _KEEPER.hold(key)


def release(key: Hashable) -> None:
    _KEEPER.release(key)
