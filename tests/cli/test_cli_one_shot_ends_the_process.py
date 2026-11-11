"""`agent-cli run` once (and a woken run): the process ends with the run, and background work goes with it."""
from agent_system.cli_utils.commands import one_shot
from agent_system.core.session_presence import wake


def test_a_one_shot_run_tells_background_work_that_the_process_ends_with_it(monkeypatch):
    """A job the run starts in the background is told to poll, not to wait for a wake -- for the run only."""
    during = []

    async def collect(agent, task, **kwargs):
        during.append(wake._ENDS_WITH_ITS_RUN)
        return {"summary": "done"}

    monkeypatch.setattr("agent_system.servers.agent.result_utils.collect_final_result", collect)
    result = one_shot.run_one_shot(object(), "a task", one_shot.RunControl("rid1"), "s1", raw=True, verbose=False,
                                   show_tools=False, show_status=False, llm_override=None, llm_profile_info=None)
    assert result == {"summary": "done"}
    assert during == [True]
    assert wake._ENDS_WITH_ITS_RUN is False
