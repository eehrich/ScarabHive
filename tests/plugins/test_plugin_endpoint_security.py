"""
Tests for plugin endpoint security enforcement.

Tests the PluginEndpointSecurityEnforcer class which handles:
- Plugin-level security policies
- Per-plugin overrides
- Pattern-based endpoint rules
- Audit logging
"""
import pytest
from unittest.mock import MagicMock

from agent_system.plugins.web_adapter import (
    PluginEndpointSecurityEnforcer,
    get_plugin_security_enforcer,
    init_plugin_security,
)
from agent_system.config.models import (
    AuthConfig,
    PluginSecurityConfig,
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
        plugin_security=PluginSecurityConfig(
            default_policy="require_auth",
            default_min_role="user",
            audit_enabled=True,
            plugin_overrides={},
            endpoint_rules=[],
        ),
    )


@pytest.fixture
def auth_config_with_overrides():
    """Auth config with plugin-specific overrides."""
    return AuthConfig(
        enabled=True,
        plugin_security=PluginSecurityConfig(
            default_policy="require_auth",
            default_min_role="user",
            audit_enabled=True,
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
    """Tests for audit logging functionality."""

    def test_audit_log_enabled(self, basic_auth_config):
        """Test audit logging when enabled."""
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)
        
        enforcer.audit_access(
            plugin_name="test",
            path="/plugins/test/action",
            method="POST",
            user_id="testuser",
            allowed=True,
            reason="Test access"
        )
        
        log = enforcer.get_audit_log()
        assert len(log) == 1
        assert log[0]["plugin_name"] == "test"
        assert log[0]["user_id"] == "testuser"
        assert log[0]["allowed"] is True

    def test_audit_log_disabled(self, basic_auth_config):
        """Test audit logging when disabled."""
        basic_auth_config.plugin_security.audit_enabled = False
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)
        
        enforcer.audit_access(
            plugin_name="test",
            path="/plugins/test/action",
            method="POST",
            user_id="testuser",
            allowed=True,
            reason="Test access"
        )
        
        log = enforcer.get_audit_log()
        assert len(log) == 0

    def test_audit_log_filter_by_plugin(self, basic_auth_config):
        """Test filtering audit log by plugin name."""
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)
        
        enforcer.audit_access("plugin_a", "/a", "GET", "user1", True, "ok")
        enforcer.audit_access("plugin_b", "/b", "GET", "user2", True, "ok")
        enforcer.audit_access("plugin_a", "/a2", "POST", "user1", False, "denied")
        
        # Filter by plugin_a
        log = enforcer.get_audit_log(plugin_name="plugin_a")
        assert len(log) == 2
        assert all(e["plugin_name"] == "plugin_a" for e in log)

    def test_audit_log_limit(self, basic_auth_config):
        """Test audit log respects limit."""
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)
        
        # Add more than limit entries
        for i in range(20):
            enforcer.audit_access("test", f"/test/{i}", "GET", "user", True, "ok")
        
        log = enforcer.get_audit_log(limit=5)
        assert len(log) == 5

    def test_audit_log_max_entries(self, basic_auth_config):
        """Test audit log doesn't exceed 1000 entries."""
        enforcer = PluginEndpointSecurityEnforcer(basic_auth_config)
        
        # Add more than 1000 entries
        for i in range(1100):
            enforcer.audit_access("test", f"/test/{i}", "GET", "user", True, "ok")
        
        # Internal list should be capped at 1000
        assert len(enforcer._audit_log) == 1000


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
        assert config.audit_enabled is True
        assert config.plugin_overrides == {}
        assert len(config.endpoint_rules) == 1  # Default admin rule

    def test_auth_config_includes_plugin_security(self):
        """Test AuthConfig includes plugin_security field."""
        config = AuthConfig()
        
        assert hasattr(config, 'plugin_security')
        assert isinstance(config.plugin_security, PluginSecurityConfig)
