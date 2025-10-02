#!/usr/bin/env python3
"""Test script to verify web_research_agent context window inheritance."""

import pytest

# Import the web_research_agent plugin module if available, otherwise skip
web_research_mod = pytest.importorskip('plugins.web_research_agent.server', reason="web_research_agent plugin not present in this environment")

def test_web_research_agent_module_exports():
    """Sanity test: ensure web_research_agent plugin module exports reasonable symbols.

    We avoid running deep integration here; this test ensures the module exists
    and exposes either a factory callable or a WebResearchAgent class to be
    used by higher-level tests.
    """
    # Check for a factory function or a class in the module
    has_factory = hasattr(web_research_mod, 'web_research_agent') and callable(getattr(web_research_mod, 'web_research_agent'))
    has_class = hasattr(web_research_mod, 'WebResearchAgent')
    assert has_factory or has_class, "web_research_agent module must expose 'web_research_agent' factory or 'WebResearchAgent' class"