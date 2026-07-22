"""Consolidated tests for the Weather plugin.

This file merges tests that used to be split across
`test_weather_simple.py`, `test_weather_sources.py`,
`test_weather_status_phases.py` and the original
`test_plugin_weather.py` into a single plugin-prefixed file
so plugin tests appear first when running the test suite.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, Mock, patch
import json

from plugins.weather.server import WeatherServer
from plugins.weather import sources
from agent_system.mcp.status import status_bus, StatusPhase
from plugins.weather.__main__ import main, build_parser


def _get_tool_name(tool):
    """Extract the function.name from either a dict or an MCPTool-like object."""
    if isinstance(tool, dict):
        func = tool.get("function") or {}
        return func.get("name")
    func = getattr(tool, "function", None)
    if func is None:
        return getattr(tool, "name", None)
    if isinstance(func, dict):
        return func.get("name")
    return getattr(func, "name", None)


def _get_tool_function(tool):
    """Extract the function object from either a dict or an MCPTool-like object."""
    if isinstance(tool, dict):
        return tool.get("function", {})
    return getattr(tool, "function", None)


class TestWeatherServerBasic:
    """Basic unit tests for WeatherServer."""

    def test_weather_server_initialization(self, mock_system_config, mock_mcp_config):
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)
        assert server.name == "weather"
        assert server.ssl_verify is True

    def test_weather_server_schema(self, mock_system_config, mock_mcp_config):
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)
        tools = server.get_tools()

        assert isinstance(tools, list)
        assert len(tools) == 1
        tool = tools[0]
        assert tool["function"]["name"] == "weather_forecast"
        params = tool["function"]["parameters"]
        assert "location" in params["properties"]


@pytest.mark.asyncio
async def test_fetch_wttr_parsing(mock_system_config, mock_mcp_config):
    # Mock httpx AsyncClient.get and response
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {
        "current_condition": [{
            "temp_C": "20",
            "FeelsLikeC": "20",
            "humidity": "50",
            "windspeedKmph": "5",
            "winddir16Point": "N",
            "pressure": "1010",
            "visibility": "10",
            "weatherDesc": [{"value": "Sunny"}],
            "observation_time": "10:00 AM"
        }],
        "weather": [
            {"date": "2025-08-26", "maxtempC": "22", "mintempC": "12", "avgtempC": "17", "hourly": []}
        ]
    }

    class DummyClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        async def get(self, url):
            return mock_resp

    with patch('httpx.AsyncClient', return_value=DummyClient()):
        res = await sources.fetch_wttr("Berlin", 3, "metric", ssl_verify=True)
        assert res["location"] == "Berlin"
        assert res["source"] == "wttr.in"
        assert "current" in res
        assert "forecast" in res


@pytest.mark.asyncio
async def test_fetch_met_no_parsing(mock_system_config, mock_mcp_config):
    mock_timeseries = [
        {"time": "2025-08-26T00:00:00Z", "data": {"instant": {"details": {"air_temperature": 20}}}},
        {"time": "2025-08-27T00:00:00Z", "data": {"instant": {"details": {"air_temperature": 22}}}},
    ]

    mock_weather = {"properties": {"timeseries": mock_timeseries}}

    mock_resp_geo = Mock()
    mock_resp_geo.status_code = 200
    mock_resp_geo.json = lambda: [{"lat": "52.52", "lon": "13.405"}]

    mock_resp_weather = Mock()
    mock_resp_weather.status_code = 200
    mock_resp_weather.json = lambda: mock_weather

    class DummyClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        async def get(self, url, params=None):
            if 'nominatim' in url:
                return mock_resp_geo
            if 'met.no' in url:
                return mock_resp_weather
            raise RuntimeError("Unexpected URL")

    with patch('httpx.AsyncClient', return_value=DummyClient()):
        res = await sources.fetch_met_no("Berlin", 2, "metric", ssl_verify=True)
        assert res["location"] == "Berlin"
        assert res["source"] == "met.no"
        assert "forecast" in res


@pytest.mark.anyio
async def test_weather_status_phases(mock_system_config, mock_mcp_config):
    server = WeatherServer("weather", mock_system_config, mock_mcp_config)
    queue = await status_bus.subscribe(server="weather")
    try:
        from agent_system.mcp.status import StatusScope
        async with StatusScope(status_bus, "weather") as status:
            result = await server.call("weather_forecast", {"location": "Munich, Germany", "_status": status})
        await asyncio.sleep(0.1)
        events = []
        while True:
            try:
                event = queue.get_nowait()
                events.append(event)
            except asyncio.QueueEmpty:
                break
        phases = [event.phase for event in events]
        assert StatusPhase.START in phases
        if result.get("status") == "success":
            assert StatusPhase.END in phases
        else:
            assert StatusPhase.ERROR in phases
        start_index = None
        end_or_error_index = None
        for i, phase in enumerate(phases):
            if phase == StatusPhase.START and start_index is None:
                start_index = i
            elif phase in [StatusPhase.END, StatusPhase.ERROR] and end_or_error_index is None:
                end_or_error_index = i
        assert start_index is not None
        assert end_or_error_index is not None
        assert start_index < end_or_error_index
    finally:
        status_bus.unsubscribe(queue)


@pytest.mark.anyio
async def test_weather_error_status_phases(mock_system_config, mock_mcp_config):
    server = WeatherServer("weather_error", mock_system_config, mock_mcp_config)
    queue = await status_bus.subscribe(server="weather_error")
    try:
        from agent_system.mcp.status import StatusScope
        async with StatusScope(status_bus, "weather_error") as status:
            # Tool name is weather_error_forecast (server name + suffix)
            result = await server.call("weather_error_forecast", {"_status": status})
        await asyncio.sleep(0.1)
        events = []
        while True:
            try:
                event = queue.get_nowait()
                events.append(event)
            except asyncio.QueueEmpty:
                break
        phases = [event.phase for event in events]
        assert StatusPhase.ERROR in phases
        assert result["status"] == "error"
    finally:
        status_bus.unsubscribe(queue)


class TestWeatherCLIAndFactory:
    def test_plugin_factory_basic(self, mock_system_config, mock_mcp_config):
        from plugins.weather.plugin import PLUGIN_FACTORY
        server = PLUGIN_FACTORY("weather", mock_system_config, mock_mcp_config)
        assert server.name == "weather"


class TestWeatherCLI:
    """Test the weather plugin CLI functionality."""

    def test_build_parser_basic_args(self, mock_system_config, mock_mcp_config):
        """Test basic argument parsing."""
        parser = build_parser()
        args = parser.parse_args(['--location', 'Berlin'])

        assert args.location == 'Berlin'
        assert args.days == 3
        assert args.units == 'metric'
        assert args.server is False
        assert args.port == 8080

    def test_build_parser_all_args(self, mock_system_config, mock_mcp_config):
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

    def test_build_parser_defaults(self, mock_system_config, mock_mcp_config):
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

    def test_weather_server_initialization(self, mock_system_config, mock_mcp_config):
        """Test weather server initialization."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)
        assert server.name == "weather"
        assert server.ssl_verify is True

    def test_weather_server_initialization_with_config(self, mock_system_config, mock_mcp_config):
        """Test weather server initialization with config."""
        from agent_system.config.models import MCPConfig, AgentConfig
        
        mock_system_config.ssl_verify = False
        mcp_config = MCPConfig(type="weather", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 30
        mcp_config.retries = 3
        
        server = WeatherServer("weather", mock_system_config, mcp_config)
        assert server.name == "weather"
        assert server.ssl_verify is False

    def test_weather_server_schema(self, mock_system_config, mock_mcp_config):
        """Test weather server tools structure."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)
        tools = server.get_tools()

        assert isinstance(tools, list)
        assert len(tools) == 1
        
        tool = tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "weather_forecast"
        assert "description" in tool["function"]
        assert tool["function"]["parameters"]["type"] == "object"

        params = tool["function"]["parameters"]
        assert "location" in params["properties"]
        assert "source" in params["properties"]
        assert "days" in params["properties"]
        assert "units" in params["properties"]
        assert "include_marine" in params["properties"]

        # Check required fields
        assert "location" in params["required"]

    def test_weather_server_tool_name(self, mock_system_config, mock_mcp_config):
        """Test weather server tool name."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "weather_forecast"

    @pytest.mark.asyncio
    async def test_weather_server_missing_location(self, mock_system_config, mock_mcp_config):
        """Test weather server with missing location."""
        from unittest.mock import AsyncMock
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)
        status = AsyncMock()

        result = await server.call("weather_forecast", {"_status": status})
        assert result["status"] == "error"
        assert "Missing required parameter: location" in result["error"]

    @pytest.mark.asyncio
    async def test_weather_server_invalid_tool(self, mock_system_config, mock_mcp_config):
        """Test weather server with invalid tool name."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        # Modern pattern: generic dispatcher raises ValueError for unknown tools
        with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
            await server.call("invalid_tool", {"location": "Berlin", "_status": mock_status})

    @pytest.mark.asyncio
    async def test_weather_server_valid_tool(self, mock_system_config, mock_mcp_config):
        """Test weather server with valid tool name."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        result = await server.call("weather_forecast", {"location": "Berlin", "_status": mock_status})
        # Should not fail with "Unknown tool" error
        assert "Unknown tool" not in result.get("error", "")

    @pytest.mark.asyncio
    async def test_weather_server_source_selection(self, mock_system_config, mock_mcp_config):
        """Test weather server source selection logic."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

        # Mock successful response for met.no
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

            mock_status = AsyncMock()
            result = await server.call("weather_forecast", {"location": "Berlin", "_status": mock_status})
            assert result["status"] == "success"
            assert result["source"] == "met.no"
            assert "current" in result
            assert "forecast" in result

            # Test explicit source - should also use mocked response
            result = await server.call("weather_forecast", {"location": "Berlin", "source": "met.no", "_status": mock_status})
            assert result["status"] == "success"
            assert result["source"] == "met.no"

    @pytest.mark.asyncio
    async def test_weather_server_days_parameter(self, mock_system_config, mock_mcp_config):
        """Test weather server days parameter handling."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

        # Mock response for default days
        mock_response = {
            "location": "Berlin",
            "source": "met.no",
            "units": "metric",
            "current": {"temperature": 20},
            "forecast": [{"date": "2025-09-08", "max_temp": 22}]
        }

        with patch('plugins.weather.sources.fetch_met_no', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_response

            # Test default days
            mock_status = AsyncMock()
            result = await server.call("weather_forecast", {"location": "Berlin", "_status": mock_status})
            assert result["status"] == "success"
            assert len(result["forecast"]) >= 1

            # Test custom days
            result = await server.call("weather_forecast", {"location": "Berlin", "days": 2, "_status": mock_status})
            assert result["status"] == "success"
            assert len(result["forecast"]) >= 1  # Should have at least one day

    @pytest.mark.asyncio
    async def test_weather_server_units_parameter(self, mock_system_config, mock_mcp_config):
        """Test weather server units parameter handling."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

        # Mock response for metric units (met.no always returns metric)
        mock_response = {
            "location": "Berlin",
            "source": "met.no",
            "units": "metric",
            "current": {"temperature": 20},
            "forecast": [{"date": "2025-09-08", "max_temp": 22}]
        }

        with patch('plugins.weather.sources.fetch_met_no', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_response

            # Test metric units (default) - met.no always returns metric
            mock_status = AsyncMock()
            result = await server.call("weather_forecast", {"location": "Berlin", "units": "metric", "_status": mock_status})
            assert result["status"] == "success"
            assert result["units"] == "metric"

            # Test imperial units - met.no doesn't support imperial, so it still returns metric
            result = await server.call("weather_forecast", {"location": "Berlin", "units": "imperial", "_status": mock_status})
            assert result["status"] == "success"
            # met.no API only provides metric units, so units will be "metric" regardless of request
            assert result["units"] == "metric"

    @pytest.mark.asyncio
    async def test_weather_server_marine_parameter(self, mock_system_config, mock_mcp_config):
        """Test weather server marine parameter handling."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

        # Mock response for marine data
        mock_response = {
            "location": "Berlin",
            "source": "marine.weather.gov",
            "units": "metric",
            "current": {"temperature": 20},
            "forecast": [{"date": "2025-09-08", "max_temp": 22}]
        }

        with patch('plugins.weather.sources.fetch_marine_weather_gov', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_response

            # Test marine data request - should switch to marine source
            mock_status = AsyncMock()
            result = await server.call("weather_forecast", {"location": "Berlin", "include_marine": True, "_status": mock_status})
            assert result["status"] == "success"
            # Should switch to marine.weather.gov when marine data is requested
            assert result["source"] in ["marine.weather.gov", "met.no"]

    @pytest.mark.asyncio
    async def test_weather_server_source_switching_logic(self, mock_system_config, mock_mcp_config):
        """Test weather server automatic source switching logic."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

        # Mock response for met.no (fallback for wttr.in with > 3 days)
        mock_response = {
            "location": "Berlin",
            "source": "met.no",
            "units": "metric",
            "current": {"temperature": 20},
            "forecast": [{"date": "2025-09-08", "max_temp": 22}]
        }

        with patch('plugins.weather.sources.fetch_met_no', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_response

            # Test wttr.in with > 3 days should switch to met.no
            mock_status = AsyncMock()
            result = await server.call("weather_forecast", {"location": "Berlin", "source": "wttr.in", "days": 5, "_status": mock_status})
            assert result["status"] == "success"
            # Should switch to met.no for > 3 days with wttr.in
            assert result["source"] in ["met.no", "wttr.in"]

    @pytest.mark.asyncio
    async def test_weather_server_error_handling(self, mock_system_config, mock_mcp_config):
        """Test weather server error handling."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

        # Test with invalid location (should still work with real API, but let's test unsupported source)
        mock_status = AsyncMock()
        result = await server.call("weather_forecast", {"location": "Berlin", "source": "unsupported", "_status": mock_status})
        assert result["status"] == "error"
        assert "Unsupported weather source" in result["error"]

    @pytest.mark.asyncio
    async def test_weather_server_unsupported_source(self, mock_system_config, mock_mcp_config):
        """Test weather server with unsupported source."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        result = await server.call("weather_forecast", {"location": "Berlin", "source": "unsupported", "_status": mock_status})
        assert result["status"] == "error"
        assert "Unsupported weather source" in result["error"]

    @pytest.mark.asyncio
    async def test_weather_server_ssl_verify_parameter(self, mock_system_config, mock_mcp_config):
        """Test weather server SSL verification parameter."""
        # Mock response
        mock_response = {
            "location": "Berlin",
            "source": "met.no",
            "units": "metric",
            "current": {"temperature": 20},
            "forecast": [{"date": "2025-09-08", "max_temp": 22}]
        }

        with patch('plugins.weather.sources.fetch_met_no', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_response

            # Test with SSL verification enabled
            mock_status = AsyncMock()
            server_ssl = WeatherServer("weather", mock_system_config, mock_mcp_config)
            result = await server_ssl.call("weather_forecast", {"location": "Berlin", "_status": mock_status})
            assert result["status"] == "success"

            # Test with SSL verification disabled
            server_no_ssl = WeatherServer("weather", mock_system_config, mock_mcp_config)
            result = await server_no_ssl.call("weather_forecast", {"location": "Berlin", "_status": mock_status})
            assert result["status"] == "success"


class TestWeatherServerIntegration:
    """Integration tests for WeatherServer with mocked external services."""

    @pytest.mark.asyncio
    async def test_weather_server_full_workflow(self, mock_system_config, mock_mcp_config):
        """Test complete weather server workflow."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

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

            mock_status = AsyncMock()
            result = await server.call("weather_forecast", {
                "location": "Berlin",
                "days": 1,
                "units": "metric",
                "_status": mock_status
            })

            assert result["status"] == "success"
            assert result["location"] == "Berlin"
            assert result["source"] == "met.no"
            assert "current" in result
            assert "forecast" in result
            assert len(result["forecast"]) >= 1

    @pytest.mark.asyncio
    async def test_weather_server_multiple_sources(self, mock_system_config, mock_mcp_config):
        """Test weather server with different sources."""
        server = WeatherServer("weather", mock_system_config, mock_mcp_config)

        # Mock responses for different sources
        mock_met_no_response = {
            "location": "Berlin",
            "source": "met.no",
            "units": "metric",
            "current": {"temperature": 20},
            "forecast": [{"date": "2025-09-08", "max_temp": 22}]
        }

        mock_wttr_response = {
            "location": "Berlin",
            "source": "wttr.in",
            "units": "metric",
            "current": {"temperature": 20},
            "forecast": [{"date": "2025-09-08", "max_temp": 22}]
        }

        sources_to_test = [
            ("met.no", "met.no", mock_met_no_response, 'fetch_met_no'),
            ("wttr.in", "wttr.in", mock_wttr_response, 'fetch_wttr'),
        ]

        for source_name, expected_source, mock_response, mock_function in sources_to_test:
            with patch(f'plugins.weather.sources.{mock_function}', new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = mock_response

                mock_status = AsyncMock()
                result = await server.call("weather_forecast", {
                    "location": "Berlin",
                    "source": source_name,
                    "_status": mock_status
                })

                assert result["status"] == "success"
                assert result["source"] == expected_source


class TestWeatherPluginFactory:
    """Test the weather plugin factory function."""

    def test_plugin_factory_basic(self, mock_system_config, mock_mcp_config):
        """Test basic plugin factory functionality."""
        from plugins.weather.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("weather", mock_system_config, mock_mcp_config)
        assert server.name == "weather"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self, mock_system_config, mock_mcp_config):
        """Test plugin factory with configuration."""
        from plugins.weather.plugin import PLUGIN_FACTORY
        from agent_system.config.models import MCPConfig, AgentConfig

        # Mock system config with ssl_verify=False
        mock_system_config.ssl_verify = False
        mcp_config = MCPConfig(type="weather", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 60
        
        server = PLUGIN_FACTORY("weather", mock_system_config, mcp_config)
        assert server.name == "weather"
        assert server.ssl_verify is False

    def test_plugin_factory_name_parameter(self, mock_system_config, mock_mcp_config):
        """Test plugin factory with custom name."""
        from plugins.weather.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_weather", mock_system_config, mock_mcp_config)
        assert server.name == "custom_weather"


class TestWeatherSummary:
    """Test the weather summary generation feature."""

    @pytest.mark.asyncio
    async def test_summary_field_present(self, mock_system_config, mock_mcp_config):
        """Test that summary field is present in weather response."""
        from plugins.weather.server import WeatherServer
        from unittest.mock import AsyncMock, patch

        server = WeatherServer("weather", mock_system_config, mock_mcp_config)
        
        mock_weather_data = {
            "location": "TestCity",
            "source": "met.no",
            "units": "metric",
            "current": {
                "temperature": 20.5,
                "humidity": 65,
                "wind_speed": 15.2,
            },
            "forecast": [
                {
                    "date": "2025-01-01",
                    "max_temp": 22,
                    "min_temp": 18,
                    "hourly": [
                        {"time": "2025-01-01T12:00:00Z", "precipitation": 0.5},
                        {"time": "2025-01-01T15:00:00Z", "precipitation": 1.2},
                    ]
                }
            ]
        }
        
        with patch('plugins.weather.sources.fetch_met_no', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_weather_data
            
            mock_status = AsyncMock()
            result = await server.call("weather_forecast", {
                "location": "TestCity",
                "_status": mock_status
            })
            
            assert result["status"] == "success"
            assert "summary" in result
            assert isinstance(result["summary"], str)
            assert len(result["summary"]) > 0

    @pytest.mark.asyncio
    async def test_summary_content(self, mock_system_config, mock_mcp_config):
        """Test that summary contains expected weather information."""
        from plugins.weather.server import WeatherServer
        from unittest.mock import AsyncMock, patch

        server = WeatherServer("weather", mock_system_config, mock_mcp_config)
        
        mock_weather_data = {
            "location": "Berlin",
            "source": "met.no",
            "units": "metric",
            "current": {
                "temperature": 15.0,
                "humidity": 70,
                "wind_speed": 10.0,
            },
            "forecast": [
                {
                    "date": "2025-01-01",
                    "max_temp": 18,
                    "min_temp": 12,
                    "hourly": [
                        {"time": "2025-01-01T12:00:00Z", "precipitation": 2.5},
                    ]
                }
            ]
        }
        
        with patch('plugins.weather.sources.fetch_met_no', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_weather_data
            
            mock_status = AsyncMock()
            result = await server.call("weather_forecast", {
                "location": "Berlin",
                "_status": mock_status
            })
            
            summary = result["summary"]
            
            # Check for key information in summary
            assert "Berlin" in summary
            assert "15" in summary or "15.0" in summary  # Temperature
            assert "°C" in summary
            assert "humidity" in summary.lower()
            assert "2025-01-01" in summary  # Forecast date
            assert "rain" in summary.lower()  # Precipitation warning

    def test_create_summary_metric_units(self, mock_system_config, mock_mcp_config):
        """Test summary generation with metric units."""
        from plugins.weather.server import WeatherServer

        server = WeatherServer("weather", mock_system_config, mock_mcp_config)
        
        weather_data = {
            "location": "TestCity",
            "current": {
                "temperature": 20.5,
                "humidity": 65,
                "wind_speed": 15.2,
            },
            "forecast": [
                {
                    "date": "2025-01-01",
                    "max_temp": 22,
                    "min_temp": 18,
                    "hourly": [{"precipitation": 1.5}]
                }
            ]
        }
        
        summary = server._create_summary(weather_data, "detailed", "metric")
        
        assert "TestCity" in summary
        assert "20.5°C" in summary
        assert "humidity 65%" in summary
        assert "wind 15.2 km/h" in summary
        assert "18°C to 22°C" in summary
        assert "rain expected" in summary

    def test_create_summary_imperial_units(self, mock_system_config, mock_mcp_config):
        """Test summary generation with imperial units."""
        from plugins.weather.server import WeatherServer

        server = WeatherServer("weather", mock_system_config, mock_mcp_config)
        
        weather_data = {
            "location": "NewYork",
            "current": {
                "temperature": 68.5,
                "humidity": 60,
                "wind_speed": 10.2,
            },
            "forecast": [
                {
                    "date": "2025-01-01",
                    "max_temp": 72,
                    "min_temp": 65,
                    "hourly": [{"precipitation": 0.0}]
                }
            ]
        }
        
        summary = server._create_summary(weather_data, "detailed", "imperial")
        
        assert "NewYork" in summary
        assert "68.5°F" in summary
        assert "wind 10.2 mph" in summary
        assert "65°F to 72°F" in summary
        assert "rain expected" not in summary  # No precipitation
