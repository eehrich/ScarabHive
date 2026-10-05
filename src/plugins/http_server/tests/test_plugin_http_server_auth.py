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

from plugins.http_server.server import HTTPServer


def _wrapped_server(server: HTTPServer) -> HTTPServer:
    mock = Mock()
    mock.name = "test_server"

    async def async_call(*args, **kwargs):
        return {"result": "ok"}

    mock.call_with_status = async_call
    server.wrap_server(mock)
    return server


@pytest.fixture
def server(mock_system_config, mock_server_config) -> HTTPServer:
    return _wrapped_server(HTTPServer("http_server", mock_system_config, mock_server_config))


class TestCallAuth:
    def test_open_when_no_key_configured(self, server):
        server.auth_key = None
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        r = client.post("/call", json={"tool": "t", "params": {}})
        assert r.status_code == 200
        assert r.json() == {"result": "ok"}

    def test_call_rejected_without_key(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        r = client.post("/call", json={"tool": "t", "params": {}})
        assert r.status_code == 401

    def test_call_accepts_x_api_key(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        r = client.post(
            "/call",
            json={"tool": "t", "params": {}},
            headers={"X-API-Key": "secret-key"},
        )
        assert r.status_code == 200
        assert r.json() == {"result": "ok"}

    def test_call_accepts_bearer_token(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        r = client.post(
            "/call",
            json={"tool": "t", "params": {}},
            headers={"Authorization": "Bearer secret-key"},
        )
        assert r.status_code == 200

    def test_call_rejects_wrong_key(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        r = client.post(
            "/call",
            json={"tool": "t", "params": {}},
            headers={"X-API-Key": "wrong"},
        )
        assert r.status_code == 401

    def test_health_open_even_with_key(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestHostHeaderWithoutKey:
    """Without a key, /call answers only requests naming a loopback host: a web
    page reaching 127.0.0.1 through DNS rebinding sends its own host name."""

    @pytest.mark.parametrize("base_url", ["http://evil.example:9000", "http://10.0.0.5:9000"])
    def test_foreign_host_rejected(self, server, base_url):
        server.auth_key = None
        client = TestClient(server.create_fastapi_app(), base_url=base_url)
        r = client.post("/call", json={"tool": "t", "params": {}})
        assert r.status_code == 403

    def test_malformed_host_rejected_not_crashing(self, server):
        server.auth_key = None
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        r = client.post("/call", json={"tool": "t", "params": {}}, headers={"Host": "[::1"})
        assert r.status_code == 403

    @pytest.mark.parametrize("host", ["localhost:9000", "[::1]:9000", "127.0.0.1"])
    def test_loopback_host_accepted(self, server, host):
        server.auth_key = None
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        r = client.post("/call", json={"tool": "t", "params": {}}, headers={"Host": host})
        assert r.status_code == 200

    def test_any_host_with_key(self, server):
        server.auth_key = "secret-key"
        client = TestClient(server.create_fastapi_app(), base_url="http://agents.example:9000")
        r = client.post("/call", json={"tool": "t", "params": {}}, headers={"X-API-Key": "secret-key"})
        assert r.status_code == 200


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


class TestReservedParams:
    """The runtime's `_` parameters (_session_id, _user_id, ...) are what plugins
    separate sessions and users by; a caller of the adapter must not set them."""

    def _capturing(self, server):
        seen = []

        async def capture(tool, params):
            seen.append(params)
            return {"result": "ok"}

        server.wrapped_server.call_with_status = capture
        return seen

    def test_http_call_drops_underscore_params(self, server):
        seen = self._capturing(server)
        client = TestClient(server.create_fastapi_app(), base_url="http://127.0.0.1:9000")
        client.post("/call", json={"tool": "t", "params": {"q": 1, "_user_id": "other", "_session_id": "s"}})
        assert seen == [{"q": 1}]
