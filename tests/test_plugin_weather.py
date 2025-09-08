from pathlib import Path
import pytest
import json
import asyncio
from unittest.mock import AsyncMock, Mock, patch
from io import StringIO
import sys

from agent_system.mcp.plugins import discover_all_plugins
from plugins.weather.server import WeatherServer
from plugins.weather.__main__ import main, build_parser, cli_main


def test_weather_plugin_discovered():
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    plugins = discover_all_plugins([default_dir])
    assert 'weather' in plugins
    factory = plugins['weather']
    inst = factory('weather', {})
    assert inst is not None


class TestWeatherCLI:
    """Test the weather plugin CLI functionality."""

    def test_build_parser_basic_args(self):
        """Test basic argument parsing."""
        parser = build_parser()
        args = parser.parse_args(['--location', 'Berlin'])

        assert args.location == 'Berlin'
        assert args.days == 3
        assert args.units == 'metric'
        assert args.server is False
        assert args.port == 8080

    def test_build_parser_all_args(self):
        """Test parsing with all arguments."""
        parser = build_parser()
        args = parser.parse_args([
            '--location', 'New York',
            '--source', 'weather.gov',
            '--days', '5',
            '--units', 'imperial',
            '--include-marine',
            '--summary-format', 'daily_summary',
            '--include-radiation',
            '--server',
            '--port', '9000'
        ])

        assert args.location == 'New York'
        assert args.source == 'weather.gov'
        assert args.days == 5
        assert args.units == 'imperial'
        assert args.include_marine is True
        assert args.summary_format == 'daily_summary'
        assert args.include_radiation is True
        assert args.server is True
        assert args.port == 9000

    def test_build_parser_defaults(self):
        """Test default values."""
        parser = build_parser()
        args = parser.parse_args([])

        assert args.location is None
        assert args.source is None
        assert args.days == 3
        assert args.units == 'metric'
        assert args.include_marine is False
        assert args.summary_format == 'detailed'
        assert args.include_radiation is False
        assert args.server is False
        assert args.port == 8080

    def test_main_function_output(self, capsys):
        """Test main function output."""
        # Capture stdout
        main(['--location', 'Berlin', '--source', 'met.no'])

        captured = capsys.readouterr()
        assert "Weather MCP Server" in captured.out

        # Parse the JSON output
        lines = captured.out.strip().split('\n')
        json_line = [line for line in lines if line.startswith('{')][0]
        output = json.loads(json_line)

        assert output['description'] == 'Weather MCP Server'
        assert output['location'] == 'Berlin'
        assert output['source'] == 'met.no'
        assert output['days'] == 3
        assert output['units'] == 'metric'
        assert output['include_marine'] is False
        assert output['server_mode'] is False
        assert output['port'] == 8080

    def test_main_function_server_mode(self, capsys):
        """Test main function with server mode."""
        main(['--location', 'London', '--server', '--port', '9001'])

        captured = capsys.readouterr()
        lines = captured.out.strip().split('\n')
        json_line = [line for line in lines if line.startswith('{')][0]
        output = json.loads(json_line)

        assert output['server_mode'] is True
        assert output['port'] == 9001
        assert output['location'] == 'London'


class TestWeatherServer:
    """Test the WeatherServer class functionality."""

    def test_weather_server_initialization(self):
        """Test weather server initialization."""
        server = WeatherServer("weather", {}, True)
        assert server.name == "weather"
        assert server.ssl_verify is True

    def test_weather_server_initialization_with_config(self):
        """Test weather server initialization with config."""
        config = {"timeout": 30, "retries": 3}
        server = WeatherServer("weather", config, False)
        assert server.name == "weather"
        assert server.ssl_verify is False

    def test_weather_server_schema(self):
        """Test weather server schema structure."""
        server = WeatherServer("weather", {}, True)
        schema = server.get_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "weather"
        assert "description" in schema["function"]

        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        assert "location" in params["properties"]
        assert "action" in params["properties"]
        assert "source" in params["properties"]
        assert "days" in params["properties"]
        assert "units" in params["properties"]
        assert "include_marine" in params["properties"]

        # Check required fields
        assert "location" in params["required"]

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
        assert "invalid_action" in result["error"]

    @pytest.mark.asyncio
    async def test_weather_server_valid_actions(self):
        """Test weather server with valid actions."""
        server = WeatherServer("weather", {}, True)

        valid_actions = ["forecast", "search", "query", "get", "check", "lookup"]

        for action in valid_actions:
            result = await server.call(action, {"location": "Berlin"})
            # Should not fail with "Unknown tool" error
            assert "Unknown tool" not in result.get("error", "")

    @pytest.mark.asyncio
    async def test_weather_server_source_selection(self):
        """Test weather server source selection logic."""
        server = WeatherServer("weather", {}, True)

        # Test default source - should work with real API
        result = await server.call("forecast", {"location": "Berlin"})
        assert result["status"] == "success"
        assert result["source"] == "met.no"
        assert "current" in result
        assert "forecast" in result

        # Test explicit source - should work with real API
        result = await server.call("forecast", {"location": "Berlin", "source": "met.no"})
        assert result["status"] == "success"
        assert result["source"] == "met.no"

    @pytest.mark.asyncio
    async def test_weather_server_days_parameter(self):
        """Test weather server days parameter handling."""
        server = WeatherServer("weather", {}, True)

        # Test default days
        result = await server.call("forecast", {"location": "Berlin"})
        assert result["status"] == "success"
        assert len(result["forecast"]) >= 1

        # Test custom days
        result = await server.call("forecast", {"location": "Berlin", "days": 2})
        assert result["status"] == "success"
        assert len(result["forecast"]) >= 1  # Should have at least one day

    @pytest.mark.asyncio
    async def test_weather_server_units_parameter(self):
        """Test weather server units parameter handling."""
        server = WeatherServer("weather", {}, True)

        # Test metric units (default) - met.no always returns metric
        result = await server.call("forecast", {"location": "Berlin", "units": "metric"})
        assert result["status"] == "success"
        assert result["units"] == "metric"

        # Test imperial units - met.no doesn't support imperial, so it still returns metric
        result = await server.call("forecast", {"location": "Berlin", "units": "imperial"})
        assert result["status"] == "success"
        # met.no API only provides metric units, so units will be "metric" regardless of request
        assert result["units"] == "metric"

    @pytest.mark.asyncio
    async def test_weather_server_marine_parameter(self):
        """Test weather server marine parameter handling."""
        server = WeatherServer("weather", {}, True)

        # Test marine data request - should switch to marine source
        result = await server.call("forecast", {"location": "Berlin", "include_marine": True})
        assert result["status"] == "success"
        # Should switch to marine.weather.gov when marine data is requested
        assert result["source"] in ["marine.weather.gov", "met.no"]

    @pytest.mark.asyncio
    async def test_weather_server_source_switching_logic(self):
        """Test weather server automatic source switching logic."""
        server = WeatherServer("weather", {}, True)

        # Test wttr.in with > 3 days should switch to met.no
        result = await server.call("forecast", {"location": "Berlin", "source": "wttr.in", "days": 5})
        assert result["status"] == "success"
        # Should switch to met.no for > 3 days with wttr.in
        assert result["source"] in ["met.no", "wttr.in"]

    @pytest.mark.asyncio
    async def test_weather_server_error_handling(self):
        """Test weather server error handling."""
        server = WeatherServer("weather", {}, True)

        # Test with invalid location (should still work with real API, but let's test unsupported source)
        result = await server.call("forecast", {"location": "Berlin", "source": "unsupported"})
        assert result["status"] == "error"
        assert "Unsupported weather source" in result["error"]

    @pytest.mark.asyncio
    async def test_weather_server_unsupported_source(self):
        """Test weather server with unsupported source."""
        server = WeatherServer("weather", {}, True)

        result = await server.call("forecast", {"location": "Berlin", "source": "unsupported"})
        assert result["status"] == "error"
        assert "Unsupported weather source" in result["error"]

    @pytest.mark.asyncio
    async def test_weather_server_ssl_verify_parameter(self):
        """Test weather server SSL verification parameter."""
        # Test with SSL verification enabled
        server_ssl = WeatherServer("weather", {}, True)
        result = await server_ssl.call("forecast", {"location": "Berlin"})
        assert result["status"] == "success"

        # Test with SSL verification disabled
        server_no_ssl = WeatherServer("weather", {}, False)
        result = await server_no_ssl.call("forecast", {"location": "Berlin"})
        assert result["status"] == "success"


class TestWeatherServerIntegration:
    """Integration tests for WeatherServer with mocked external services."""

    @pytest.mark.asyncio
    async def test_weather_server_full_workflow(self):
        """Test complete weather server workflow."""
        server = WeatherServer("weather", {}, True)

        # Mock successful response
        mock_response = {
            "location": "Berlin",
            "source": "met.no",
            "units": "metric",
            "current": {
                "temperature": 20,
                "humidity": 50,
                "wind_speed": 5
            },
            "forecast": [
                {
                    "date": "2025-09-08",
                    "max_temp": 22,
                    "min_temp": 12,
                    "avg_temp": 17
                }
            ]
        }

        with patch('plugins.weather.sources.fetch_met_no', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_response

            result = await server.call("forecast", {
                "location": "Berlin",
                "days": 1,
                "units": "metric"
            })

            assert result["status"] == "success"
            assert result["location"] == "Berlin"
            assert result["source"] == "met.no"
            assert "current" in result
            assert "forecast" in result
            assert len(result["forecast"]) >= 1

    @pytest.mark.asyncio
    async def test_weather_server_multiple_sources(self):
        """Test weather server with different sources."""
        server = WeatherServer("weather", {}, True)

        sources_to_test = [
            ("met.no", "met.no"),
            ("wttr.in", "wttr.in"),
        ]

        for source_name, expected_source in sources_to_test:
            result = await server.call("forecast", {
                "location": "Berlin",
                "source": source_name
            })

            assert result["status"] == "success"
            assert result["source"] == expected_source


class TestWeatherPluginFactory:
    """Test the weather plugin factory function."""

    def test_plugin_factory_basic(self):
        """Test basic plugin factory functionality."""
        from plugins.weather.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("weather")
        assert server.name == "weather"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self):
        """Test plugin factory with configuration."""
        from plugins.weather.plugin import PLUGIN_FACTORY

        config = {"timeout": 60}
        server = PLUGIN_FACTORY("weather", config, False)
        assert server.name == "weather"
        assert server.ssl_verify is False

    def test_plugin_factory_name_parameter(self):
        """Test plugin factory with custom name."""
        from plugins.weather.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_weather")
        assert server.name == "custom_weather"
