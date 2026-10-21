"""has_session_data: the chat's "Open in" menu offers the context engineer for a session once the panel would show
something of it -- a compaction, a stored tool result, an archived message, a core memory fact. The stores are
written through the hook's own components, which create their files for every session they see: a directory with
empty stores holds nothing."""
import asyncio
from types import SimpleNamespace

import pytest


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    from plugins.context_engineer.plugin import PLUGIN_FACTORY
    from plugins.context_engineer.server import ContextEngineerServer

    monkeypatch.setattr(ContextEngineerServer, "_load_history", lambda self: None)  # data/context_engineer stays unread
    plugin = PLUGIN_FACTORY("context_engineer", SimpleNamespace(),
                            SimpleNamespace(config={"storage_path": str(tmp_path)}, hook_config={}, name="context_engineer"))
    plugin.server._hooks_impl.history_callback = None  # and unwritten
    return plugin


def test_empty_stores_hold_nothing_and_each_store_holds_data(plugin, tmp_path):
    hooks = plugin.server._hooks_impl
    for session_id in ("s-empty", "s-tool", "s-archive", "s-fact"):
        hooks._get_session_components(session_id)
    assert (tmp_path / "s-empty" / "tool_results.db").exists(), "the hook created no stores -- this would be vacuous"

    hooks._get_session_components("s-tool")["tool_store"].store_and_reference("call_1", "read_file", "line\n" * 400)
    hooks._get_session_components("s-archive")["archival_memory"].store({"role": "user", "content": "An old message"})
    asyncio.run(hooks._handle_store_fact(fact="Prefers Python", category="preferences", importance=0.8,
                                         session_id="s-fact"))

    assert plugin.has_session_data("s-empty", "ada") is False
    assert plugin.has_session_data("s-nowhere", "ada") is False
    assert plugin.has_session_data("s-tool", "ada") is True
    assert plugin.has_session_data("s-archive", "ada") is True
    assert plugin.has_session_data("s-fact", "ada") is True


def test_a_compaction_of_the_session_holds_data(plugin):
    # in the shape the hook appends after a compaction (hooks.py, "stats_history.append")
    plugin.server.stats_history.append({"timestamp": 0.0, "session_id": "s-compacted", "tokens_saved": 100})

    assert plugin.has_session_data("s-compacted", "ada") is True
    assert plugin.has_session_data("s-other", "ada") is False
