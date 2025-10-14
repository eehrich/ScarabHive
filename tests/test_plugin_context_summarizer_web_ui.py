"""Tests for Context Summarizer plugin web UI."""
import pytest
from plugins.context_summarizer.plugin import PLUGIN_FACTORY
from plugins.context_summarizer.web_endpoints import ContextSummarizerWebFactory


def test_factory_returns_tuple():
    """Test that PLUGIN_FACTORY returns tuple of (hooks, web_factory)."""
    result = PLUGIN_FACTORY()
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_web_factory_type():
    """Test that second element is ContextSummarizerWebFactory."""
    _hooks_plugin, web_factory = PLUGIN_FACTORY()
    assert isinstance(web_factory, ContextSummarizerWebFactory)


def test_shared_history_list():
    """Test that hooks plugin and web factory share the same history list."""
    hooks_plugin, web_factory = PLUGIN_FACTORY()
    assert hooks_plugin.summarization_history is web_factory.summarization_history


@pytest.mark.asyncio
async def test_get_history_empty():
    """Test getting history when empty."""
    _hooks_plugin, web_factory = PLUGIN_FACTORY()
    result = await web_factory.get_history()
    assert result['success'] is True
    assert result['events'] == []


@pytest.mark.asyncio
async def test_get_stats_empty():
    """Test getting stats when empty."""
    _hooks_plugin, web_factory = PLUGIN_FACTORY()
    result = await web_factory.get_stats()
    assert result['success'] is True
    assert result['total_events'] == 0
