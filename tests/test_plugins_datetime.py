import pytest
from plugins.datetime.plugin import factory as datetime_factory


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
