"""
Agent Server - MCP Server implementations for agent-to-agent communication.

Since Agent already extends MCPServer, agents can be used directly as MCP servers
without needing wrapper classes.
"""

from ...agent.core import Agent
from .web_research import WebResearchAgent

__all__ = ["Agent", "WebResearchAgent"]
