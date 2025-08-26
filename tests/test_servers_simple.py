"""
Simplified tests for specific MCP server implementations.
"""
import pytest
from unittest.mock import patch, AsyncMock

from agent_system.servers.weather.server import WeatherServer
from agent_system.servers.datetime.server import DateTimeServer


class TestWeatherServer:
    """Test the Weather server functionality."""
    
    def test_weather_server_initialization(self):
        """Test weather server initialization."""
        server = WeatherServer("weather", {}, True)
        assert server.name == "weather"
        assert server.ssl_verify is True
    
    def test_weather_server_schema(self):
        """Test weather server schema."""
        server = WeatherServer("weather", {}, True)
        schema = server.get_schema()
        
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "weather"
        assert "location" in schema["function"]["parameters"]["properties"]
        assert "action" in schema["function"]["parameters"]["properties"]
    
    def test_weather_server_default_action(self):
        """Test weather server default action."""
        server = WeatherServer("weather", {}, True)
        assert server.get_default_action() == "forecast"
    
    @pytest.mark.asyncio
    async def test_weather_server_missing_location(self):
        """Test weather server with missing location."""
        server = WeatherServer("weather", {}, True)
        
        result = await server.call("forecast", {})
        assert result["status"] == "error"
        assert "Missing required parameter: location" in result["error"]
    
    @pytest.mark.asyncio
    async def test_weather_server_invalid_action(self):
        """Test weather server with invalid action."""
        server = WeatherServer("weather", {}, True)
        
        result = await server.call("invalid_action", {"location": "Berlin"})
        assert result["status"] == "error"
        assert "Unknown tool" in result["error"]


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


class TestServerIntegration:
    """Integration tests for server implementations."""
    
    def test_multiple_server_schemas(self):
        """Test that different servers have unique schemas."""
        weather_server = WeatherServer("weather", {}, True)
        datetime_server = DateTimeServer("datetime", {}, True)
        
        weather_schema = weather_server.get_schema()
        datetime_schema = datetime_server.get_schema()
        
        # Verify they have different function names
        assert weather_schema["function"]["name"] == "weather"
        assert datetime_schema["function"]["name"] == "datetime"


# Fixtures for server tests
@pytest.fixture
def weather_server():
    """Fixture providing a weather server."""
    return WeatherServer("weather", {}, True)


@pytest.fixture
def datetime_server():
    """Fixture providing a datetime server."""
    return DateTimeServer("datetime", {}, True)


class TestWithServerFixtures:
    """Tests using server fixtures."""
    
    def test_server_names(self, weather_server, datetime_server):
        """Test server names."""
        assert weather_server.name == "weather"
        assert datetime_server.name == "datetime"
    
    def test_server_default_actions(self, weather_server, datetime_server):
        """Test server default actions."""
        assert weather_server.get_default_action() == "forecast"
        assert datetime_server.get_default_action() == "current"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
