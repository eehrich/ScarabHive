"""
Agent Tool Server - Base agent that can be used as a Tool server by other agents.

This is the core Agent class that extends ToolServer, enabling direct agent-to-agent 
communication without wrapper classes.

Two agent base classes are provided:
- Agent: Core agent functionality (LLM, tools, conversation management)
- SchemaBasedAgent: Agent with automatic schema.yaml loading (recommended for most plugins)
"""

from .server import Agent
from .schema_based import SchemaBasedAgent

__all__ = ["Agent", "SchemaBasedAgent"]
