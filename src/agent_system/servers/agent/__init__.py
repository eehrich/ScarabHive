"""
Agent MCP Server - Base agent that can be used as an MCP Server by other agents.

This is the core Agent class that extends MCPServer, enabling direct agent-to-agent 
communication without wrapper classes.
"""

from .server import Agent

__all__ = ["Agent"]
