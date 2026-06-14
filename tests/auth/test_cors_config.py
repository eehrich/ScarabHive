"""
Tests for configure_cors (auth.middleware).

Security focus: credentials must never be combined with a wildcard origin,
because Starlette's CORSMiddleware reflects the request Origin (not a literal
"*") and emits Access-Control-Allow-Credentials: true in that case, which would
let cross-origin pages read authenticated responses (F14).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.auth.middleware import configure_cors


def _app_with_cors(**cors_kwargs) -> TestClient:
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    configure_cors(app, **cors_kwargs)
    return TestClient(app)


class TestCorsCredentialsWildcardGuard:
    """Wildcard origin + credentials must be refused (credentials dropped)."""

    def test_wildcard_origin_drops_credentials(self):
        client = _app_with_cors(allow_origins=["*"], allow_credentials=True)
        resp = client.get("/ping", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200
        # credentials must NOT be advertised under a wildcard
        assert resp.headers.get("access-control-allow-credentials") is None
        # and the response must not mirror the attacker origin as an allowlisted one
        assert resp.headers.get("access-control-allow-origin") == "*"

    def test_default_origins_is_wildcard_and_drops_credentials(self):
        # allow_origins=None -> defaults to ["*"]; still must drop credentials
        client = _app_with_cors(allow_origins=None, allow_credentials=True)
        resp = client.get("/ping", headers={"Origin": "https://evil.example"})
        assert resp.headers.get("access-control-allow-credentials") is None

    def test_preflight_wildcard_does_not_grant_credentials(self):
        client = _app_with_cors(allow_origins=["*"], allow_credentials=True)
        resp = client.options(
            "/ping",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-credentials") is None


class TestCorsExplicitAllowlist:
    """An explicit allowlist keeps credentials and only mirrors listed origins."""

    def test_explicit_origin_keeps_credentials(self):
        allowed = "https://app.example.com"
        client = _app_with_cors(allow_origins=[allowed], allow_credentials=True)
        resp = client.get("/ping", headers={"Origin": allowed})
        assert resp.headers.get("access-control-allow-origin") == allowed
        assert resp.headers.get("access-control-allow-credentials") == "true"

    def test_explicit_allowlist_rejects_unlisted_origin(self):
        allowed = "https://app.example.com"
        client = _app_with_cors(allow_origins=[allowed], allow_credentials=True)
        resp = client.get("/ping", headers={"Origin": "https://evil.example"})
        # Security property: an unlisted origin gets NO allow-origin echo, so the
        # browser blocks the response regardless of any allow-credentials header.
        assert resp.headers.get("access-control-allow-origin") is None


class TestCorsWildcardWithoutCredentials:
    """Wildcard without credentials is the normal, safe public-API case."""

    def test_wildcard_no_credentials_unchanged(self):
        client = _app_with_cors(allow_origins=["*"], allow_credentials=False)
        resp = client.get("/ping", headers={"Origin": "https://anything.example"})
        assert resp.headers.get("access-control-allow-origin") == "*"
        assert resp.headers.get("access-control-allow-credentials") is None
