"""
Auth + bind-safety tests for the http_server /call endpoint (PB24).

The /call endpoint proxies arbitrary tool calls to the wrapped server. It must:
  - require the configured API key (X-API-Key or Authorization: Bearer)
  - stay open only on a loopback bind when no key is configured
  - refuse to start on a non-loopback host without a key
/health stays open regardless.
"""

import pytest
from unittest.mock import Mock
from fastapi.testclient import TestClient

from plugins.http_server.server import HTTPServer, _is_loopback_host


def _wrapped_server(server: HTTPServer) -> HTTPServer:
    mock = Mock()
    mock.name = "test_server"

    async def async_call(*args, **kwargs):
        return {"result": "ok"}

    mock.call = async_call
    server.wrap_server(mock)
    return server


@pytest.fixture
def server(mock_system_config, mock_server_config) -> HTTPServer:
    return _wrapped_server(HTTPServer("http_server", mock_system_config, mock_server_config))


class TestIsLoopbackHost:
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
    def test_loopback(self, host):
        assert _is_loopback_host(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "1.2.3.4", "10.0.0.5", "example.com", "::"])
    def test_non_loopback(self, host):
        assert _is_loopback_host(host) is False


class TestCallAuth:
    def test_open_when_no_key_configured(self, server):
        server.auth_key = None
        client = TestClient(server.create_fastapi_app())
        r = client.post("/call", json={"tool": "t", "params": {}})
        assert r.status_code == 200
        assert r.json() == {"result": "ok"}

    def test_call_rejected_without_key(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app())
        r = client.post("/call", json={"tool": "t", "params": {}})
        assert r.status_code == 401

    def test_call_accepts_x_api_key(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app())
        r = client.post(
            "/call",
            json={"tool": "t", "params": {}},
            headers={"X-API-Key": "secret-key"},
        )
        assert r.status_code == 200
        assert r.json() == {"result": "ok"}

    def test_call_accepts_bearer_token(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app())
        r = client.post(
            "/call",
            json={"tool": "t", "params": {}},
            headers={"Authorization": "Bearer secret-key"},
        )
        assert r.status_code == 200

    def test_call_rejects_wrong_key(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app())
        r = client.post(
            "/call",
            json={"tool": "t", "params": {}},
            headers={"X-API-Key": "wrong"},
        )
        assert r.status_code == 401

    def test_health_open_even_with_key(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app())
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestBindSafety:
    def test_refuses_non_loopback_without_auth(self, server):
        server.host = "0.0.0.0"
        server.auth_key = None
        with pytest.raises(RuntimeError, match="non-loopback"):
            server._check_bind_safety()

    def test_allows_non_loopback_with_auth(self, server):
        server.host = "0.0.0.0"
        server.auth_key = "secret-key"
        server._check_bind_safety()  # must not raise

    def test_allows_loopback_without_auth(self, server):
        server.host = "127.0.0.1"
        server.auth_key = None
        server._check_bind_safety()  # must not raise
