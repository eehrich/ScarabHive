"""
Simplified tests for the Weather MCP server implementation.
"""
import pytest

from plugins.weather.server import WeatherServer


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
