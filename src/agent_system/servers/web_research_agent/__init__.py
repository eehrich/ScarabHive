"""
WebResearchAgent MCP Server - Specialized agent for web research tasks.

This agent extends the base Agent class and provides specialized research capabilities
including fact-checking and source comparison.
"""

from .server import WebResearchAgent, create_web_research_agent

__all__ = ["WebResearchAgent", "create_web_research_agent"]
