from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
import asyncio

from .status import StatusEvent, status_bus
from ..config import AgentConfig

class MCPAdapter(ABC):
    """Abstract base class for MCP adapters.

    Adapters provide a unified interface for interacting with MCP servers,
    whether local or remote.
    """

    def __init__(self, name: str, config: AgentConfig):
        self.name = name
        self.config = config or {}

    @abstractmethod
    async def publish_context(self, context: Dict[str, Any]) -> None:
        """Publish context to the MCP server."""
        pass

    @abstractmethod
    async def request_model(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Send a model request to the MCP server."""
        pass

    @abstractmethod
    async def list_capabilities(self) -> List[str]:
        """List the capabilities of the MCP server."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the MCP server is healthy."""
        pass

    @abstractmethod
    async def get_auth_headers(self) -> Dict[str, str]:
        """Get authentication headers for requests."""
        pass

    @abstractmethod
    async def get_timeout(self) -> float:
        """Get the timeout for requests in seconds."""
        pass

    # Status-related methods
    @abstractmethod
    async def publish_status(self, event: StatusEvent) -> None:
        """Publish a status event."""
        pass

    @abstractmethod
    async def subscribe_status(
        self,
        server: Optional[str] = None,
        request_id: Optional[str] = None
    ) -> asyncio.Queue:
        """Subscribe to status events with optional filtering."""
        pass


class BaseMCPAdapter(MCPAdapter):
    """Base implementation of MCPAdapter with common functionality."""

    def __init__(self, name: str, config: AgentConfig):
        super().__init__(name, config)
        self._timeout = self.config.get('timeout', 30.0)

    async def publish_context(self, context: Dict[str, Any]) -> None:
        """Default implementation - does nothing."""
        raise NotImplementedError()

    async def request_model(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Default implementation - returns error."""
        raise NotImplementedError()

    async def list_capabilities(self) -> List[str]:
        """Default implementation - returns empty list."""
        raise NotImplementedError()

    async def health_check(self) -> bool:
        """Default implementation - returns True."""
        raise NotImplementedError()

    async def get_auth_headers(self) -> Dict[str, str]:
        """Default implementation - no auth."""
        return {}

    async def get_timeout(self) -> float:
        """Return configured timeout."""
        return self._timeout

    async def publish_status(self, event: StatusEvent) -> None:
        """Publish status event via the global status bus."""
        # Ensure sequence is set if it's 0
        if event.sequence == 0:
            # Let the bus assign sequence automatically
            pass
        await status_bus.publish(event)

    async def subscribe_status(
        self,
        server: Optional[str] = None,
        request_id: Optional[str] = None
    ) -> asyncio.Queue:
        """Subscribe to status events via the global status bus."""
        return await status_bus.subscribe(server=server, request_id=request_id)
