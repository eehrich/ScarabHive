"""
Tests for the endpoint security enforcement module.

Tests the EndpointSecurityEnforcer class which provides centralized
security enforcement based on configuration rules.
"""

import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException

from agent_system.auth.enforcement import (
    EndpointSecurityEnforcer,
    EndpointPolicy,
    AnonymousUser,
    UserContext,
    has_role,
    ROLE_HIERARCHY,
)
from agent_system.config.models import (
    AuthConfig,
    AnonymousAccessConfig,
    EndpointSecurityConfig,
    EndpointSecurityRule,
    LLMSecurityConfig,
)


class TestRoleHierarchy:
    """Tests for role hierarchy validation."""
    
    def test_role_hierarchy_values(self):
        """Verify role hierarchy is correctly defined."""
        assert ROLE_HIERARCHY["guest"] < ROLE_HIERARCHY["user"]
        assert ROLE_HIERARCHY["user"] < ROLE_HIERARCHY["admin"]
    
    def test_has_role_guest(self):
        """Guest should have guest role."""
        user = MagicMock()
        user.role = "guest"
        assert has_role(user, "guest")
        assert not has_role(user, "user")
        assert not has_role(user, "admin")
    
    def test_has_role_user(self):
        """User should have user and guest roles."""
        user = MagicMock()
        user.role = "user"
        assert has_role(user, "guest")
        assert has_role(user, "user")
        assert not has_role(user, "admin")
    
    def test_has_role_admin(self):
        """Admin should have all roles."""
        user = MagicMock()
        user.role = "admin"
        assert has_role(user, "guest")
        assert has_role(user, "user")
        assert has_role(user, "admin")
    
    def test_has_role_none_user(self):
        """None user should have no roles."""
        assert not has_role(None, "guest")
        assert not has_role(None, "user")
        assert not has_role(None, "admin")
    
    def test_has_role_with_enum(self):
        """Handle role as enum value."""
        from enum import Enum
        
        class UserRole(Enum):
            ADMIN = "admin"
            USER = "user"
            GUEST = "guest"
        
        user = MagicMock()
        user.role = UserRole.USER
        assert has_role(user, "user")
        assert has_role(user, "guest")
        assert not has_role(user, "admin")


class TestAnonymousUser:
    """Tests for AnonymousUser dataclass."""
    
    def test_default_values(self):
        """Test default anonymous user values."""
        anon = AnonymousUser()
        assert anon.username == "anonymous"
        assert anon.role == "guest"
        assert anon.is_active
        assert not anon.is_authenticated
        assert anon.user_id == "anonymous"
    
    def test_custom_role(self):
        """Test anonymous user with custom role."""
        anon = AnonymousUser(role="limited")
        assert anon.role == "limited"


class TestUserContext:
    """Tests for UserContext dataclass."""
    
    def test_from_authenticated_user(self):
        """Create context from authenticated user."""
        user = MagicMock()
        user.id = 42
        user.username = "testuser"
        user.role = "user"
        
        ctx = UserContext.from_user(user)
        assert ctx.user_id == "42"
        assert ctx.username == "testuser"
        assert ctx.role == "user"
        assert ctx.is_authenticated
        assert not ctx.is_anonymous
    
    def test_from_anonymous_user(self):
        """Create context from anonymous user."""
        anon = AnonymousUser(role="guest")
        
        ctx = UserContext.from_user(anon)
        assert ctx.user_id == "anonymous"
        assert ctx.username == "anonymous"
        assert ctx.role == "guest"
        assert not ctx.is_authenticated
        assert ctx.is_anonymous


class TestEndpointSecurityEnforcer:
    """Tests for EndpointSecurityEnforcer."""
    
    @pytest.fixture
    def default_config(self) -> AuthConfig:
        """Create default auth config for tests."""
        return AuthConfig(
            enabled=True,
            anonymous_access=AnonymousAccessConfig(
                enabled=False,
                role="guest",
                allowed_endpoints=[
                    "GET /health",
                    "GET /static/*",
                    "POST /auth/login",
                ],
            ),
            endpoint_security=EndpointSecurityConfig(
                default_policy="require_auth",
                rules=[
                    EndpointSecurityRule(
                        pattern="/admin/*",
                        policy="require_auth",
                        min_role="admin",
                    ),
                    EndpointSecurityRule(
                        pattern="POST /run",
                        policy="require_auth",
                        min_role="user",
                    ),
                    EndpointSecurityRule(
                        pattern="GET /agents",
                        policy="allow_anonymous",
                    ),
                ],
            ),
            llm_security=LLMSecurityConfig(
                require_valid_user=True,
                validate_session_ownership=True,
                audit_llm_requests=True,
            ),
        )
    
    def test_auth_disabled_allows_all(self):
        """When auth is disabled, all endpoints are open."""
        config = AuthConfig(enabled=False)
        enforcer = EndpointSecurityEnforcer(config)
        
        policy = enforcer.get_endpoint_policy("POST", "/run")
        assert not policy.requires_auth
        
        policy = enforcer.get_endpoint_policy("GET", "/admin/users")
        assert not policy.requires_auth
    
    def test_admin_endpoint_requires_admin_role(self, default_config):
        """Admin endpoints should require admin role."""
        enforcer = EndpointSecurityEnforcer(default_config)
        
        policy = enforcer.get_endpoint_policy("GET", "/admin/users")
        assert policy.requires_auth
        assert policy.min_role == "admin"
    
    def test_run_endpoint_requires_user_role(self, default_config):
        """POST /run should require user role."""
        enforcer = EndpointSecurityEnforcer(default_config)
        
        policy = enforcer.get_endpoint_policy("POST", "/run")
        assert policy.requires_auth
        assert policy.min_role == "user"
    
    def test_agents_endpoint_allows_anonymous(self, default_config):
        """GET /agents explicitly allows anonymous access."""
        enforcer = EndpointSecurityEnforcer(default_config)
        
        policy = enforcer.get_endpoint_policy("GET", "/agents")
        assert not policy.requires_auth
    
    def test_default_policy_applied(self, default_config):
        """Unknown endpoints should use default policy."""
        enforcer = EndpointSecurityEnforcer(default_config)
        
        policy = enforcer.get_endpoint_policy("GET", "/some/unknown/endpoint")
        assert policy.requires_auth  # default is require_auth
        assert policy.min_role == "user"  # default min_role
    
    def test_method_specific_matching(self, default_config):
        """Rules should match specific HTTP methods."""
        enforcer = EndpointSecurityEnforcer(default_config)
        
        # POST /run matches the rule
        policy = enforcer.get_endpoint_policy("POST", "/run")
        assert policy.requires_auth
        assert policy.min_role == "user"
        
        # GET /run doesn't match the POST-specific rule, falls to default
        policy = enforcer.get_endpoint_policy("GET", "/run")
        assert policy.requires_auth
        assert policy.min_role == "user"  # default
    
    def test_wildcard_pattern_matching(self, default_config):
        """Wildcard patterns should match multiple paths."""
        enforcer = EndpointSecurityEnforcer(default_config)
        
        # /admin/* should match any admin path
        policy = enforcer.get_endpoint_policy("GET", "/admin/users")
        assert policy.requires_auth
        assert policy.min_role == "admin"
        
        policy = enforcer.get_endpoint_policy("DELETE", "/admin/users/123")
        assert policy.requires_auth
        assert policy.min_role == "admin"
    
    def test_is_endpoint_allowed_anonymous(self, default_config):
        """Test anonymous endpoint allowlist checking."""
        default_config.anonymous_access.enabled = True
        enforcer = EndpointSecurityEnforcer(default_config)
        
        # Allowed endpoints
        assert enforcer.is_endpoint_allowed_anonymous("GET", "/health")
        assert enforcer.is_endpoint_allowed_anonymous("GET", "/static/css/style.css")
        assert enforcer.is_endpoint_allowed_anonymous("POST", "/auth/login")
        
        # Not in allowlist
        assert not enforcer.is_endpoint_allowed_anonymous("POST", "/run")
        assert not enforcer.is_endpoint_allowed_anonymous("GET", "/admin/users")
    
    def test_is_endpoint_allowed_anonymous_disabled(self, default_config):
        """Anonymous endpoints not allowed when feature disabled."""
        default_config.anonymous_access.enabled = False
        enforcer = EndpointSecurityEnforcer(default_config)
        
        # Even listed endpoints are not allowed
        assert not enforcer.is_endpoint_allowed_anonymous("GET", "/health")
    
    def test_validate_role_success(self, default_config):
        """Role validation should pass for sufficient permissions."""
        enforcer = EndpointSecurityEnforcer(default_config)
        
        admin_user = MagicMock()
        admin_user.role = "admin"
        
        # Admin can access admin endpoints
        enforcer.validate_role(admin_user, "admin")  # Should not raise
        
        # Admin can also access user endpoints
        enforcer.validate_role(admin_user, "user")  # Should not raise
    
    def test_validate_role_failure(self, default_config):
        """Role validation should fail for insufficient permissions."""
        enforcer = EndpointSecurityEnforcer(default_config)
        
        user = MagicMock()
        user.role = "user"
        
        # User cannot access admin endpoints
        with pytest.raises(HTTPException) as exc_info:
            enforcer.validate_role(user, "admin")
        
        assert exc_info.value.status_code == 403
        assert "admin" in exc_info.value.detail
    
    def test_validate_role_none_allowed(self, default_config):
        """None role requirement should always pass."""
        enforcer = EndpointSecurityEnforcer(default_config)
        
        user = MagicMock()
        user.role = "guest"
        
        enforcer.validate_role(user, None)  # Should not raise
    
    def test_create_anonymous_user(self, default_config):
        """Anonymous user creation should use configured role."""
        default_config.anonymous_access.role = "limited_guest"
        enforcer = EndpointSecurityEnforcer(default_config)
        
        anon = enforcer.create_anonymous_user()
        assert isinstance(anon, AnonymousUser)
        assert anon.role == "limited_guest"


class TestEndpointSecurityEnforcerAsync:
    """Async tests for EndpointSecurityEnforcer."""
    
    @pytest.fixture
    def enforcer_with_anonymous(self) -> EndpointSecurityEnforcer:
        """Create enforcer with anonymous access enabled."""
        config = AuthConfig(
            enabled=True,
            anonymous_access=AnonymousAccessConfig(
                enabled=True,
                role="guest",
                allowed_endpoints=[
                    "GET /health",
                    "POST /auth/login",
                ],
            ),
            endpoint_security=EndpointSecurityConfig(
                default_policy="require_auth",
                rules=[
                    EndpointSecurityRule(
                        pattern="POST /run",
                        policy="require_auth",
                        min_role="user",
                    ),
                ],
            ),
        )
        return EndpointSecurityEnforcer(config)
    
    @pytest.mark.asyncio
    async def test_enforce_with_authenticated_user(self, enforcer_with_anonymous):
        """Authenticated user should pass security check."""
        request = MagicMock()
        request.method = "POST"
        request.url.path = "/run"
        
        user = MagicMock()
        user.username = "testuser"
        user.role = "user"
        
        async def get_user(req):
            return user
        
        result = await enforcer_with_anonymous.enforce_endpoint_security(request, get_user)
        assert result == user
    
    @pytest.mark.asyncio
    async def test_enforce_anonymous_on_allowed_endpoint(self, enforcer_with_anonymous):
        """Anonymous access on allowed endpoint should return AnonymousUser."""
        request = MagicMock()
        request.method = "GET"
        request.url.path = "/health"
        
        async def get_user(req):
            return None
        
        result = await enforcer_with_anonymous.enforce_endpoint_security(request, get_user)
        assert isinstance(result, AnonymousUser)
    
    @pytest.mark.asyncio
    async def test_enforce_anonymous_on_protected_endpoint_raises(self, enforcer_with_anonymous):
        """Anonymous access on protected endpoint should raise 401."""
        request = MagicMock()
        request.method = "POST"
        request.url.path = "/run"
        
        async def get_user(req):
            return None
        
        with pytest.raises(HTTPException) as exc_info:
            await enforcer_with_anonymous.enforce_endpoint_security(request, get_user)
        
        assert exc_info.value.status_code == 401
    
    @pytest.mark.asyncio
    async def test_enforce_insufficient_role_raises(self, enforcer_with_anonymous):
        """User with insufficient role should get 403."""
        request = MagicMock()
        request.method = "POST"
        request.url.path = "/run"
        
        guest_user = MagicMock()
        guest_user.username = "guest"
        guest_user.role = "guest"
        
        async def get_user(req):
            return guest_user
        
        with pytest.raises(HTTPException) as exc_info:
            await enforcer_with_anonymous.enforce_endpoint_security(request, get_user)
        
        assert exc_info.value.status_code == 403


class TestEndpointPolicyDataclass:
    """Tests for EndpointPolicy dataclass."""
    
    def test_policy_creation(self):
        """Test EndpointPolicy creation."""
        policy = EndpointPolicy(
            requires_auth=True,
            min_role="admin",
            rule_description="Admin only",
            matched_pattern="/admin/*",
        )
        
        assert policy.requires_auth
        assert policy.min_role == "admin"
        assert policy.rule_description == "Admin only"
        assert policy.matched_pattern == "/admin/*"
    
    def test_policy_no_auth(self):
        """Test EndpointPolicy with no auth required."""
        policy = EndpointPolicy(
            requires_auth=False,
            min_role=None,
            rule_description="Public endpoint",
            matched_pattern=None,
        )
        
        assert not policy.requires_auth
        assert policy.min_role is None


class TestConfigIntegration:
    """Tests for config model integration."""
    
    def test_auth_config_defaults(self):
        """AuthConfig should have sensible defaults."""
        config = AuthConfig()
        
        assert not config.enabled  # Disabled by default
        assert not config.anonymous_access.enabled
        assert config.endpoint_security.default_policy == "require_auth"
        assert config.llm_security.require_valid_user
    
    def test_auth_config_with_anonymous(self):
        """Test AuthConfig with anonymous access enabled."""
        config = AuthConfig(
            enabled=True,
            anonymous_access=AnonymousAccessConfig(
                enabled=True,
                role="public",
                rate_limit_multiplier=0.25,
            ),
        )
        
        assert config.enabled
        assert config.anonymous_access.enabled
        assert config.anonymous_access.role == "public"
        assert config.anonymous_access.rate_limit_multiplier == 0.25
    
    def test_llm_security_config(self):
        """Test LLMSecurityConfig settings."""
        config = LLMSecurityConfig(
            require_valid_user=True,
            validate_session_ownership=True,
            audit_llm_requests=False,
            max_requests_per_hour_anonymous=10,
        )
        
        assert config.require_valid_user
        assert config.validate_session_ownership
        assert not config.audit_llm_requests
        assert config.max_requests_per_hour_anonymous == 10
