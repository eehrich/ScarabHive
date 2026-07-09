"""Ordering guarantee between set_context and a concurrently spawned sub-agent.

An LLM routinely emits `set_context(aufgabe=X)` and a sub-agent spawn in the
SAME turn; tool_execution runs those as concurrent asyncio tasks. The spawn
inherits the parent's LIVE tracker vars, so the set_context write must land
first — otherwise the sub-agent renders its prompt with the previous task.

This holds because set_context reaches its tracker write with ZERO awaits,
while any consumer yields at least once (the spawn does session I/O first).
These tests pin that invariant: a regression would be an `await` sneaking in
front of the write.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_system.config.models import MCPConfig
from plugins.task_switch.server import TaskSwitchServer


class FakeTracker:
    def __init__(self):
        self.vars = {}

    def get_session_template_vars(self, session_id):
        return dict(self.vars.get(session_id, {}))

    def set_session_template_vars(self, session_id, values):
        self.vars.setdefault(session_id, {}).update(values)


class FakeAgent:
    def __init__(self):
        self._session_tracker = FakeTracker()
        self._session_service = None  # persistence is skipped without it
        self.agent_config = MagicMock()


@pytest.fixture
def server():
    return TaskSwitchServer(
        "v6_workflow", MagicMock(), MCPConfig(type="task_switch", enabled=True))


def _set_context_source() -> str:
    return inspect.getsource(TaskSwitchServer.set_context)


class TestZeroAwaitBeforeWrite:
    """Static guard: no await may precede the tracker write on the happy path."""

    def test_no_await_before_tracker_write(self):
        tree = ast.parse(textwrap.dedent(_set_context_source()))
        func = tree.body[0]

        write_line = None
        for node in ast.walk(func):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set_session_template_vars"):
                write_line = node.lineno
                break
        assert write_line is not None, "tracker write not found — test needs updating"

        # Awaits before the write are only allowed inside the early-return error
        # branch (`if not context_vars: await status.error(...); return`).
        offenders = []
        for node in ast.walk(func):
            if isinstance(node, ast.Await) and node.lineno < write_line:
                snippet = ast.dump(node.value)
                if "status" in snippet and "error" in snippet:
                    continue  # error path returns before reaching the write
                offenders.append(node.lineno)

        assert not offenders, (
            f"await(s) at line offset {offenders} precede the tracker write. "
            f"This breaks the ordering guarantee against concurrently spawned "
            f"sub-agents — see the ORDERING INVARIANT comment in task_switch.")

    def test_source_documents_the_invariant(self):
        assert "ORDERING INVARIANT" in _set_context_source()


class TestConcurrentSpawnSeesNewValue:
    """Behavioural guard: a consumer that awaits even once reads the new value."""

    @pytest.mark.asyncio
    async def test_spawn_that_awaits_once_reads_new_value(self, server):
        agent = FakeAgent()
        agent._session_tracker.set_session_template_vars("parent", {"aufgabe": "Idee"})
        seen = {}

        async def spawn_like_sub_agent():
            # Mirrors create_sub_session: at least one await (session I/O)
            # before reading the parent's live tracker.
            await asyncio.sleep(0)
            seen["vars"] = agent._session_tracker.get_session_template_vars("parent")

        async def set_context():
            return await server.set_context({
                "_agent": agent, "_session_id": "parent", "aufgabe": "World"})

        # Both orders must give the same result — the LLM decides the order.
        for tasks in ([set_context(), spawn_like_sub_agent()],
                      [spawn_like_sub_agent(), set_context()]):
            seen.clear()
            agent._session_tracker.vars["parent"] = {"aufgabe": "Idee"}
            await asyncio.gather(*tasks)
            assert seen["vars"]["aufgabe"] == "World", (
                "sub-agent spawn inherited the STALE task")

    @pytest.mark.asyncio
    async def test_write_happens_before_first_yield(self, server):
        agent = FakeAgent()
        task = asyncio.create_task(server.set_context({
            "_agent": agent, "_session_id": "parent", "aufgabe": "World"}))
        # One loop tick: enough for a coroutine with zero awaits before the
        # write to have written.
        await asyncio.sleep(0)
        assert agent._session_tracker.get_session_template_vars(
            "parent") == {"aufgabe": "World"}
        await task


def test_invariant_comment_survives_in_file():
    src = Path("src/plugins/task_switch/server.py").read_text(encoding="utf-8")
    assert "do NOT introduce an `await` before the tracker" in src
