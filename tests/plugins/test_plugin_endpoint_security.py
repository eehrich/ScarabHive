"""
Tests for plugin endpoint security enforcement.

Tests the PluginEndpointSecurityEnforcer class which handles:
- Plugin-level security policies
- Per-plugin overrides
- Pattern-based endpoint rules
- Audit logging
"""
import pytest

from agent_system.plugins.web_adapter import (
    PluginEndpointSecurityEnforcer,
    get_plugin_security_enforcer,
    init_plugin_security,
)
from agent_system.config.models import (
    AuthConfig,
    PluginSecurityConfig,
    EndpointSecurityConfig,
    EndpointSecurityRule,
)


# ===========================
# Fixtures
# ===========================

@pytest.fixture
def basic_auth_config():
    """Basic auth config with plugin security enabled."""
    return AuthConfig(
        enabled=True,
        endpoint_security=EndpointSecurityConfig(audit_enabled=True),
        plugin_security=PluginSecurityConfig(
            default_policy="require_auth",
            default_min_role="user",
            plugin_overrides={},
            endpoint_rules=[],
        ),
    )


@pytest.fixture
def auth_config_with_overrides():
    """Auth config with plugin-specific overrides."""
    return AuthConfig(
        enabled=True,
        endpoint_security=EndpointSecurityConfig(audit_enabled=True),
        plugin_security=PluginSecurityConfig(
            default_policy="require_auth",
            default_min_role="user",
            plugin_overrides={
                "public_plugin": {"policy": "allow_anonymous"},
                "admin_tools": {"min_role": "admin"},
            },
            endpoint_rules=[
                EndpointSecurityRule(
                    pattern="/plugins/*/admin/*",
                    policy="require_auth",
                    min_role="admin",
                    description="Admin endpoints"
                ),
                EndpointSecurityRule(
                    pattern="GET /plugins/todo/panel",
                    policy="allow_anonymous",
                    description="Public todo panel"
                ),
            ],
        ),
    )


@pytest.fixture
def auth_disabled_config():
    """Auth config with auth disabled."""
    return AuthConfig(enabled=False)


# ===========================
# PluginEndpointSecurityEnforcer Tests
# ===========================

class TestPluginEndpointSecurityEnforcer:
    """Tests for PluginEndpointSecurityEnforcer class."""

    def test_enforcer_creation(self, basic_auth_config):
        """Test creating an enforcer."""
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)
        assert enforcer.auth_config == basic_auth_config

    def test_auth_disabled_policy(self, auth_disabled_config):
        """Test policy when auth is disabled."""
        enforcer = PluginEndpointSecurityEnforcer(auth_disabled_config)
        policy = enforcer.get_plugin_policy("todo", "/plugins/todo/panel", "GET")
        
        assert policy["requires_auth"] is False
        assert policy["min_role"] is None
        assert "disabled" in policy["description"].lower()

    def test_default_policy(self, basic_auth_config):
        """Test default policy for unknown plugin."""
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)
        policy = enforcer.get_plugin_policy("unknown_plugin", "/plugins/unknown/test", "GET")
        
        assert policy["requires_auth"] is True
        assert policy["min_role"] == "user"
        assert "default" in policy["description"].lower()

    def test_plugin_override_anonymous(self, auth_config_with_overrides):
        """Test plugin-specific override for anonymous access."""
        enforcer = PluginEndpointSecurityEnforcer(auth_config_with_overrides)
        policy = enforcer.get_plugin_policy("public_plugin", "/plugins/public_plugin/data", "GET")
        
        assert policy["requires_auth"] is False

    def test_plugin_override_admin_role(self, auth_config_with_overrides):
        """Test plugin-specific override for admin role."""
        enforcer = PluginEndpointSecurityEnforcer(auth_config_with_overrides)
        policy = enforcer.get_plugin_policy("admin_tools", "/plugins/admin_tools/users", "GET")
        
        assert policy["requires_auth"] is True
        assert policy["min_role"] == "admin"

    def test_endpoint_rule_takes_precedence(self, auth_config_with_overrides):
        """Test that endpoint rules take precedence over plugin overrides."""
        enforcer = PluginEndpointSecurityEnforcer(auth_config_with_overrides)
        
        # GET /plugins/todo/panel should be anonymous (endpoint rule)
        policy = enforcer.get_plugin_policy("todo", "/plugins/todo/panel", "GET")
        assert policy["requires_auth"] is False
        
        # POST /plugins/todo/panel should use default (no matching rule)
        policy = enforcer.get_plugin_policy("todo", "/plugins/todo/panel", "POST")
        assert policy["requires_auth"] is True

    def test_admin_wildcard_rule(self, auth_config_with_overrides):
        """Test wildcard pattern for admin endpoints."""
        enforcer = PluginEndpointSecurityEnforcer(auth_config_with_overrides)
        
        # /plugins/anything/admin/whatever should require admin
        policy = enforcer.get_plugin_policy("todo", "/plugins/todo/admin/settings", "GET")
        assert policy["requires_auth"] is True
        assert policy["min_role"] == "admin"


class TestPatternMatching:
    """Tests for pattern matching logic."""

    def test_exact_path_match(self, basic_auth_config):
        """Test exact path matching."""
        basic_auth_config.plugin_security.endpoint_rules = [
            EndpointSecurityRule(
                pattern="/plugins/test/exact",
                policy="allow_anonymous",
            )
        ]
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)
        
        policy = enforcer.get_plugin_policy("test", "/plugins/test/exact", "GET")
        assert policy["requires_auth"] is False
        
        # Different path should not match
        policy = enforcer.get_plugin_policy("test", "/plugins/test/other", "GET")
        assert policy["requires_auth"] is True

    def test_wildcard_path_match(self, basic_auth_config):
        """Test wildcard path matching."""
        basic_auth_config.plugin_security.endpoint_rules = [
            EndpointSecurityRule(
                pattern="/plugins/test/*",
                policy="allow_anonymous",
            )
        ]
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)
        
        # Should match any path under /plugins/test/
        assert enforcer.get_plugin_policy("test", "/plugins/test/a", "GET")["requires_auth"] is False
        assert enforcer.get_plugin_policy("test", "/plugins/test/b/c", "GET")["requires_auth"] is False

    def test_method_specific_rule(self, basic_auth_config):
        """Test method-specific rules."""
        basic_auth_config.plugin_security.endpoint_rules = [
            EndpointSecurityRule(
                pattern="POST /plugins/test/data",
                policy="require_auth",
                min_role="admin",
            ),
            EndpointSecurityRule(
                pattern="GET /plugins/test/data",
                policy="allow_anonymous",
            ),
        ]
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)
        
        # GET should be anonymous
        policy = enforcer.get_plugin_policy("test", "/plugins/test/data", "GET")
        assert policy["requires_auth"] is False
        
        # POST should require admin
        policy = enforcer.get_plugin_policy("test", "/plugins/test/data", "POST")
        assert policy["requires_auth"] is True
        assert policy["min_role"] == "admin"


class TestAuditLogging:
    """A refused plugin request is written to logs/security.log with the rule's reason."""

    @pytest.fixture
    def written(self, monkeypatch):
        import logging

        from agent_system.auth.middleware import AUDIT_LOGGER_NAME

        lines = []

        class Collect(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        monkeypatch.setattr(logging.getLogger(AUDIT_LOGGER_NAME), "handlers", [Collect()])
        return lines

    def test_a_refused_request_is_logged_with_its_reason(self, basic_auth_config, written):
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)

        enforcer.audit_denied("test", "/plugins/test/action", "POST", "testuser", "Insufficient role: required admin")
        enforcer.audit_denied("test", "/plugins/test/other", "GET", None, "Authentication required")

        assert written == [
            "DENIED | POST /plugins/test/action | plugin=test | user=testuser | Insufficient role: required admin",
            "DENIED | GET /plugins/test/other | plugin=test | user=anonymous | Authentication required",
        ]

    def test_nothing_is_logged_while_the_audit_is_off(self, basic_auth_config, written):
        basic_auth_config.endpoint_security.audit_enabled = False
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)

        enforcer.audit_denied("test", "/plugins/test/action", "POST", "testuser", "Insufficient role")

        assert written == []


class TestGlobalEnforcer:
    """Tests for global enforcer singleton."""

    def test_get_enforcer_creates_singleton(self):
        """Test that get_plugin_security_enforcer creates a singleton."""
        enforcer1 = get_plugin_security_enforcer()
        enforcer2 = get_plugin_security_enforcer()
        assert enforcer1 is enforcer2

    def test_init_updates_config(self, basic_auth_config):
        """Test that init_plugin_security updates config."""
        enforcer = init_plugin_security(basic_auth_config)
        assert enforcer.auth_config == basic_auth_config


class TestConfigIntegration:
    """Tests for config model integration."""

    def test_plugin_security_config_defaults(self):
        """Test PluginSecurityConfig default values."""
        config = PluginSecurityConfig()
        
        assert config.default_policy == "require_auth"
        assert config.default_min_role == "user"
        assert config.plugin_overrides == {}
        assert len(config.endpoint_rules) == 1  # Default admin rule

    def test_endpoint_security_config_audit_enabled(self):
        """Test EndpointSecurityConfig audit_enabled default value."""
        config = EndpointSecurityConfig()
        assert config.audit_enabled is True

    def test_auth_config_includes_plugin_security(self):
        """Test AuthConfig includes plugin_security field."""
        config = AuthConfig()
        
        assert hasattr(config, 'plugin_security')
        assert isinstance(config.plugin_security, PluginSecurityConfig)
