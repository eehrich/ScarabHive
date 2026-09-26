"""Inside a run, the run's user is the user of the calls it makes.

An activity's decision reaches the hooks without an agent (llm/hook_notify.py)
under the run's own id, which nobody registered: the message debugger kept it
as nobody's, and its user never saw it. The run names its user where it names
its request id.
"""
from __future__ import annotations

import textwrap

import pytest

from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, Harness

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

MACHINE = {
    "m.yaml": textwrap.dedent("""\
        stategraph: 1
        id: m
        python: m.py
        context: {user: null}
        initial: a
        states:
          a:
            do: {call: run_user}
            transitions:
              - target: done
                effect: ctx.user = out
          done: {type: final, output: "{{ ctx.user }}"}
        """),
    "m.py": textwrap.dedent("""\
        def run_user():
            from agent_system.core.request_context import current_run_user
            return current_run_user.get()
        """),
}


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


@pytest.mark.parametrize("user_id", ["alice", None])
async def test_inside_a_run_the_user_is_the_runs(harness, user_id):
    row = await harness.run(MACHINE, user_id=user_id)

    assert row["status"] == "succeeded", row["error"]
    assert (row["output"] or None) == user_id
