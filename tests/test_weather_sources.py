import pytest
import asyncio
from unittest.mock import AsyncMock, Mock, patch

from plugins.weather import sources


@pytest.mark.asyncio
async def test_fetch_wttr_parsing():
    # Mock httpx AsyncClient.get and response
    # Use Mock (not AsyncMock) for response since httpx response methods are sync
    mock_resp = Mock()
    mock_resp.status_code = 200
    # httpx.Response.json() is synchronous in our usage after awaiting get()
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
async def test_fetch_met_no_parsing():
    # Provide a simplified met.no timeseries payload
    mock_timeseries = [
        {"time": "2025-08-26T00:00:00Z", "data": {"instant": {"details": {"air_temperature": 20}}}},
        {"time": "2025-08-27T00:00:00Z", "data": {"instant": {"details": {"air_temperature": 22}}}},
    ]

    mock_weather = {"properties": {"timeseries": mock_timeseries}}

    # Use Mock (not AsyncMock) for responses since httpx response methods are sync
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
        assert isinstance(res["forecast"], list)
