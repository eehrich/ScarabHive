"""duckduckgo_search: the ddgs client is patched, nothing reaches the network."""
import sys
from unittest.mock import AsyncMock, patch

import pytest

from agent_system.plugins.cache import PluginCache
from plugins.duckduckgo_search.server import DuckDuckGoSearchServer

HITS = [
    {"title": "Result 1", "href": "https://example.com/1", "body": "snippet 1"},
    {"title": "Result 2", "href": "https://example.com/2", "body": "snippet 2"},
]


@pytest.fixture
def server(mock_system_config, mock_server_config, tmp_path):
    srv = DuckDuckGoSearchServer("ddg", mock_system_config, mock_server_config)
    # PluginCache persists to data/cache/<plugin>/ on disk; a test must not
    # read what another test (or a real run) left there.
    srv.cache = PluginCache("duckduckgo_search", cache_dir=tmp_path)
    return srv


def _ddgs(text):
    """Patch the ddgs client so ``DDGS().text(...)`` is ``text``."""
    return patch("ddgs.DDGS", **{"return_value.text": text})


def test_schema_has_one_prefixed_search_tool(server):
    tools = server.get_tools()
    assert [t["function"]["name"] for t in tools] == ["ddg_web_search"]
    assert {"query", "max_results"} <= set(tools[0]["function"]["parameters"]["properties"])


async def test_empty_query_is_refused_without_a_request(server):
    with _ddgs(AsyncMock()) as client:
        result = await server.call("web_search", {"query": "   ", "_status": AsyncMock()})
    assert result["error"] == "Empty query" and result["results"] == []
    client.assert_not_called()


async def test_unknown_tool_raises(server):
    with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
        await server.call("invalid_tool", {"query": "x", "_status": AsyncMock()})


async def test_results_come_back_and_the_end_line_counts_them(server):
    status = AsyncMock()
    with _ddgs(lambda query, max_results: HITS):
        result = await server.call("web_search", {"query": "test", "max_results": 2, "_status": status})
    assert result["engine"] == "duckduckgo" and result["results"] == HITS
    line = status.end.call_args.args[0]
    assert "2 results" in line and "test" in line


async def test_a_second_identical_query_is_served_from_the_cache(server):
    calls = []

    def text(query, max_results):
        calls.append(query)
        return HITS

    with _ddgs(text):
        await server.call("web_search", {"query": "Cached", "_status": AsyncMock()})
        status = AsyncMock()
        again = await server.call("web_search", {"query": "cached ", "_status": status})
    assert calls == ["Cached"], "the second call must not reach DuckDuckGo"
    assert again["results"] == HITS
    assert "(cached)" in status.end.call_args.args[0]


async def test_an_empty_answer_is_not_cached(server):
    """A DuckDuckGo hiccup that returns nothing must not be remembered for
    fifteen minutes -- the next call has to ask again."""
    answers = iter([[], HITS])
    with _ddgs(lambda query, max_results: next(answers)):
        first = await server.call("web_search", {"query": "flaky", "_status": AsyncMock()})
        second = await server.call("web_search", {"query": "flaky", "_status": AsyncMock()})
    assert first["results"] == []
    assert second["results"] == HITS


async def test_a_rate_limit_is_retried_and_then_succeeds(server):
    from ddgs.exceptions import RatelimitException
    answers = iter([RatelimitException("429"), HITS])

    def text(query, max_results):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with _ddgs(text), patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await server.call("web_search", {"query": "busy", "_status": AsyncMock()})
    assert result["results"] == HITS
    assert sleep.await_count == 1


async def test_a_persistent_rate_limit_ends_as_an_error_not_a_raise(server):
    from ddgs.exceptions import RatelimitException

    def text(query, max_results):
        raise RatelimitException("429")

    status = AsyncMock()
    with _ddgs(text), patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await server.call("web_search", {"query": "busy", "_status": status})
    assert "RatelimitException" in result["error"] and result["results"] == []
    assert sleep.await_count == 2, "three attempts, two pauses between them"
    status.error.assert_awaited_once()


async def test_zero_hits_is_an_answer_not_an_error(server):
    """ddgs raises instead of returning an empty list. Its sentinel message
    means every engine answered and none had a hit -- a truthful zero, which
    the agent must not read as a broken tool, and no reason to retry."""
    from ddgs.exceptions import DDGSException
    attempts = []

    def text(query, max_results):
        attempts.append(1)
        raise DDGSException("No results found.")

    status = AsyncMock()
    with _ddgs(text), patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await server.call("web_search", {"query": "zzz", "_status": status})
    assert result["results"] == [] and "error" not in result
    assert len(attempts) == 1 and sleep.await_count == 0
    assert "0 results" in status.end.call_args.args[0]
    status.error.assert_not_awaited()


async def test_every_engine_failing_is_retried_not_reported_as_zero_hits(server):
    """The same exception type also carries the last engine's own error when
    all of them failed. That is a hiccup, so it gets the retries -- and if it
    persists it is an error, never a silent 'nothing found'."""
    from ddgs.exceptions import DDGSException
    answers = iter([DDGSException("Error in engine duckduckgo: ConnectionError"), HITS])

    def text(query, max_results):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with _ddgs(text), patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await server.call("web_search", {"query": "flaky engine", "_status": AsyncMock()})
    assert result["results"] == HITS and sleep.await_count == 1


def test_factory_builds_a_named_server(mock_system_config, mock_server_config):
    from plugins.duckduckgo_search.plugin import PLUGIN_FACTORY
    assert PLUGIN_FACTORY("custom_ddg", mock_system_config, mock_server_config).name == "custom_ddg"


async def test_a_missing_package_is_an_error_not_zero_hits(server, monkeypatch):
    """The predecessor package answered an unusable install with an EMPTY list
    and no exception -- a search that finds nothing looks like a search that
    ran. A missing package must therefore never come back as a result."""
    monkeypatch.setitem(sys.modules, "ddgs", None)  # `from ddgs import ...` raises
    with pytest.raises(RuntimeError, match="pip install ddgs"):
        await server.call("web_search", {"query": "test", "_status": AsyncMock()})
