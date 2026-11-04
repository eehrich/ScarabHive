"""What create(blocking=false) tells the model has to match what rings: told it would be woken while no wake
was asked for ("false" as text), the agent ended its turn and the job's result reached nobody."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.sub_agent_manager import server as server_module
from plugins.sub_agent_manager.server import SubAgentManagerServer


async def _message(monkeypatch, asked):
    async def never_blocked(*args):
        return ""

    monkeypatch.setattr(server_module, "wake_blocked", lambda *args: "")
    server = SimpleNamespace(system_config=None, _never_woken=never_blocked)
    manager = SimpleNamespace(_extract_user_id=lambda *args: "alice")
    return await SubAgentManagerServer._async_started_message(server, {"wake_when_done": asked}, "parent", manager)


@pytest.mark.parametrize("asked", ["false", "no", "0", False, None])
async def test_no_wake_asked_is_not_promised(monkeypatch, asked):
    message = await _message(monkeypatch, asked)
    assert "poll or wait" in message and "woken" not in message


@pytest.mark.parametrize("asked", ["true", True])
async def test_a_wake_asked_is_promised(monkeypatch, asked):
    assert "this session is woken" in await _message(monkeypatch, asked)
