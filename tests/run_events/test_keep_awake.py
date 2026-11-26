"""The machine stays awake while this process runs agent work (agent_system.core.keep_awake).

A laptop in standby stops the agent with it: this morning's run of a phase stood still for 90 minutes over three
naps of the machine, and looked hung to its person. Every run this process holds keeps it awake, its end lets go.
"""
from __future__ import annotations

import pytest

from agent_system import run_events
from agent_system.core import keep_awake
from agent_system.core.keep_awake import (ES_CONTINUOUS, ES_DISPLAY_REQUIRED, ES_SYSTEM_REQUIRED, Keeper,
                                          flags_for)
from agent_system.run_events import RunEventBus, RunEventStore, RunReporter

AWAKE = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED


class _System:
    """Takes the execution states the keeper sets, as SetThreadExecutionState would."""

    def __init__(self) -> None:
        self.said: list[int] = []

    def __call__(self, flags: int) -> int:
        self.said.append(flags)
        return ES_CONTINUOUS


@pytest.fixture
def system():
    told = _System()
    keeper = Keeper(setter=told, how=lambda: keep_awake.DISPLAY)
    keep_awake.use_keeper(keeper)
    yield told
    keep_awake.use_keeper(None)


@pytest.fixture
def store(tmp_path):
    store = RunEventStore(tmp_path / "run_events.db")
    run_events.use_run_events(store, RunEventBus())
    yield store
    run_events.use_run_events(None)


def test_the_states_for_each_mode():
    assert flags_for(True, keep_awake.DISPLAY) == AWAKE
    assert flags_for(True, keep_awake.SYSTEM) == ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    assert flags_for(True, keep_awake.OFF) == ES_CONTINUOUS
    assert flags_for(False, keep_awake.DISPLAY) == ES_CONTINUOUS, "nothing held: the machine may sleep again"


def test_the_last_run_through_lets_the_machine_sleep(system):
    keeper = keep_awake.keeper()
    keeper.hold("a")
    keeper.hold("b")
    keeper.hold("a")
    keeper.settle()
    assert system.said == [AWAKE], "one state for any number of runs"
    keeper.release("a")
    keeper.settle()
    assert system.said == [AWAKE]
    keeper.release("b")
    keeper.release("b")
    keeper.settle()
    assert system.said == [AWAKE, ES_CONTINUOUS]
    assert keeper.held == 0


def test_off_holds_nothing(monkeypatch):
    told = _System()
    keeper = Keeper(setter=told, how=lambda: keep_awake.OFF)
    keeper.hold("a")
    keeper.release("a")
    keeper.settle()
    assert told.said == []


@pytest.mark.parametrize("said, mode", [("", keep_awake.DISPLAY), ("system", keep_awake.SYSTEM),
                                        ("off", keep_awake.OFF), ("0", keep_awake.OFF),
                                        ("whatever", keep_awake.DISPLAY)])
def test_the_mode_as_the_environment_says(monkeypatch, said, mode):
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")
    monkeypatch.setenv(keep_awake.ENV, said)
    assert keep_awake.mode() == mode


def test_nothing_is_held_off_windows(monkeypatch):
    monkeypatch.setattr(keep_awake.sys, "platform", "linux")
    monkeypatch.setenv(keep_awake.ENV, "display")
    assert keep_awake.mode() == keep_awake.OFF


def test_a_setter_that_fails_never_reaches_the_run(system):
    def broken(flags: int) -> int:
        raise OSError("no such call")

    keeper = Keeper(setter=broken, how=lambda: keep_awake.DISPLAY)
    keeper.hold("a")
    keeper.release("a")
    keeper.settle()


async def test_a_run_holds_the_machine_awake_from_its_start_to_its_end(system, store):
    run = RunReporter("r1", "developer", who="alice")
    await run.started()
    keep_awake.keeper().settle()
    assert system.said == [AWAKE]
    await run.step("builds")
    await run.done("built")
    keep_awake.keeper().settle()
    assert system.said == [AWAKE, ES_CONTINUOUS]

    await run.started()   # the same run begins again: held again
    keep_awake.keeper().settle()
    assert system.said == [AWAKE, ES_CONTINUOUS, AWAKE]
    run.end_detached("run.cancelled").join(5)
    keep_awake.keeper().settle()
    assert system.said == [AWAKE, ES_CONTINUOUS, AWAKE, ES_CONTINUOUS]


async def test_a_run_whose_start_was_not_kept_holds_nothing(system, store, monkeypatch):
    """Its start not written, nobody ends it: held, the machine would never sleep again."""
    monkeypatch.setattr(store, "append", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    run = RunReporter("r1", "developer", who="alice")
    assert await run.started() is None
    keep_awake.keeper().settle()
    assert system.said == [] and keep_awake.keeper().held == 0
