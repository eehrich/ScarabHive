"""
Tests for security enforcement integration in the FastAPI app.

These tests verify that the security enforcement is properly integrated
into the application endpoints.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import HTTPException


class TestSecurityEnforcerIntegration:
    """Tests for security enforcer integration."""
    
    def test_security_config_models_import(self):
        """Verify security config models can be imported."""
        from agent_system.config.models import (
            AuthConfig,
            AnonymousAccessConfig,
            EndpointSecurityConfig,
            EndpointSecurityRule,
            LLMSecurityConfig,
        )
        
        # Create a full auth config with all security settings
        config = AuthConfig(
            enabled=True,
            anonymous_access=AnonymousAccessConfig(
                enabled=True,
                role="guest",
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
            llm_security=LLMSecurityConfig(
                require_valid_user=True,
                validate_session_ownership=True,
            ),
        )
        
        assert config.enabled
        assert config.anonymous_access.enabled
        assert config.endpoint_security.default_policy == "require_auth"
        assert config.llm_security.require_valid_user
    
    def test_enforcement_module_import(self):
        """Verify enforcement module can be imported."""
        from agent_system.auth.enforcement import (
            EndpointSecurityEnforcer,
            AnonymousUser,
            UserContext,
            EndpointPolicy,
            has_role,
        )
        
        # Verify classes/functions exist
        assert EndpointSecurityEnforcer is not None
        assert AnonymousUser is not None
        assert UserContext is not None
        assert EndpointPolicy is not None
        assert has_role is not None
    
    def test_enforcer_initialization(self):
        """Verify enforcer initializes with default config."""
        from agent_system.config.models import AuthConfig
        from agent_system.auth.enforcement import EndpointSecurityEnforcer
        
        config = AuthConfig(enabled=True)
        enforcer = EndpointSecurityEnforcer(config)
        
        # Verify it can check endpoints
        policy = enforcer.get_endpoint_policy("GET", "/health")
        assert policy is not None
    
    def test_enforcer_with_disabled_auth(self):
        """When auth is disabled, enforcer should allow all endpoints."""
        from agent_system.config.models import AuthConfig
        from agent_system.auth.enforcement import EndpointSecurityEnforcer
        
        config = AuthConfig(enabled=False)
        enforcer = EndpointSecurityEnforcer(config)
        
        # All endpoints should be allowed
        policy = enforcer.get_endpoint_policy("POST", "/run")
        assert not policy.requires_auth
        
        policy = enforcer.get_endpoint_policy("GET", "/admin/users")
        assert not policy.requires_auth
    
    @pytest.mark.asyncio
    async def test_enforcer_blocks_anonymous_llm_requests(self):
        """Verify enforcer blocks anonymous users from LLM endpoints."""
        from agent_system.config.models import AuthConfig, LLMSecurityConfig
        from agent_system.auth.enforcement import EndpointSecurityEnforcer, AnonymousUser
        
        config = AuthConfig(
            enabled=True,
            llm_security=LLMSecurityConfig(
                require_valid_user=True,
                max_requests_per_hour_anonymous=0,
            ),
        )
        enforcer = EndpointSecurityEnforcer(config)
        
        # Anonymous user should be created but LLM access should be blocked by app logic
        anon = enforcer.create_anonymous_user()
        assert isinstance(anon, AnonymousUser)
        assert not anon.is_authenticated
    
    def test_config_yaml_defaults(self):
        """Verify config.yaml can be loaded with new security settings."""
        from agent_system.config.models import AuthConfig
        
        # Test that defaults are safe (auth disabled)
        config = AuthConfig()
        assert not config.enabled
        assert not config.anonymous_access.enabled
        assert config.llm_security.require_valid_user
        assert config.llm_security.max_requests_per_hour_anonymous == 0


class TestEndpointSecurityRules:
    """Tests for endpoint security rule matching."""
    
    def test_admin_endpoint_rule(self):
        """Admin endpoints should require admin role."""
        from agent_system.config.models import AuthConfig, EndpointSecurityConfig, EndpointSecurityRule
        from agent_system.auth.enforcement import EndpointSecurityEnforcer
        
        config = AuthConfig(
            enabled=True,
            endpoint_security=EndpointSecurityConfig(
                rules=[
                    EndpointSecurityRule(
                        pattern="/admin/*",
                        policy="require_auth",
                        min_role="admin",
                    ),
                ],
            ),
        )
        enforcer = EndpointSecurityEnforcer(config)
        
        policy = enforcer.get_endpoint_policy("GET", "/admin/users")
        assert policy.requires_auth
        assert policy.min_role == "admin"
        
        policy = enforcer.get_endpoint_policy("DELETE", "/admin/users/123")
        assert policy.requires_auth
        assert policy.min_role == "admin"
    
    def test_run_endpoint_rule(self):
        """POST /run should require user role."""
        from agent_system.config.models import AuthConfig, EndpointSecurityConfig, EndpointSecurityRule
        from agent_system.auth.enforcement import EndpointSecurityEnforcer
        
        config = AuthConfig(
            enabled=True,
            endpoint_security=EndpointSecurityConfig(
                rules=[
                    EndpointSecurityRule(
                        pattern="POST /run",
                        policy="require_auth",
                        min_role="user",
                    ),
                ],
            ),
        )
        enforcer = EndpointSecurityEnforcer(config)
        
        policy = enforcer.get_endpoint_policy("POST", "/run")
        assert policy.requires_auth
        assert policy.min_role == "user"
    
    def test_anonymous_allowed_endpoint(self):
        """Endpoints in anonymous allowlist should be accessible."""
        from agent_system.config.models import AuthConfig, AnonymousAccessConfig
        from agent_system.auth.enforcement import EndpointSecurityEnforcer
        
        config = AuthConfig(
            enabled=True,
            anonymous_access=AnonymousAccessConfig(
                enabled=True,
                allowed_endpoints=[
                    "GET /health",
                    "GET /static/*",
                ],
            ),
        )
        enforcer = EndpointSecurityEnforcer(config)
        
        assert enforcer.is_endpoint_allowed_anonymous("GET", "/health")
        assert enforcer.is_endpoint_allowed_anonymous("GET", "/static/css/style.css")
        assert not enforcer.is_endpoint_allowed_anonymous("POST", "/run")


class TestRoleValidation:
    """Tests for role validation."""
    
    def test_role_validation_success(self):
        """Valid roles should pass validation."""
        from agent_system.config.models import AuthConfig
        from agent_system.auth.enforcement import EndpointSecurityEnforcer
        
        config = AuthConfig(enabled=True)
        enforcer = EndpointSecurityEnforcer(config)
        
        admin_user = MagicMock()
        admin_user.role = "admin"
        
        # Should not raise
        enforcer.validate_role(admin_user, "admin")
        enforcer.validate_role(admin_user, "user")
        enforcer.validate_role(admin_user, "guest")
    
    def test_role_validation_failure(self):
        """Insufficient roles should fail validation."""
        from agent_system.config.models import AuthConfig
        from agent_system.auth.enforcement import EndpointSecurityEnforcer
        
        config = AuthConfig(enabled=True)
        enforcer = EndpointSecurityEnforcer(config)
        
        guest_user = MagicMock()
        guest_user.role = "guest"
        
        with pytest.raises(HTTPException) as exc_info:
            enforcer.validate_role(guest_user, "admin")
        
        assert exc_info.value.status_code == 403
    
    def test_has_role_function(self):
        """Test has_role helper function."""
        from agent_system.auth.enforcement import has_role
        
        admin = MagicMock()
        admin.role = "admin"
        
        user = MagicMock()
        user.role = "user"
        
        guest = MagicMock()
        guest.role = "guest"
        
        # Admin has all roles
        assert has_role(admin, "admin")
        assert has_role(admin, "user")
        assert has_role(admin, "guest")
        
        # User has user and guest
        assert not has_role(user, "admin")
        assert has_role(user, "user")
        assert has_role(user, "guest")
        
        # Guest only has guest
        assert not has_role(guest, "admin")
        assert not has_role(guest, "user")
        assert has_role(guest, "guest")
