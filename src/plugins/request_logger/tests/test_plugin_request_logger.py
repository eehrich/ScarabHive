"""request_logger driven through the real HookRegistry: every hook gets its own
copy of a fresh context, so these tests catch state that only seems to carry
over when a handler is called directly."""
import logging
from types import SimpleNamespace

import pytest

from agent_system.hooks import HookContext, HookType
from agent_system.hooks.registry import HookRegistry
from agent_system.llm.models import ChatMessage
from agent_system.plugins.discovery import register_plugin_hooks
from plugins.request_logger.plugin import PLUGIN_FACTORY

LOGGER = "plugins.request_logger.hooks"


async def _registry(config):
    plugin = PLUGIN_FACTORY("request_logger", None, SimpleNamespace(config=config))
    registry = HookRegistry()
    await register_plugin_hooks("request_logger", plugin, plugin.get_schema_data(), registry=registry)
    return registry


async def _run(registry, session_id, request_id, calls=1, answer="the answer"):
    messages = [ChatMessage(role="user", content="hello there")]
    for step in range(1, calls + 1):
        await registry.execute_hooks(HookType.PRE_LLM_CALL, HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id=request_id, session_id=session_id,
            agent_name="a", messages=messages, step=step))
        # The agent loop's result shape: the text sits under "assistant".
        response = {"assistant": {"role": "assistant", "content": answer}, "usage": {}}
        await registry.execute_hooks(HookType.POST_LLM_CALL, HookContext(
            hook_type=HookType.POST_LLM_CALL, request_id=request_id, session_id=session_id,
            agent_name="a", messages=messages, llm_response=response, step=step))
    await registry.execute_hooks(HookType.SESSION_END, HookContext(
        hook_type=HookType.SESSION_END, request_id=request_id, session_id=session_id, agent_name="a"))


def _lines(caplog, level=None):
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and (level is None or r.levelno == level)]


@pytest.mark.asyncio
async def test_post_line_carries_number_duration_and_answer(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    registry = await _registry({})
    await _run(registry, "s1", "r1")
    post = [line for line in _lines(caplog) if "Post-LLM" in line]
    assert len(post) == 1
    assert post[0].startswith("[RequestLogger] Request #1 - Post-LLM Call: duration=")
    assert post[0].endswith("response_preview=the answer")


@pytest.mark.asyncio
async def test_run_end_counts_the_calls_of_that_run_only(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    registry = await _registry({})
    await _run(registry, "s1", "r1", calls=2)
    await _run(registry, "s1", "r2", calls=1)
    ends = [line for line in _lines(caplog) if "Run Ended" in line]
    assert len(ends) == 2
    assert "llm_calls=2, duration=" in ends[0]
    # The second run of the session has a duration of its own, too.
    assert "llm_calls=1, duration=" in ends[1]


@pytest.mark.asyncio
async def test_log_level_setting_is_applied(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    registry = await _registry({"log_level": "WARNING"})
    await _run(registry, "s1", "r1")
    assert len(_lines(caplog)) == 4
    assert len(_lines(caplog, logging.WARNING)) == 4


@pytest.mark.asyncio
async def test_preview_length_is_clamped(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    registry = await _registry({"max_content_preview": -1})
    await _run(registry, "s1", "r1", answer="x" * 50)
    post = [line for line in _lines(caplog) if "Post-LLM" in line][0]
    assert post.endswith("response_preview=" + "x" * 10 + "...")


@pytest.mark.asyncio
async def test_content_off_logs_no_text(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    registry = await _registry({"log_message_content": False})
    await _run(registry, "s1", "r1")
    text = "\n".join(_lines(caplog))
    assert "hello there" not in text and "the answer" not in text
    assert "Pre-LLM Call" in text


@pytest.mark.asyncio
async def test_open_runs_are_bounded_oldest_dropped_first(caplog, monkeypatch):
    from plugins.request_logger import hooks
    monkeypatch.setattr(hooks, "_MAX_RUNS", 2)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    plugin = PLUGIN_FACTORY("request_logger", None, SimpleNamespace(config={}))
    for request_id in ("r1", "r2", "r3"):  # three runs whose session_end never came
        await plugin.log_pre_llm(HookContext(
            hook_type=HookType.PRE_LLM_CALL, request_id=request_id, session_id="s1",
            agent_name="a", messages=[]))
    assert list(plugin._runs) == [("s1", "r2"), ("s1", "r3")]
