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
from agent_system.tools.status import status_bus, StatusPhase
from plugins.weather.__main__ import main, build_parser


def _get_tool_name(tool):
    """Extract the function.name from either a dict or a ToolDef-like object."""
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
    """Extract the function object from either a dict or a ToolDef-like object."""
    if isinstance(tool, dict):
        return tool.get("function", {})
    return getattr(tool, "function", None)


class TestWeatherServerBasic:
    """Basic unit tests for WeatherServer."""

    def test_weather_server_initialization(self, mock_system_config, mock_server_config):
        server = WeatherServer("weather", mock_system_config, mock_server_config)
        assert server.name == "weather"
        assert server.ssl_verify is True

    def test_weather_server_schema(self, mock_system_config, mock_server_config):
        server = WeatherServer("weather", mock_system_config, mock_server_config)
        tools = server.get_tools()

        assert isinstance(tools, list)
        assert len(tools) == 1
        tool = tools[0]
        assert tool["function"]["name"] == "weather_forecast"
        params = tool["function"]["parameters"]
        assert "location" in params["properties"]


@pytest.mark.asyncio
async def test_fetch_wttr_parsing(mock_system_config, mock_server_config):
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
async def test_fetch_met_no_parsing(mock_system_config, mock_server_config):
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
async def test_weather_status_phases(mock_system_config, mock_server_config):
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    queue = await status_bus.subscribe(server="weather")
    try:
        from agent_system.tools.status import StatusScope
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
async def test_weather_error_status_phases(mock_system_config, mock_server_config):
    server = WeatherServer("weather_error", mock_system_config, mock_server_config)
    queue = await status_bus.subscribe(server="weather_error")
    try:
        from agent_system.tools.status import StatusScope
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
    def test_plugin_factory_basic(self, mock_system_config, mock_server_config):
        from plugins.weather.plugin import PLUGIN_FACTORY
        server = PLUGIN_FACTORY("weather", mock_system_config, mock_server_config)
        assert server.name == "weather"


class TestWeatherCLI:
    """Test the weather plugin CLI functionality."""

    def test_build_parser_basic_args(self, mock_system_config, mock_server_config):
        """Test basic argument parsing."""
        parser = build_parser()
        args = parser.parse_args(['--location', 'Berlin'])

        assert args.location == 'Berlin'
        assert args.days == 3
        assert args.units == 'metric'
        assert args.server is False
        assert args.port == 8080

    def test_build_parser_all_args(self, mock_system_config, mock_server_config):
        """Test parsing with all arguments."""
        parser = build_parser()
        args = parser.parse_args([
            '--location', 'New York',
            '--source', 'weather.gov',
            '--days', '5',
            '--units', 'imperial',
            '--include-marine',
            '--summary-format', 'hourly',
            '--include-radiation',
            '--server',
            '--port', '9000'
        ])

        assert args.location == 'New York'
        assert args.source == 'weather.gov'
        assert args.days == 5
        assert args.units == 'imperial'
        assert args.include_marine is True
        assert args.summary_format == 'hourly'
        assert args.include_radiation is True
        assert args.server is True
        assert args.port == 9000

    def test_build_parser_defaults(self, mock_system_config, mock_server_config):
        """Test default values."""
        parser = build_parser()
        args = parser.parse_args([])

        assert args.location is None
        assert args.source is None
        assert args.days == 3
        assert args.units == 'metric'
        assert args.include_marine is False
        assert args.summary_format == 'daily'
        assert args.include_radiation is False
        assert args.server is False
        assert args.port == 8080

    def test_main_function_output(self, capsys):
        """Test main function output."""
        # Capture stdout
        main(['--location', 'Berlin', '--source', 'met.no'])

        captured = capsys.readouterr()
        assert "Weather Tool Server" in captured.out

        # Parse the JSON output
        lines = captured.out.strip().split('\n')
        json_line = [line for line in lines if line.startswith('{')][0]
        output = json.loads(json_line)

        assert output['description'] == 'Weather Tool Server'
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

    def test_weather_server_initialization(self, mock_system_config, mock_server_config):
        """Test weather server initialization."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)
        assert server.name == "weather"
        assert server.ssl_verify is True

    def test_weather_server_initialization_with_config(self, mock_system_config, mock_server_config):
        """Test weather server initialization with config."""
        from agent_system.config.models import ToolServerConfig, AgentConfig
        
        mock_system_config.ssl_verify = False
        server_config = ToolServerConfig(type="weather", enabled=True, agent_config=AgentConfig())
        server_config.timeout = 30
        server_config.retries = 3
        
        server = WeatherServer("weather", mock_system_config, server_config)
        assert server.name == "weather"
        assert server.ssl_verify is False

    def test_weather_server_schema(self, mock_system_config, mock_server_config):
        """Test weather server tools structure."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)
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

    def test_weather_server_tool_name(self, mock_system_config, mock_server_config):
        """Test weather server tool name."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "weather_forecast"

    @pytest.mark.asyncio
    async def test_weather_server_missing_location(self, mock_system_config, mock_server_config):
        """Test weather server with missing location."""
        from unittest.mock import AsyncMock
        server = WeatherServer("weather", mock_system_config, mock_server_config)
        status = AsyncMock()

        result = await server.call("weather_forecast", {"_status": status})
        assert result["status"] == "error"
        assert "Missing required parameter: location" in result["error"]

    @pytest.mark.asyncio
    async def test_weather_server_invalid_tool(self, mock_system_config, mock_server_config):
        """Test weather server with invalid tool name."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

        mock_status = AsyncMock()
        # Modern pattern: generic dispatcher raises ValueError for unknown tools
        with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
            await server.call("invalid_tool", {"location": "Berlin", "_status": mock_status})

    @pytest.mark.asyncio
    async def test_weather_server_valid_tool(self, mock_system_config, mock_server_config):
        """Test weather server with valid tool name."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

        mock_status = AsyncMock()
        result = await server.call("weather_forecast", {"location": "Berlin", "_status": mock_status})
        # Should not fail with "Unknown tool" error
        assert "Unknown tool" not in result.get("error", "")

    @pytest.mark.asyncio
    async def test_weather_server_source_selection(self, mock_system_config, mock_server_config):
        """Test weather server source selection logic."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

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
    async def test_weather_server_days_parameter(self, mock_system_config, mock_server_config):
        """Test weather server days parameter handling."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

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
    async def test_weather_server_units_parameter(self, mock_system_config, mock_server_config):
        """Test weather server units parameter handling."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

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
    async def test_weather_server_marine_parameter(self, mock_system_config, mock_server_config):
        """Test weather server marine parameter handling."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

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
    async def test_weather_server_source_switching_logic(self, mock_system_config, mock_server_config):
        """Test weather server automatic source switching logic."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

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
    async def test_weather_server_error_handling(self, mock_system_config, mock_server_config):
        """Test weather server error handling."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

        # Test with invalid location (should still work with real API, but let's test unsupported source)
        mock_status = AsyncMock()
        result = await server.call("weather_forecast", {"location": "Berlin", "source": "unsupported", "_status": mock_status})
        assert result["status"] == "error"
        assert "Unsupported weather source" in result["error"]

    @pytest.mark.asyncio
    async def test_weather_server_unsupported_source(self, mock_system_config, mock_server_config):
        """Test weather server with unsupported source."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

        mock_status = AsyncMock()
        result = await server.call("weather_forecast", {"location": "Berlin", "source": "unsupported", "_status": mock_status})
        assert result["status"] == "error"
        assert "Unsupported weather source" in result["error"]

    @pytest.mark.asyncio
    async def test_weather_server_ssl_verify_parameter(self, mock_system_config, mock_server_config):
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
            server_ssl = WeatherServer("weather", mock_system_config, mock_server_config)
            result = await server_ssl.call("weather_forecast", {"location": "Berlin", "_status": mock_status})
            assert result["status"] == "success"

            # Test with SSL verification disabled
            server_no_ssl = WeatherServer("weather", mock_system_config, mock_server_config)
            result = await server_no_ssl.call("weather_forecast", {"location": "Berlin", "_status": mock_status})
            assert result["status"] == "success"


class TestWeatherServerIntegration:
    """Integration tests for WeatherServer with mocked external services."""

    @pytest.mark.asyncio
    async def test_weather_server_full_workflow(self, mock_system_config, mock_server_config):
        """Test complete weather server workflow."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

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
    async def test_weather_server_multiple_sources(self, mock_system_config, mock_server_config):
        """Test weather server with different sources."""
        server = WeatherServer("weather", mock_system_config, mock_server_config)

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

    def test_plugin_factory_basic(self, mock_system_config, mock_server_config):
        """Test basic plugin factory functionality."""
        from plugins.weather.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("weather", mock_system_config, mock_server_config)
        assert server.name == "weather"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self, mock_system_config, mock_server_config):
        """Test plugin factory with configuration."""
        from plugins.weather.plugin import PLUGIN_FACTORY
        from agent_system.config.models import ToolServerConfig, AgentConfig

        # Mock system config with ssl_verify=False
        mock_system_config.ssl_verify = False
        server_config = ToolServerConfig(type="weather", enabled=True, agent_config=AgentConfig())
        server_config.timeout = 60
        
        server = PLUGIN_FACTORY("weather", mock_system_config, server_config)
        assert server.name == "weather"
        assert server.ssl_verify is False

    def test_plugin_factory_name_parameter(self, mock_system_config, mock_server_config):
        """Test plugin factory with custom name."""
        from plugins.weather.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_weather", mock_system_config, mock_server_config)
        assert server.name == "custom_weather"


class TestWeatherSummary:
    """Test the weather summary generation feature."""

    @pytest.mark.asyncio
    async def test_summary_field_present(self, mock_system_config, mock_server_config):
        """Test that summary field is present in weather response."""
        from plugins.weather.server import WeatherServer
        from unittest.mock import AsyncMock, patch

        server = WeatherServer("weather", mock_system_config, mock_server_config)
        
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
    async def test_summary_content(self, mock_system_config, mock_server_config):
        """Test that summary contains expected weather information."""
        from plugins.weather.server import WeatherServer
        from unittest.mock import AsyncMock, patch

        server = WeatherServer("weather", mock_system_config, mock_server_config)
        
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
                    "precipitation": 2.5,
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

    def test_create_summary_metric_units(self, mock_system_config, mock_server_config):
        """Test summary generation with metric units."""
        from plugins.weather.server import WeatherServer

        server = WeatherServer("weather", mock_system_config, mock_server_config)
        
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
                    "precipitation": 1.5,
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

    def test_create_summary_imperial_units(self, mock_system_config, mock_server_config):
        """Test summary generation with imperial units."""
        from plugins.weather.server import WeatherServer

        server = WeatherServer("weather", mock_system_config, mock_server_config)
        
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
                    "precipitation": 0.0,
                }
            ]
        }
        
        summary = server._create_summary(weather_data, "detailed", "imperial")
        
        assert "NewYork" in summary
        assert "68.5°F" in summary
        assert "wind 10.2 mph" in summary
        assert "65°F to 72°F" in summary
        assert "rain expected" not in summary  # No precipitation


# --- Regression tests for the fixes made while writing weather.guide ---------


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """httpx.AsyncClient stand-in: records its kwargs, answers by URL substring."""

    instances: list = []

    def __init__(self, routes, **kwargs):
        self.routes = routes
        self.kwargs = kwargs
        self.urls = []
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self.urls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp
        raise RuntimeError(f"unexpected URL {url}")


def _fake_httpx(routes):
    _FakeClient.instances = []
    return patch("httpx.AsyncClient", side_effect=lambda **kw: _FakeClient(routes, **kw))


def _nws_period(name, start, is_day, temp):
    return {"name": name, "startTime": start, "isDaytime": is_day, "temperature": temp,
            "temperatureUnit": "F", "windSpeed": "5 mph", "windDirection": "W",
            "shortForecast": name, "detailedForecast": name}


# An evening forecast: it starts with a night period.
_EVENING_PERIODS = [
    _nws_period("Tonight", "2026-09-29T18:00:00-07:00", False, 53),
    _nws_period("Wednesday", "2026-09-30T06:00:00-07:00", True, 63),
    _nws_period("Wednesday Night", "2026-09-30T18:00:00-07:00", False, 52),
    _nws_period("Thursday", "2026-10-01T06:00:00-07:00", True, 64),
]


@pytest.mark.asyncio
async def test_summary_labels_the_units_the_source_answered_in(mock_system_config, mock_server_config):
    """met.no answers metric with wind in m/s, whatever units were asked for."""
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    met_no = {"location": "Munich", "source": "met.no", "units": "metric",
              "current": {"temperature": 12.2, "wind_speed": 1.5},
              "forecast": [{"date": "2026-10-01", "max_temp": 20, "min_temp": 10, "hourly": []}]}
    with patch("plugins.weather.sources.fetch_met_no", new_callable=AsyncMock, return_value=met_no):
        result = await server.call("weather_forecast",
                                   {"location": "Munich", "units": "imperial", "_status": AsyncMock()})
    assert "Currently: 12.2°C, wind 1.5 m/s" in result["summary"]
    assert "10°C to 20°C" in result["summary"]


@pytest.mark.asyncio
async def test_weather_gov_follows_redirects_and_pairs_by_daytime():
    routes = {
        "geocoding": _Resp({"result": {"addressMatches": [{"coordinates": {"x": -122.33212, "y": 47.60621}}]}}),
        "/points/": _Resp({"properties": {"forecast": "https://api.weather.gov/gridpoints/SEW/1,2/forecast"}}),
        "gridpoints": _Resp({"properties": {"periods": _EVENING_PERIODS}}),
    }
    with _fake_httpx(routes):
        res = await sources.fetch_weather_gov("1 Main St, Seattle, WA", 2, "metric", True)
    # api.weather.gov answers 301 for coordinates with more than four decimals
    assert _FakeClient.instances[0].kwargs.get("follow_redirects") is True
    first, second = res["forecast"][0], res["forecast"][1]
    assert "day" not in first and first["night"]["temperature"] == 53
    assert second["day"]["temperature"] == 63 and second["night"]["temperature"] == 52


def test_summary_shows_weather_gov_day_and_night_temperatures(mock_system_config, mock_server_config):
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    result = {"location": "Seattle", "source": "weather.gov", "units": "imperial",
              "forecast": [{"date": "2026-09-30", "day": {"temperature": 63}, "night": {"temperature": 52}}]}
    assert "2026-09-30: 52°F to 63°F" in server._create_summary(result, "detailed", "metric")


@pytest.mark.asyncio
async def test_marine_geocodes_with_user_agent_and_reports_noaa_failure():
    routes = {"nominatim": _Resp([{"lat": "53.55", "lon": "10.0"}]),
              "/points/": _Resp({}, status_code=404)}
    with _fake_httpx(routes):
        res = await sources.fetch_marine_weather_gov("Hamburg", 1, "metric", True, True)
    kwargs = _FakeClient.instances[0].kwargs
    assert "User-Agent" in (kwargs.get("headers") or {})  # Nominatim answers 403 without one
    assert kwargs.get("follow_redirects") is True
    assert "404" in res["atmospheric_data_error"]
    assert res["units"] == "imperial"


@pytest.mark.asyncio
async def test_marine_pairs_periods_by_daytime():
    routes = {"nominatim": _Resp([{"lat": "25.79", "lon": "-80.13"}]),
              "/points/": _Resp({"properties": {"forecast": "https://api.weather.gov/gridpoints/MFL/1,2/forecast"}}),
              "gridpoints": _Resp({"properties": {"periods": _EVENING_PERIODS}})}
    with _fake_httpx(routes):
        res = await sources.fetch_marine_weather_gov("Miami Beach", 2, "metric", True, False)
    tonight, wednesday = res["forecast"][0], res["forecast"][1]
    assert tonight["max_temp"] == tonight["min_temp"] == 53
    assert (wednesday["max_temp"], wednesday["min_temp"]) == (63, 52)


@pytest.mark.asyncio
async def test_fetch_failure_answers_an_error_instead_of_raising(mock_system_config, mock_server_config):
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    with patch("plugins.weather.sources.fetch_met_no", new_callable=AsyncMock,
               side_effect=ValueError("Could not find coordinates for location: Nowhere")):
        result = await server.call("weather_forecast", {"location": "Nowhere", "_status": AsyncMock()})
    assert result["status"] == "error"
    assert result["error"] == "Could not find coordinates for location: Nowhere"
    assert result["message"] == "Failed to fetch weather data from met.no"


@pytest.mark.asyncio
async def test_days_is_validated_and_clamped(mock_system_config, mock_server_config):
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    met_no = {"location": "Munich", "source": "met.no", "units": "metric", "current": None, "forecast": []}
    result = await server.call("weather_forecast", {"location": "Munich", "days": "many", "_status": AsyncMock()})
    assert result == {"status": "error", "error": "days must be a whole number from 1 to 7, got 'many'"}
    with patch("plugins.weather.sources.fetch_met_no", new_callable=AsyncMock, return_value=met_no) as fetch:
        await server.call("weather_forecast", {"location": "Munich", "days": -2, "_status": AsyncMock()})
        await server.call("weather_forecast", {"location": "Munich", "days": 30, "_status": AsyncMock()})
    assert [c.args[1] for c in fetch.call_args_list] == [1, 7]


@pytest.mark.asyncio
async def test_include_marine_wins_over_the_wttr_fallback(mock_system_config, mock_server_config):
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    marine = {"location": "Miami", "source": "marine.weather.gov", "units": "imperial", "current": {}, "forecast": []}
    with patch("plugins.weather.sources.fetch_marine_weather_gov", new_callable=AsyncMock, return_value=marine) as fetch:
        result = await server.call("weather_forecast", {"location": "Miami", "source": "wttr.in", "days": 5,
                                                        "include_marine": True, "_status": AsyncMock()})
    assert fetch.await_count == 1
    assert result["source"] == "marine.weather.gov"


@pytest.mark.asyncio
async def test_cli_direct_mode_calls_the_forecast_tool(capsys, monkeypatch):
    from plugins.weather.__main__ import async_main
    met_no = {"location": "Munich", "source": "met.no", "units": "metric", "current": None, "forecast": []}
    monkeypatch.setattr("sys.argv", ["weather", "--location", "Munich"])
    with patch("plugins.weather.sources.fetch_met_no", new_callable=AsyncMock, return_value=met_no):
        await async_main()
    assert '"status": "success"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_null_source_takes_the_default(mock_system_config, mock_server_config):
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    met_no = {"location": "Munich", "source": "met.no", "units": "metric", "current": None, "forecast": []}
    with patch("plugins.weather.sources.fetch_met_no", new_callable=AsyncMock, return_value=met_no) as fetch:
        result = await server.call("weather_forecast", {"location": "Munich", "source": None, "units": None,
                                                        "summary_format": None, "_status": AsyncMock()})
    assert fetch.await_count == 1
    assert result["status"] == "success"


# --- Second review round ------------------------------------------------------


def _met_series(start, hourly_count, six_hourly_count):
    """A met.no series: hourly steps, then six-hourly ones, like the real API."""
    import datetime as _dt
    t = _dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    times = [t + _dt.timedelta(hours=h) for h in range(hourly_count)]
    last = times[-1]
    first_six = last + _dt.timedelta(hours=6 - last.hour % 6)
    times += [first_six + _dt.timedelta(hours=6 * k) for k in range(six_hourly_count)]
    return [{"time": x.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "data": {"instant": {"details": {"air_temperature": float(x.hour), "wind_speed": x.hour / 2}},
                      "next_6_hours": {"summary": {"symbol_code": f"sym{x.hour:02d}"},
                                       "details": {"precipitation_amount": 1.0}}}}
            for x in times]


async def _fetch_met_no(series, days, lon="0.0", hourly=False):
    routes = {"nominatim": _Resp([{"lat": "50.0", "lon": lon}]),
              "met.no": _Resp({"properties": {"timeseries": series}})}
    with _fake_httpx(routes):
        return await sources.fetch_met_no("Somewhere", days, "metric", True, hourly)


@pytest.mark.asyncio
async def test_met_no_days_reach_into_the_six_hourly_part():
    """60 hourly steps, six-hourly after: days=7 must give seven dates."""
    series = _met_series("2026-10-01T00:00:00Z", 60, 26)
    res = await _fetch_met_no(series, 7)
    assert [d["date"] for d in res["forecast"]] == [f"2026-10-0{d}" for d in range(1, 8)]


@pytest.mark.asyncio
async def test_met_no_groups_by_local_date():
    """At 23:00 UTC it is midnight at 15 degrees east: no one-entry first day."""
    series = _met_series("2026-10-01T23:00:00Z", 49, 0)
    res = await _fetch_met_no(series, 2, lon="15.0", hourly=True)
    assert [d["date"] for d in res["forecast"]] == ["2026-10-02", "2026-10-03"]
    assert len(res["forecast"][0]["hourly"]) == 24


@pytest.mark.asyncio
async def test_met_no_default_answer_is_one_compact_entry_per_day():
    series = _met_series("2026-10-01T00:00:00Z", 30, 0)
    res = await _fetch_met_no(series, 1)
    assert res["forecast"] == [{
        "date": "2026-10-01", "min_temp": 0.0, "max_temp": 23.0,
        # every step has 1 mm in its next six hours; only 00/06/12/18Z count
        "precipitation": 4.0, "wind_max": 11.5,
        "symbols": ["sym00", "sym06", "sym12", "sym18"],
    }]
    res = await _fetch_met_no(series, 1, hourly=True)
    assert len(res["forecast"][0]["hourly"]) == 24


@pytest.mark.asyncio
async def test_wttr_time_steps_only_with_hourly():
    day = {"date": "2026-10-01", "maxtempC": "20", "mintempC": "10",
           "hourly": [{"time": "0", "tempC": "10", "weatherDesc": [{"value": "Clear"}]}]}
    routes = {"wttr.in": _Resp({"current_condition": [{}], "weather": [day]})}
    with _fake_httpx(routes):
        compact = await sources.fetch_wttr("Oslo", 1, "metric", True)
        detailed = await sources.fetch_wttr("Oslo", 1, "metric", True, True)
    assert "hours" not in compact["forecast"][0]
    assert detailed["forecast"][0]["hours"][0]["temperature"] == "10"


@pytest.mark.asyncio
async def test_summary_format_hourly_reaches_the_source_and_keeps_the_summary(
        mock_system_config, mock_server_config):
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    met_no = {"location": "Oslo", "source": "met.no", "units": "metric", "current": None,
              "forecast": [{"date": "2026-10-01", "min_temp": 5, "max_temp": 9, "precipitation": 3.2}]}
    with patch("plugins.weather.sources.fetch_met_no", new_callable=AsyncMock, return_value=met_no) as fetch:
        result = await server.call("weather_forecast", {"location": "Oslo", "summary_format": "hourly",
                                                        "_status": AsyncMock()})
        await server.call("weather_forecast", {"location": "Oslo", "_status": AsyncMock()})
    assert [c.args[4] for c in fetch.call_args_list] == [True, False]
    assert "2026-10-01: 5°C to 9°C, rain expected (3.2 mm)" in result["summary"]


@pytest.mark.asyncio
async def test_nws_night_after_midnight_is_dated_the_day_before():
    """Overnight from 02:00, then Today and Tonight: two dates, not one twice."""
    periods = [
        _nws_period("Overnight", "2026-10-01T02:00:00-07:00", False, 50),
        _nws_period("Today", "2026-10-01T06:00:00-07:00", True, 64),
        _nws_period("Tonight", "2026-10-01T18:00:00-07:00", False, 51),
    ]
    forecast = {"properties": {"periods": periods}}
    points = {"properties": {"forecast": "https://api.weather.gov/gridpoints/SEW/1,2/forecast"}}
    routes = {"geocoding": _Resp({"result": {"addressMatches": [{"coordinates": {"x": -122.3, "y": 47.6}}]}}),
              "nominatim": _Resp([{"lat": "47.6", "lon": "-122.3"}]),
              "/points/": _Resp(points), "gridpoints": _Resp(forecast)}
    with _fake_httpx(routes):
        gov = await sources.fetch_weather_gov("1 Main St, Seattle, WA", 2, "metric", True)
        marine = await sources.fetch_marine_weather_gov("Seattle", 2, "metric", True, False)
    assert [d["date"] for d in gov["forecast"]] == ["2026-09-30", "2026-10-01"]
    assert [d["date"] for d in marine["forecast"]] == ["2026-09-30", "2026-10-01"]


@pytest.mark.asyncio
async def test_days_null_takes_the_default(mock_system_config, mock_server_config):
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    met_no = {"location": "Oslo", "source": "met.no", "units": "metric", "current": None, "forecast": []}
    with patch("plugins.weather.sources.fetch_met_no", new_callable=AsyncMock, return_value=met_no) as fetch:
        await server.call("weather_forecast", {"location": "Oslo", "days": None, "_status": AsyncMock()})
        await server.call("weather_forecast", {"location": "Oslo", "days": 0, "_status": AsyncMock()})
    assert [c.args[1] for c in fetch.call_args_list] == [3, 1]


@pytest.mark.asyncio
async def test_include_marine_as_the_string_false_does_not_switch(mock_system_config, mock_server_config):
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    met_no = {"location": "Oslo", "source": "met.no", "units": "metric", "current": None, "forecast": []}
    with patch("plugins.weather.sources.fetch_met_no", new_callable=AsyncMock, return_value=met_no):
        result = await server.call("weather_forecast", {"location": "Oslo", "include_marine": "false",
                                                        "_status": AsyncMock()})
    assert result["source"] == "met.no"


# --- Third review round -------------------------------------------------------


def _met_entry(time, temp, rain_1h=None, rain_6h=None, slot_min=None, slot_max=None):
    data = {"instant": {"details": {"air_temperature": temp, "wind_speed": 1.0}}}
    if rain_1h is not None:
        data["next_1_hours"] = {"summary": {"symbol_code": "rain"},
                                "details": {"precipitation_amount": rain_1h}}
    if rain_6h is not None:
        details = {"precipitation_amount": rain_6h}
        if slot_min is not None:
            details.update(air_temperature_min=slot_min, air_temperature_max=slot_max)
        data["next_6_hours"] = {"summary": {"symbol_code": "rain"}, "details": details}
    return {"time": time, "data": data}


@pytest.mark.asyncio
async def test_met_no_first_day_counts_the_hours_before_the_first_slot():
    """From 14Z: 14-17Z hour by hour (4 x 1 mm), then the 18Z slot (6 mm)."""
    series = [_met_entry(f"2026-10-01T{h:02d}:00:00Z", 10.0, rain_1h=1.0, rain_6h=6.0) for h in range(14, 24)]
    series.append(_met_entry("2026-10-02T00:00:00Z", 10.0, rain_1h=1.0, rain_6h=6.0))
    res = await _fetch_met_no(series, 1)
    assert res["forecast"][0]["precipitation"] == 10.0


@pytest.mark.asyncio
async def test_met_no_six_hourly_day_takes_the_slot_extremes():
    series = [_met_entry(f"2026-10-05T{h:02d}:00:00Z", t, rain_6h=0.0, slot_min=lo, slot_max=hi)
              for h, t, lo, hi in ((0, 5.0, 2.0, 6.0), (6, 8.0, 4.0, 15.0), (12, 16.0, 12.0, 18.0),
                                   (18, 9.0, 6.0, 12.0))]
    routes = {"nominatim": _Resp([{"lat": "50.0", "lon": "0.0"}]),
              "met.no": _Resp({"properties": {"timeseries": series}})}
    with _fake_httpx(routes):
        res = await sources.fetch_met_no("Somewhere", 1, "metric", True)
    day = res["forecast"][0]
    assert (day["min_temp"], day["max_temp"]) == (2.0, 18.0)
    # only locationforecast/complete carries the slot extremes
    assert "/locationforecast/2.0/complete" in _FakeClient.instances[0].urls[1]


@pytest.mark.asyncio
async def test_wttr_daily_mode_derives_the_day_from_its_hours():
    def hour(time, wind, humidity, rain, desc):
        return {"time": time, "windspeedKmph": wind, "humidity": humidity, "chanceofrain": rain,
                "weatherDesc": [{"value": desc}]}
    day = {"date": "2026-10-01", "maxtempC": "20", "mintempC": "10",
           "hourly": [hour("0", "5", "80", "0", "Clear "), hour("300", "12", "90", "70", "Light rain"),
                      hour("600", "8", "70", "40", "Clear")]}
    routes = {"wttr.in": _Resp({"current_condition": [{}], "weather": [day]})}
    with _fake_httpx(routes):
        res = await sources.fetch_wttr("Oslo", 1, "metric", True)
    entry = res["forecast"][0]
    assert entry["wind_speed"] == 12
    assert entry["humidity"] == 80
    assert entry["chance_of_rain"] == 70
    assert entry["weather_desc"] == ["Clear", "Light rain"]
    assert "hours" not in entry


def test_summary_shows_a_single_value_when_min_equals_max(mock_system_config, mock_server_config):
    server = WeatherServer("weather", mock_system_config, mock_server_config)
    result = {"location": "Oslo", "source": "met.no", "units": "metric",
              "forecast": [{"date": "2026-10-01", "min_temp": 6.0, "max_temp": 6.0}]}
    summary = server._create_summary(result, "daily", "metric")
    assert "  2026-10-01: 6.0°C" in summary
    assert "to 6.0°C" not in summary


@pytest.mark.asyncio
async def test_met_no_slot_is_booked_by_its_midpoint():
    """lon 77.2 is +5 h: the 18Z slot runs 23:00-05:00 local, mostly tomorrow."""
    series = [_met_entry("2026-10-01T12:00:00Z", 20.0, rain_6h=0.0, slot_min=18.0, slot_max=22.0),
              _met_entry("2026-10-01T18:00:00Z", 15.0, rain_6h=5.0, slot_min=2.0, slot_max=15.0),
              _met_entry("2026-10-02T00:00:00Z", 10.0, rain_6h=0.0, slot_min=8.0, slot_max=12.0)]
    res = await _fetch_met_no(series, 2, lon="77.2")
    today, tomorrow = res["forecast"]
    assert (today["date"], today["min_temp"], today["precipitation"]) == ("2026-10-01", 15.0, 0.0)
    assert (tomorrow["date"], tomorrow["min_temp"], tomorrow["precipitation"]) == ("2026-10-02", 2.0, 5.0)
