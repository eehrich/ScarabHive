import pytest
from unittest.mock import AsyncMock, patch

from agent_system.mcp.adapters import MCPAdapter, BaseMCPAdapter
from agent_system.mcp.status import StatusEvent, status_bus


class TestMCPAdapter:
    """Test the abstract MCPAdapter interface."""

    def test_abstract_methods(self):
        """Test that MCPAdapter defines the expected abstract methods."""
        # This should raise TypeError since we can't instantiate abstract class
        with pytest.raises(TypeError):
            MCPAdapter("test")


class TestBaseMCPAdapter:
    """Test the BaseMCPAdapter implementation."""

    def test_init(self):
        """Test BaseMCPAdapter initialization."""
        config = {"timeout": 60.0, "other": "value"}
        adapter = BaseMCPAdapter("test_adapter", config)

        assert adapter.name == "test_adapter"
        assert adapter.config == config
        assert adapter._timeout == 60.0

    def test_init_default_config(self):
        """Test BaseMCPAdapter with default config."""
        adapter = BaseMCPAdapter("test_adapter")

        assert adapter.name == "test_adapter"
        assert adapter.config == {}
        assert adapter._timeout == 30.0

    @pytest.mark.asyncio
    async def test_get_auth_headers(self):
        """Test default auth headers implementation."""
        adapter = BaseMCPAdapter("test_adapter")
        headers = await adapter.get_auth_headers()
        assert headers == {}

    @pytest.mark.asyncio
    async def test_get_timeout(self):
        """Test timeout retrieval."""
        adapter = BaseMCPAdapter("test_adapter", {"timeout": 45.0})
        timeout = await adapter.get_timeout()
        assert timeout == 45.0

    @pytest.mark.asyncio
    async def test_publish_status(self):
        """Test status publishing through the bus."""
        adapter = BaseMCPAdapter("test_adapter")

        # Mock the status bus publish
        with patch.object(status_bus, 'publish', new_callable=AsyncMock) as mock_publish:
            event = StatusEvent(
                server="server1",
                request_id="req1",
                message="test message"
            )
            await adapter.publish_status(event)

            mock_publish.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_subscribe_status(self):
        """Test status subscription through the bus."""
        adapter = BaseMCPAdapter("test_adapter")

        # Mock the status bus subscribe
        mock_queue = AsyncMock()
        with patch.object(status_bus, 'subscribe', new_callable=AsyncMock, return_value=mock_queue) as mock_subscribe:
            queue = await adapter.subscribe_status(server="server1", request_id="req1")

            mock_subscribe.assert_called_once_with(server="server1", request_id="req1")
            assert queue == mock_queue

    # Abstract methods that should raise NotImplementedError
    @pytest.mark.asyncio
    async def test_abstract_publish_context(self):
        """Test that abstract methods raise NotImplementedError."""
        adapter = BaseMCPAdapter("test_adapter")

        with pytest.raises(NotImplementedError):
            await adapter.publish_context({})

    @pytest.mark.asyncio
    async def test_abstract_request_model(self):
        """Test that abstract methods raise NotImplementedError."""
        adapter = BaseMCPAdapter("test_adapter")

        with pytest.raises(NotImplementedError):
            await adapter.request_model({})

    @pytest.mark.asyncio
    async def test_abstract_list_capabilities(self):
        """Test that abstract methods raise NotImplementedError."""
        adapter = BaseMCPAdapter("test_adapter")

        with pytest.raises(NotImplementedError):
            await adapter.list_capabilities()

    @pytest.mark.asyncio
    async def test_abstract_health_check(self):
        """Test that abstract methods raise NotImplementedError."""
        adapter = BaseMCPAdapter("test_adapter")

        with pytest.raises(NotImplementedError):
            await adapter.health_check()
