import pytest
from plugins.datetime.plugin import factory as datetime_factory
from plugins.datetime.server import DateTimeServer

def test_plugin_factory_creates_server():
    server = datetime_factory("datetime_test", cfg=None, ssl_verify=True)
    assert server is not None
    assert server.name == "datetime_test"


@pytest.mark.asyncio
async def test_current_action_returns_success():
    server = datetime_factory("datetime_test")
    result = await server.call("current", {"timezone": "UTC", "format": "iso"})
    assert isinstance(result, dict)
    assert result.get("status") == "success"
    assert "current_time" in result


@pytest.mark.asyncio
async def test_format_and_add_actions():
    server = datetime_factory("datetime_test")

    # Test formatting a known date
    r = await server.call("format", {"datetime": "2020-01-02T15:04:05", "format": "%Y/%m/%d"})
    assert r.get("status") == "success"
    assert r.get("formatted") == "2020/01/02"

    # Test adding days
    r2 = await server.call("add", {"datetime": "2020-01-01T00:00:00", "days": 1})
    assert r2.get("status") == "success"
    assert r2.get("result", "").startswith("2020-01-02")


class TestDateTimeServer:
    """Test the DateTime server functionality."""
    
    def test_datetime_server_initialization(self):
        """Test datetime server initialization."""
        server = DateTimeServer("datetime", {}, True)
        assert server.name == "datetime"
        assert server.ssl_verify is True
    
    def test_datetime_server_schema(self):
        """Test datetime server schema."""
        server = DateTimeServer("datetime", {}, True)
        schema = server.get_schema()
        
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "datetime"
        assert "action" in schema["function"]["parameters"]["properties"]
    
    def test_datetime_server_default_action(self):
        """Test datetime server default action."""
        server = DateTimeServer("datetime", {}, True)
        assert server.get_default_action() == "current"
    
    @pytest.mark.asyncio
    async def test_datetime_current(self):
        """Test current datetime functionality."""
        server = DateTimeServer("datetime", {}, True)
        
        result = await server.call("current", {})
        assert isinstance(result, dict)
        # Just check that it returns something, don't assume structure
    
    @pytest.mark.asyncio
    async def test_datetime_invalid_action(self):
        """Test datetime server with invalid action."""
        server = DateTimeServer("datetime", {}, True)
        
        result = await server.call("invalid_action", {})
        assert result["status"] == "error"
        assert "Unknown action" in result["error"]
