"""A woken run's task reaches the model and the transcript as the RUN speaking.

Everything else about the wake is measured on the two producers (the CLI and
the chat) or on one consumer at a time. This drives a real Agent over one, from
`run_events` to the persist, because the step in between is where the role can
be quietly dropped: the task is appended verbatim only while it arrives as a
ChatMessage, and rebuilding it -- the branch right next to it does exactly that
-- would put the wake back into the history as something a person typed, with
every test above still green.
"""
import pytest

from agent_system.cli_utils.agent_runner import wake_message
from agent_system.llm.message_roles import DEVELOPER
from test_agent_max_steps_final_call import _LLM, _run


@pytest.mark.asyncio
async def test_the_wake_keeps_its_role_through_the_run():
    llm = _LLM(text_steps=True)
    task = wake_message()

    _, _, stored = await _run(llm, max_steps=1, session="wake_task", task=task)

    asked = llm.seen[0][-1]
    assert asked.content == task.content, "the model was asked something else"
    assert asked.role == DEVELOPER, "the model was told a person typed this"
    assert [msg.role for msg in stored if msg.content == task.content] == [DEVELOPER], \
        "the transcript kept it as something a person typed"
