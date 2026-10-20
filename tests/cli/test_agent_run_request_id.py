"""agent-run --request-id: a caller names the run and finds its LLM calls again by that name."""
import sys

import pytest

from agent_system import agent_run


def test_the_request_id_reaches_the_run(monkeypatch):
    got = {}

    async def main_async(**kw):
        got.update(kw)
    monkeypatch.setattr(agent_run, "main_async", main_async)
    monkeypatch.setattr(sys, "argv", ["agent-run", "--request-id", "job-7-ab12", "hello"])
    agent_run.main()
    assert got["request_id"] == "job-7-ab12"
    monkeypatch.setattr(sys, "argv", ["agent-run", "hello"])
    agent_run.main()
    assert got["request_id"] is None


@pytest.mark.parametrize("bad", ["job_7", "", "a b", "x" * 65])
def test_an_id_a_sub_request_could_be_taken_for_is_refused(monkeypatch, bad):
    """Sub-requests are named "<id>_<n>": a root id with an underscore would read as another's child."""
    ran = []

    async def main_async(**kw):
        ran.append(kw)
    monkeypatch.setattr(agent_run, "main_async", main_async)
    monkeypatch.setattr(sys, "argv", ["agent-run", "--request-id", bad, "hello"])
    with pytest.raises(SystemExit):
        agent_run.main()
    assert not ran


async def test_the_run_goes_under_the_given_id(monkeypatch):
    seen = []

    async def collect(agent, request, request_id=None, **kw):
        seen.append(request_id)
        return {}
    monkeypatch.setattr("agent_system.servers.agent.result_utils.collect_final_result", collect)
    await agent_run.run_agent_request(object(), "x", "s1", request_id="job-7-ab12")
    await agent_run.run_agent_request(object(), "x", "s1")
    assert seen[0] == "job-7-ab12" and seen[1] and seen[1] != seen[0]
