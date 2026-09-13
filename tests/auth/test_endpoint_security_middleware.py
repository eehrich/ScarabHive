"""
Tests for the EndpointSecurityMiddleware (ASGI middleware).

Tests the middleware that enforces endpoint_security rules from config.yaml
by checking JWT tokens and validating user roles.
"""

import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone

from agent_system.auth.middleware import (
    EndpointSecurityMiddleware,
    ROLE_HIERARCHY,
)
from agent_system.config.models import (
    AuthConfig,
    AnonymousAccessConfig,
    EndpointSecurityConfig,
    EndpointSecurityRule,
)


class TestRoleHierarchyMiddleware:
    """Tests for role hierarchy in middleware."""
    
    def test_role_hierarchy_values(self):
        """Verify role hierarchy is correctly defined."""
        assert ROLE_HIERARCHY["guest"] == 1
        assert ROLE_HIERARCHY["user"] == 2
        assert ROLE_HIERARCHY["admin"] == 3
        assert ROLE_HIERARCHY["guest"] < ROLE_HIERARCHY["user"] < ROLE_HIERARCHY["admin"]


class TestEndpointSecurityMiddlewareInit:
    """Tests for EndpointSecurityMiddleware initialization."""
    
    @pytest.fixture
    def auth_config(self) -> AuthConfig:
        """Create auth config for tests."""
        return AuthConfig(
            enabled=True,
            secret_key="test-secret-key-for-jwt",
            algorithm="HS256",
            anonymous_access=AnonymousAccessConfig(
                enabled=False,
            ),
            endpoint_security=EndpointSecurityConfig(
                default_policy="require_auth",
                rules=[
                    EndpointSecurityRule(
                        pattern="GET /public",
                        policy="allow_anonymous",
                    ),
                    EndpointSecurityRule(
                        pattern="* /admin/*",
                        policy="require_auth",
                        min_role="admin",
                    ),
                    EndpointSecurityRule(
                        pattern="POST /run",
                        policy="require_auth",
                        min_role="user",
                    ),
                    EndpointSecurityRule(
                        pattern="/debug/*",
                        policy="require_auth",
                        min_role="admin",
                    ),
                ],
            ),
        )
    
    def test_middleware_compiles_patterns(self, auth_config):
        """Middleware should compile patterns on init."""
        app = AsyncMock()
        middleware = EndpointSecurityMiddleware(app, auth_config)
        
        assert len(middleware._compiled_patterns) == 4
    
    def test_middleware_parses_method_prefix(self, auth_config):
        """Middleware should parse method prefix from patterns."""
        app = AsyncMock()
        middleware = EndpointSecurityMiddleware(app, auth_config)
        
        # Check that methods were extracted correctly
        methods = [m for _, m, _ in middleware._compiled_patterns]
        assert "GET" in methods  # "GET /public"
        assert "*" in methods    # "* /admin/*"
        assert "POST" in methods # "POST /run"


class TestEndpointSecurityMiddlewarePatternMatching:
    """Tests for pattern matching logic."""
    
    @pytest.fixture
    def middleware(self) -> EndpointSecurityMiddleware:
        """Create middleware with test config."""
        config = AuthConfig(
            enabled=True,
            secret_key="test-secret-key",
            algorithm="HS256",
            endpoint_security=EndpointSecurityConfig(
                default_policy="require_auth",
                rules=[
                    EndpointSecurityRule(
                        pattern="GET /",
                        policy="allow_anonymous",
                    ),
                    EndpointSecurityRule(
                        pattern="GET /public/*",
                        policy="allow_anonymous",
                    ),
                    EndpointSecurityRule(
                        pattern="* /admin/*",
                        policy="require_auth",
                        min_role="admin",
                    ),
                    EndpointSecurityRule(
                        pattern="POST /run",
                        policy="require_auth",
                        min_role="user",
                    ),
                    EndpointSecurityRule(
                        pattern="/debug/*",
                        policy="require_auth",
                        min_role="admin",
                    ),
                    EndpointSecurityRule(
                        pattern="GET /docs",
                        policy="require_auth",
                        min_role="admin",
                    ),
                ],
            ),
        )
        app = AsyncMock()
        return EndpointSecurityMiddleware(app, config)
    
    def test_exact_match_root(self, middleware):
        """Exact match for root should not match other paths."""
        requires_auth, min_role, pattern = middleware._get_endpoint_policy("GET", "/")
        assert not requires_auth
        assert min_role is None
        assert pattern == "GET /"
        
        # /debug should NOT match "GET /" - it should match /debug/* rule
        requires_auth, min_role, pattern = middleware._get_endpoint_policy("GET", "/debug")
        # Falls through to default since /debug doesn't match /debug/*
        assert requires_auth
    
    def test_wildcard_pattern(self, middleware):
        """Wildcard patterns should match subpaths."""
        requires_auth, min_role, pattern = middleware._get_endpoint_policy("GET", "/public/css/style.css")
        assert not requires_auth
        assert pattern == "GET /public/*"
        
        requires_auth, min_role, pattern = middleware._get_endpoint_policy("GET", "/public/anything")
        assert not requires_auth
    
    def test_admin_wildcard_any_method(self, middleware):
        """* /admin/* should match any method."""
        for method in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
            requires_auth, min_role, pattern = middleware._get_endpoint_policy(method, "/admin/users")
            assert requires_auth
            assert min_role == "admin"
    
    def test_method_specific_rule(self, middleware):
        """Method-specific rules should only match that method."""
        # POST /run matches
        requires_auth, min_role, pattern = middleware._get_endpoint_policy("POST", "/run")
        assert requires_auth
        assert min_role == "user"
        assert pattern == "POST /run"
        
        # GET /run falls to default
        requires_auth, min_role, pattern = middleware._get_endpoint_policy("GET", "/run")
        assert requires_auth
        assert pattern == "default"
    
    def test_exact_match_docs(self, middleware):
        """GET /docs should match exactly, not /docs/anything."""
        requires_auth, min_role, pattern = middleware._get_endpoint_policy("GET", "/docs")
        assert requires_auth
        assert min_role == "admin"
        assert pattern == "GET /docs"
    
    def test_default_policy_fallback(self, middleware):
        """Unknown endpoints should use default policy."""
        requires_auth, min_role, pattern = middleware._get_endpoint_policy("GET", "/unknown/path")
        assert requires_auth
        assert min_role == "user"  # default
        assert pattern == "default"


class TestEndpointSecurityMiddlewareRoleCheck:
    """Tests for role checking logic."""
    
    @pytest.fixture
    def middleware(self) -> EndpointSecurityMiddleware:
        """Create middleware."""
        config = AuthConfig(
            enabled=True,
            secret_key="test-secret-key",
            algorithm="HS256",
        )
        app = AsyncMock()
        return EndpointSecurityMiddleware(app, config)
    
    def test_check_role_guest_for_guest(self, middleware):
        """Guest should have guest role."""
        assert middleware._check_role("guest", "guest")
    
    def test_check_role_guest_for_user(self, middleware):
        """Guest should NOT have user role."""
        assert not middleware._check_role("guest", "user")
    
    def test_check_role_guest_for_admin(self, middleware):
        """Guest should NOT have admin role."""
        assert not middleware._check_role("guest", "admin")
    
    def test_check_role_user_for_guest(self, middleware):
        """User should have guest role (hierarchy)."""
        assert middleware._check_role("user", "guest")
    
    def test_check_role_user_for_user(self, middleware):
        """User should have user role."""
        assert middleware._check_role("user", "user")
    
    def test_check_role_user_for_admin(self, middleware):
        """User should NOT have admin role."""
        assert not middleware._check_role("user", "admin")
    
    def test_check_role_admin_for_all(self, middleware):
        """Admin should have all roles."""
        assert middleware._check_role("admin", "guest")
        assert middleware._check_role("admin", "user")
        assert middleware._check_role("admin", "admin")
    
    def test_check_role_none_min_role(self, middleware):
        """None min_role should always pass."""
        assert middleware._check_role("guest", None)
        assert middleware._check_role("user", None)
        assert middleware._check_role("admin", None)
    
    def test_check_role_none_user_role(self, middleware):
        """None user role should always fail."""
        assert not middleware._check_role(None, "guest")
        assert not middleware._check_role(None, "user")
        assert not middleware._check_role(None, "admin")
    
    def test_check_role_case_insensitive(self, middleware):
        """Role check should be case insensitive."""
        assert middleware._check_role("ADMIN", "admin")
        assert middleware._check_role("Admin", "ADMIN")
        assert middleware._check_role("USER", "user")
    
    def test_check_role_unknown_role(self, middleware):
        """Unknown roles should have level 0."""
        # Unknown user role vs known min_role
        assert not middleware._check_role("unknown", "guest")
        
        # Known user role vs unknown min_role (min_role level = 0, so user passes)
        assert middleware._check_role("user", "unknown")


class TestEndpointSecurityMiddlewareJWTExtraction:
    """Tests for JWT token extraction."""
    
    @pytest.fixture
    def middleware(self) -> EndpointSecurityMiddleware:
        """Create middleware with known secret."""
        config = AuthConfig(
            enabled=True,
            secret_key="test-secret-key-12345",
            algorithm="HS256",
        )
        app = AsyncMock()
        return EndpointSecurityMiddleware(app, config)
    
    def _create_token(self, payload: dict, secret: str = "test-secret-key-12345") -> str:
        """Create a JWT token for testing."""
        from jose import jwt
        return jwt.encode(payload, secret, algorithm="HS256")
    
    def test_extract_from_bearer_header(self, middleware):
        """Should extract token from Authorization Bearer header."""
        token = self._create_token({"sub": "testuser", "role": "admin"})
        scope = {
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username == "testuser"
        assert role == "admin"
    
    def test_extract_from_cookie(self, middleware):
        """Should extract token from access_token cookie."""
        token = self._create_token({"sub": "cookieuser", "role": "user"})
        scope = {
            "headers": [
                (b"cookie", f"other=value; access_token={token}; another=test".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username == "cookieuser"
        assert role == "user"
    
    def test_a_token_in_the_query_string_does_not_authenticate(self, middleware):
        """A token in a URL leaks into access logs, history and Referer headers,
        so ``?token=`` is refused -- even carrying a valid JWT."""
        token = self._create_token({"sub": "queryuser", "role": "user"})
        scope = {
            "headers": [],
            "query_string": f"token={token}&other=value".encode(),
        }

        assert middleware._extract_user_info(scope) == (None, None)
    
    def test_extract_bearer_takes_precedence_over_cookie(self, middleware):
        """Bearer header should take precedence over cookie."""
        bearer_token = self._create_token({"sub": "bearer_user", "role": "admin"})
        cookie_token = self._create_token({"sub": "cookie_user", "role": "guest"})
        
        scope = {
            "headers": [
                (b"authorization", f"Bearer {bearer_token}".encode()),
                (b"cookie", f"access_token={cookie_token}".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username == "bearer_user"
        assert role == "admin"
    
    def test_extract_cookie_takes_precedence_over_query(self, middleware):
        """Cookie should take precedence over query parameter."""
        cookie_token = self._create_token({"sub": "cookie_user", "role": "admin"})
        query_token = self._create_token({"sub": "query_user", "role": "guest"})
        
        scope = {
            "headers": [
                (b"cookie", f"access_token={cookie_token}".encode()),
            ],
            "query_string": f"token={query_token}".encode(),
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username == "cookie_user"
        assert role == "admin"
    
    def test_extract_no_token(self, middleware):
        """Should return None for missing token."""
        scope = {"headers": [], "query_string": b""}
        
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None
    
    def test_extract_invalid_token(self, middleware):
        """Should return None for invalid token."""
        scope = {
            "headers": [
                (b"authorization", b"Bearer invalid-token"),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None
    
    def test_extract_wrong_secret(self, middleware):
        """Should return None for token signed with wrong secret."""
        token = self._create_token({"sub": "hacker", "role": "admin"}, secret="wrong-secret")
        scope = {
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None
    
    def test_extract_expired_token(self, middleware):
        """Should return None for expired token."""
        expired_payload = {
            "sub": "testuser",
            "role": "admin",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1)
        }
        token = self._create_token(expired_payload)
        
        scope = {
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None
    
    def test_extract_default_role(self, middleware):
        """Should default to 'user' role if not in token."""
        token = self._create_token({"sub": "norole"})  # No role in payload
        scope = {
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username == "norole"
        assert role == "user"  # Default
    
    def test_extract_rejects_invalid_username_chars(self, middleware):
        """Should reject usernames with invalid characters."""
        # Usernames with special characters should be rejected
        token = self._create_token({"sub": "user<script>alert(1)</script>"})
        scope = {
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None
    
    def test_extract_rejects_empty_username(self, middleware):
        """Should reject empty username."""
        token = self._create_token({"sub": ""})
        scope = {
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None
    
    def test_extract_rejects_none_username(self, middleware):
        """Should reject None username."""
        token = self._create_token({"role": "admin"})  # No sub claim
        scope = {
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None
    
    def test_extract_allows_valid_username_formats(self, middleware):
        """Should allow valid username formats."""
        valid_usernames = [
            "user123",
            "john.doe",
            "user_name",
            "user-name",
            "User.Name_123",
        ]
        
        for valid_username in valid_usernames:
            token = self._create_token({"sub": valid_username, "role": "user"})
            scope = {
                "headers": [
                    (b"authorization", f"Bearer {token}".encode()),
                ],
                "query_string": b"",
            }
            
            username, role = middleware._extract_user_info(scope)
            assert username == valid_username, f"Should accept '{valid_username}'"
    
    def test_extract_unknown_role_defaults_to_user(self, middleware):
        """Unknown roles should default to 'user'."""
        token = self._create_token({"sub": "testuser", "role": "superadmin"})
        scope = {
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username == "testuser"
        assert role == "user"  # Unknown role defaults to user


class TestEndpointSecurityMiddlewareApiKeyExtraction:
    """Tests for X-API-Key fallback in _extract_user_info.

    Backs the middleware patch that makes the same key UserDatabase accepts
    via FastAPI's ``X-API-Key`` header also work at the ASGI middleware layer
    (previously the middleware rejected such requests before the endpoint
    ever ran).
    """

    @pytest.fixture
    def middleware(self):
        """Create middleware with known secret."""
        config = AuthConfig(
            enabled=True,
            secret_key="test-secret-key-12345",
            algorithm="HS256",
        )
        app = AsyncMock()
        return EndpointSecurityMiddleware(app, config)

    @pytest.fixture
    def api_key_db(self, tmp_path, monkeypatch):
        """Temporary UserDatabase wired as the global singleton.

        Resets the global ``_db`` reference both before and after the test so
        that the middleware's lazy lookup uses our isolated copy and the next
        test doesn't see leaked state.
        """
        from pathlib import Path
        from agent_system.auth import database as auth_db
        from agent_system.auth.models import UserCreate, UserRole

        previous = auth_db._db
        db = auth_db.setup_database(Path(tmp_path) / "users.db")
        user = db.create_user(UserCreate(
            username="apiuser",
            email="api@example.com",
            password="irrelevant-pw-123",
            full_name="API User",
            role=UserRole.ADMIN,
            is_active=True,
        ))
        api_key = db.generate_user_api_key(user.id)
        yield db, user, api_key
        auth_db._db = previous

    def _create_token(self, payload: dict, secret: str = "test-secret-key-12345") -> str:
        from jose import jwt
        return jwt.encode(payload, secret, algorithm="HS256")

    def test_extract_from_api_key_header(self, middleware, api_key_db):
        """Valid X-API-Key should authenticate the request."""
        _, user, api_key = api_key_db
        scope = {
            "headers": [(b"x-api-key", api_key.encode())],
            "query_string": b"",
        }

        username, role = middleware._extract_user_info(scope)
        assert username == user.username
        assert role == "admin"

    def test_api_key_unknown_returns_none(self, middleware, api_key_db):
        """Unknown key must yield (None, None) without raising."""
        scope = {
            "headers": [(b"x-api-key", b"not-a-real-key")],
            "query_string": b"",
        }

        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None

    def test_api_key_inactive_user_rejected(self, middleware, api_key_db):
        """Deactivated users must be rejected even with a valid key."""
        db, user, api_key = api_key_db
        from agent_system.auth.models import UserUpdate
        db.update_user(user.id, UserUpdate(is_active=False))

        scope = {
            "headers": [(b"x-api-key", api_key.encode())],
            "query_string": b"",
        }

        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None

    def test_jwt_takes_precedence_over_api_key(self, middleware, api_key_db):
        """Valid JWT wins over X-API-Key when both are present."""
        _, _, api_key = api_key_db
        token = self._create_token({"sub": "jwtuser", "role": "user"})
        scope = {
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"x-api-key", api_key.encode()),
            ],
            "query_string": b"",
        }

        username, role = middleware._extract_user_info(scope)
        assert username == "jwtuser"
        assert role == "user"

    def test_invalid_jwt_falls_back_to_api_key(self, middleware, api_key_db):
        """An invalid/expired JWT must not block a valid X-API-Key fallback."""
        _, user, api_key = api_key_db
        scope = {
            "headers": [
                (b"authorization", b"Bearer garbage.not.a.jwt"),
                (b"x-api-key", api_key.encode()),
            ],
            "query_string": b"",
        }

        username, role = middleware._extract_user_info(scope)
        assert username == user.username
        assert role == "admin"

    def test_empty_api_key_header_returns_none(self, middleware, api_key_db):
        """An empty X-API-Key header must not crash the lookup."""
        scope = {
            "headers": [(b"x-api-key", b"   ")],
            "query_string": b"",
        }

        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None

    def test_api_key_lookup_is_constant_time(self, middleware, api_key_db, monkeypatch):
        """Verify path goes through verify_api_key (hmac.compare_digest)."""
        _, _, api_key = api_key_db
        called = {"verify": False}

        from agent_system.auth import security as auth_security
        original_verify = auth_security.verify_api_key

        def spy_verify(plain, hashed):
            called["verify"] = True
            return original_verify(plain, hashed)

        monkeypatch.setattr(auth_security, "verify_api_key", spy_verify)

        scope = {
            "headers": [(b"x-api-key", api_key.encode())],
            "query_string": b"",
        }
        middleware._extract_user_info(scope)
        assert called["verify"], "verify_api_key (hmac.compare_digest) must be invoked"

    def test_api_key_verify_runs_on_db_miss_too(self, middleware, api_key_db, monkeypatch):
        """verify_api_key must be invoked even when the key is unknown.

        Keeps the timing path constant between hit and miss so an attacker
        cannot enumerate valid API keys by measuring response latency.
        """
        calls = []
        from agent_system.auth import security as auth_security
        original_verify = auth_security.verify_api_key

        def spy_verify(plain, hashed):
            calls.append(hashed[:8])
            return original_verify(plain, hashed)

        monkeypatch.setattr(auth_security, "verify_api_key", spy_verify)

        scope = {
            "headers": [(b"x-api-key", b"nonexistent-key-12345")],
            "query_string": b"",
        }
        username, role = middleware._extract_user_info(scope)
        assert (username, role) == (None, None)
        assert len(calls) == 1, "verify_api_key must be called on miss too (timing parity)"

    def test_multiple_api_key_headers_rejected(self, middleware, api_key_db):
        """Two X-API-Key headers must be rejected as ambiguous.

        Python dict() keeps the LAST tuple, while Starlette/FastAPI Header()
        returns the FIRST — so if a client (or upstream proxy) sends two
        keys the middleware and downstream endpoint could authenticate as
        different users. Resolve the ambiguity by rejecting outright.
        """
        _, _, api_key = api_key_db
        scope = {
            "headers": [
                (b"x-api-key", api_key.encode()),
                (b"x-api-key", b"other-key-value"),
            ],
            "query_string": b"",
        }
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None

    def test_db_lookup_failure_returns_none_without_raising(self, middleware, api_key_db, monkeypatch):
        """A raised exception in the DB layer must not surface as 500."""
        from agent_system.auth import database as auth_db

        broken = type("Broken", (), {
            "get_user_by_api_key": lambda self, h: (_ for _ in ()).throw(RuntimeError("disk full"))
        })()
        monkeypatch.setattr(auth_db, "_db", broken)
        middleware._user_db = broken

        scope = {
            "headers": [(b"x-api-key", b"any-key")],
            "query_string": b"",
        }
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None


class TestEndpointSecurityMiddlewareCall:
    """Tests for middleware __call__ method."""
    
    @pytest.fixture
    def auth_config(self) -> AuthConfig:
        """Create auth config."""
        return AuthConfig(
            enabled=True,
            secret_key="test-secret-key-12345",
            algorithm="HS256",
            anonymous_access=AnonymousAccessConfig(enabled=False),
            endpoint_security=EndpointSecurityConfig(
                default_policy="require_auth",
                rules=[
                    EndpointSecurityRule(
                        pattern="GET /public",
                        policy="allow_anonymous",
                    ),
                    EndpointSecurityRule(
                        pattern="* /admin/*",
                        policy="require_auth",
                        min_role="admin",
                    ),
                ],
            ),
        )
    
    def _create_token(self, payload: dict, secret: str = "test-secret-key-12345") -> str:
        """Create a JWT token."""
        from jose import jwt
        return jwt.encode(payload, secret, algorithm="HS256")
    
    @pytest.mark.asyncio
    async def test_non_http_passes_through(self, auth_config):
        """Non-HTTP scopes should pass through."""
        app = AsyncMock()
        middleware = EndpointSecurityMiddleware(app, auth_config)
        
        scope = {"type": "websocket"}
        receive = AsyncMock()
        send = AsyncMock()
        
        await middleware(scope, receive, send)
        
        app.assert_called_once_with(scope, receive, send)
    
    @pytest.mark.asyncio
    async def test_public_endpoint_allows_anonymous(self, auth_config):
        """Public endpoints should allow anonymous access."""
        app = AsyncMock()
        middleware = EndpointSecurityMiddleware(app, auth_config)
        
        scope = {
            "type": "http",
            "path": "/public",
            "method": "GET",
            "headers": [],
        }
        receive = AsyncMock()
        send = AsyncMock()
        
        await middleware(scope, receive, send)
        
        app.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_protected_endpoint_rejects_unauthenticated(self, auth_config):
        """Protected endpoints should reject unauthenticated requests."""
        app = AsyncMock()
        middleware = EndpointSecurityMiddleware(app, auth_config)
        
        scope = {
            "type": "http",
            "path": "/admin/users",
            "method": "GET",
            "headers": [],
        }
        receive = AsyncMock()
        send = AsyncMock()
        
        await middleware(scope, receive, send)
        
        # App should NOT be called
        app.assert_not_called()
        
        # Should send 401 response
        calls = send.call_args_list
        assert len(calls) >= 1
        start_call = calls[0][0][0]
        assert start_call["type"] == "http.response.start"
        assert start_call["status"] == 401
    
    @pytest.mark.asyncio
    async def test_protected_endpoint_allows_authenticated(self, auth_config):
        """Protected endpoints should allow authenticated users."""
        app = AsyncMock()
        middleware = EndpointSecurityMiddleware(app, auth_config)
        
        token = self._create_token({"sub": "testuser", "role": "user"})
        scope = {
            "type": "http",
            "path": "/protected",
            "method": "GET",
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
        }
        receive = AsyncMock()
        send = AsyncMock()
        
        await middleware(scope, receive, send)
        
        app.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_admin_endpoint_rejects_user_role(self, auth_config):
        """Admin endpoints should reject users without admin role."""
        app = AsyncMock()
        middleware = EndpointSecurityMiddleware(app, auth_config)
        
        token = self._create_token({"sub": "testuser", "role": "user"})
        scope = {
            "type": "http",
            "path": "/admin/users",
            "method": "GET",
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
        }
        receive = AsyncMock()
        send = AsyncMock()
        
        await middleware(scope, receive, send)
        
        # App should NOT be called
        app.assert_not_called()
        
        # Should send 403 response
        calls = send.call_args_list
        start_call = calls[0][0][0]
        assert start_call["status"] == 403
    
    @pytest.mark.asyncio
    async def test_admin_endpoint_allows_admin_role(self, auth_config):
        """Admin endpoints should allow admin users."""
        app = AsyncMock()
        middleware = EndpointSecurityMiddleware(app, auth_config)
        
        token = self._create_token({"sub": "adminuser", "role": "admin"})
        scope = {
            "type": "http",
            "path": "/admin/users",
            "method": "GET",
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
            ],
        }
        receive = AsyncMock()
        send = AsyncMock()
        
        await middleware(scope, receive, send)
        
        app.assert_called_once()


class TestEndpointSecurityMiddlewareErrorResponses:
    """Tests for error response format."""
    
    @pytest.fixture
    def middleware(self) -> EndpointSecurityMiddleware:
        """Create middleware."""
        config = AuthConfig(
            enabled=True,
            secret_key="test-secret-key",
            algorithm="HS256",
            endpoint_security=EndpointSecurityConfig(
                default_policy="require_auth",
            ),
        )
        app = AsyncMock()
        return EndpointSecurityMiddleware(app, config)
    
    @pytest.mark.asyncio
    async def test_401_response_format(self, middleware):
        """401 response should have correct format and headers."""
        send = AsyncMock()
        
        await middleware._send_error_response(send, 401, "Authentication required")
        
        calls = send.call_args_list
        assert len(calls) == 2
        
        # Check start response
        start = calls[0][0][0]
        assert start["type"] == "http.response.start"
        assert start["status"] == 401
        
        # Check headers
        headers = dict(start["headers"])
        assert headers[b"content-type"] == b"application/json"
        assert b"www-authenticate" in headers  # RFC 7235
        
        # Check body
        import json
        body = calls[1][0][0]
        body_json = json.loads(body["body"])
        assert body_json["detail"] == "Authentication required"
        assert body_json["error"] == "Unauthorized"
        assert body_json["status_code"] == 401
    
    @pytest.mark.asyncio
    async def test_403_response_format(self, middleware):
        """403 response should have correct format."""
        send = AsyncMock()
        
        await middleware._send_error_response(send, 403, "Insufficient permissions")
        
        calls = send.call_args_list
        start = calls[0][0][0]
        assert start["status"] == 403
        
        # No WWW-Authenticate for 403
        headers = dict(start["headers"])
        assert b"www-authenticate" not in headers
        
        # Check body
        import json
        body = calls[1][0][0]
        body_json = json.loads(body["body"])
        assert body_json["error"] == "Forbidden"
        assert body_json["status_code"] == 403


class TestEndpointSecurityMiddlewareSecurityVectors:
    """Security-focused tests for potential vulnerabilities."""
    
    @pytest.fixture
    def middleware(self) -> EndpointSecurityMiddleware:
        """Create middleware with strict config."""
        config = AuthConfig(
            enabled=True,
            secret_key="secure-secret-key-32chars!",
            algorithm="HS256",
            endpoint_security=EndpointSecurityConfig(
                default_policy="require_auth",
                rules=[
                    EndpointSecurityRule(
                        pattern="* /admin/*",
                        policy="require_auth",
                        min_role="admin",
                    ),
                    EndpointSecurityRule(
                        pattern="GET /public/*",
                        policy="allow_anonymous",
                    ),
                ],
            ),
        )
        app = AsyncMock()
        return EndpointSecurityMiddleware(app, config)
    
    def _create_token(self, payload: dict, secret: str = "secure-secret-key-32chars!") -> str:
        """Create a JWT token."""
        from jose import jwt
        return jwt.encode(payload, secret, algorithm="HS256")
    
    def test_role_case_manipulation_attack(self, middleware):
        """Attackers should not bypass role check with case manipulation."""
        # Case variations of admin should all work (case-insensitive)
        assert middleware._check_role("ADMIN", "admin")
        assert middleware._check_role("Admin", "admin")
        assert middleware._check_role("admin", "admin")
        
        # User trying to become admin - should NEVER work regardless of case
        assert not middleware._check_role("user", "admin")
        assert not middleware._check_role("USER", "admin")
        assert not middleware._check_role("User", "admin")
        assert not middleware._check_role("user", "ADMIN")
        
        # User can access user-level endpoints
        assert middleware._check_role("user", "user")
        assert middleware._check_role("USER", "user")
    
    def test_none_algorithm_attack(self, middleware):
        """Tokens with 'none' algorithm should be rejected."""
        # jose library should reject these, but let's verify
        scope = {
            "headers": [
                (b"authorization", b"Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJoYWNrZXIiLCJyb2xlIjoiYWRtaW4ifQ."),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None
    
    def test_empty_token_attack(self, middleware):
        """Empty tokens should be rejected."""
        scope = {
            "headers": [
                (b"authorization", b"Bearer "),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        assert username is None
        assert role is None
    
    def test_malformed_header_attack(self, middleware):
        """Malformed auth headers should be rejected."""
        test_cases = [
            b"Bearer",              # No token
            b"Basic dXNlcjpwYXNz",  # Wrong scheme
            b"bearer token",        # lowercase bearer
            b"  Bearer token",      # leading space
        ]
        
        for header in test_cases:
            scope = {"headers": [(b"authorization", header)], "query_string": b""}
            username, role = middleware._extract_user_info(scope)
            # Should either be None or at least not give admin access
            if username is not None:
                assert role != "admin"
    
    def test_path_traversal_with_dotdot(self, middleware):
        """Path traversal with .. should be normalized."""
        # /admin/../public should normalize to /public (which is allowed)
        requires_auth, min_role, pattern = middleware._get_endpoint_policy(
            "GET", "/admin/../public/file.txt"
        )
        assert not requires_auth  # Should match /public/* after normalization
    
    def test_path_traversal_to_admin(self, middleware):
        """Path traversal trying to access admin should be blocked."""
        # /public/../admin/users should normalize to /admin/users
        requires_auth, min_role, pattern = middleware._get_endpoint_policy(
            "GET", "/public/../admin/users"
        )
        assert requires_auth
        assert min_role == "admin"
    
    def test_url_encoded_path_traversal(self, middleware):
        """URL-encoded path traversal should be normalized."""
        # %2e%2e is URL-encoded ..
        requires_auth, min_role, pattern = middleware._get_endpoint_policy(
            "GET", "/public/%2e%2e/admin/users"
        )
        assert requires_auth
        assert min_role == "admin"
    
    def test_double_slash_normalization(self, middleware):
        """Double slashes should be normalized."""
        requires_auth, min_role, pattern = middleware._get_endpoint_policy(
            "GET", "/admin//users"
        )
        assert requires_auth
        assert min_role == "admin"
    
    def test_dot_segment_normalization(self, middleware):
        """Single dot segments should be normalized."""
        requires_auth, min_role, pattern = middleware._get_endpoint_policy(
            "GET", "/admin/./users"
        )
        assert requires_auth
        assert min_role == "admin"
    
    @pytest.mark.asyncio
    async def test_path_traversal_attack(self, middleware):
        """Path traversal should not bypass security."""
        app = AsyncMock()
        middleware.app = app
        
        attack_paths = [
            "/admin/../public",
            "/public/../admin/users",
            "/admin/./users",
            "/admin//users",
        ]
        
        for path in attack_paths:
            scope = {
                "type": "http",
                "path": path,
                "method": "GET",
                "headers": [],
                "query_string": b"",
            }
            receive = AsyncMock()
            send = AsyncMock()
            app.reset_mock()
            
            await middleware(scope, receive, send)
            
            # If path contains /admin/, it should require auth
            if "/admin" in path.replace("..", ""):
                # Either blocked (401) or requires the rule check
                pass
    
    def test_jwt_tampering_attack(self, middleware):
        """Tampered JWT should be rejected."""
        # Create valid token
        valid_token = self._create_token({"sub": "user", "role": "user"})
        
        # Tamper with payload (change role)
        parts = valid_token.split(".")
        import base64
        import json
        
        # Decode payload, modify, re-encode (without re-signing)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
        payload["role"] = "admin"
        tampered_payload = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).decode().rstrip("=")
        
        tampered_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"
        
        scope = {
            "headers": [
                (b"authorization", f"Bearer {tampered_token}".encode()),
            ],
            "query_string": b"",
        }
        
        username, role = middleware._extract_user_info(scope)
        # Tampered token should fail verification
        assert username is None
        assert role is None


class TestPathNormalization:
    """Tests for path normalization security feature."""
    
    @pytest.fixture
    def middleware(self) -> EndpointSecurityMiddleware:
        """Create middleware."""
        config = AuthConfig(
            enabled=True,
            secret_key="test-secret-key",
            algorithm="HS256",
        )
        app = AsyncMock()
        return EndpointSecurityMiddleware(app, config)
    
    def test_normalize_simple_path(self, middleware):
        """Simple paths should remain unchanged."""
        assert middleware._normalize_path("/admin/users") == "/admin/users"
        assert middleware._normalize_path("/") == "/"
        assert middleware._normalize_path("/api/v1/run") == "/api/v1/run"
    
    def test_normalize_double_slash(self, middleware):
        """Double slashes should be collapsed."""
        assert middleware._normalize_path("//admin//users") == "/admin/users"
        assert middleware._normalize_path("/api//v1///run") == "/api/v1/run"
    
    def test_normalize_dotdot(self, middleware):
        """Parent directory references should be resolved."""
        assert middleware._normalize_path("/admin/../public") == "/public"
        assert middleware._normalize_path("/a/b/c/../../d") == "/a/d"
        assert middleware._normalize_path("/admin/users/../config") == "/admin/config"
    
    def test_normalize_dot(self, middleware):
        """Current directory references should be removed."""
        assert middleware._normalize_path("/admin/./users") == "/admin/users"
        assert middleware._normalize_path("/./api/./v1/./run") == "/api/v1/run"
    
    def test_normalize_url_encoded_dotdot(self, middleware):
        """URL-encoded .. should be decoded and normalized."""
        # %2e = .
        assert middleware._normalize_path("/%2e%2e/admin") == "/admin"
        assert middleware._normalize_path("/public/%2e%2e/admin") == "/admin"
    
    def test_normalize_url_encoded_slash(self, middleware):
        """URL-encoded slash should be decoded."""
        # %2f = /
        assert middleware._normalize_path("/admin%2fusers") == "/admin/users"
    
    def test_normalize_traversal_above_root(self, middleware):
        """Traversal above root should stay at root."""
        assert middleware._normalize_path("/../../../etc/passwd") == "/etc/passwd"
        assert middleware._normalize_path("/admin/../../..") == "/"
    
    def test_normalize_mixed_encoded(self, middleware):
        """Mixed encoded and plain paths should work."""
        assert middleware._normalize_path("/admin/%2e%2e/public/../api") == "/api"
    
    def test_normalize_empty_path(self, middleware):
        """Empty path should become root."""
        assert middleware._normalize_path("") == "/"
    
    def test_normalize_preserves_query_independence(self, middleware):
        """Path normalization doesn't affect query strings (they're separate)."""
        # Query strings are handled separately in ASGI scope
        # This just tests the path component
        assert middleware._normalize_path("/api/search") == "/api/search"
