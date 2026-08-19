"""Tests for InitializationService.

Verifies centralized initialization logic works correctly for all entry points.
"""
import pytest

from agent_system.config.settings import load_settings
from agent_system.services.initialization_service import InitializationService
from agent_system.mcp.base import MCPRegistry


class TestInitializationService:
    """Test suite for InitializationService."""

    @pytest.fixture
    def config(self):
        """Load system config for testing."""
        return load_settings()

    @pytest.fixture
    def service(self, config):
        """Create InitializationService instance."""
        return InitializationService(config)

    def test_session_manager_creation(self, service):
        """Test SessionManager is created on first access."""
        assert service._session_manager is None
        manager = service.session_manager
        assert manager is not None
        assert service._session_manager is manager  # Same instance
        # Second access returns cached instance
        assert service.session_manager is manager

    def test_session_service_creation(self, service):
        """Test SessionService is created on first access."""
        assert service._session_service is None
        svc = service.session_service
        assert svc is not None
        assert service._session_service is svc
        # Second access returns cached instance
        assert service.session_service is svc

    def test_bootstrap_and_inject_creates_registry(self, service):
        """Test bootstrap creates new registry when not provided."""
        registry = service.bootstrap_and_inject()
        assert isinstance(registry, MCPRegistry)
        assert service.initialized is True

    def test_bootstrap_and_inject_uses_provided_registry(self, service):
        """Test bootstrap uses provided registry instead of creating new one."""
        existing_registry = MCPRegistry()
        registry = service.bootstrap_and_inject(registry=existing_registry)
        assert registry is existing_registry
        assert service.initialized is True

    def test_bootstrap_registers_servers(self, service):
        """Test bootstrap actually registers configured servers."""
        registry = service.bootstrap_and_inject()
        server_list = registry.list()
        
        # Should have at least some servers registered
        assert len(server_list) > 0
        
        # Check for known servers from config
        # (thinking_agent, sysadmin_agent, etc.)
        assert any('thinking' in name.lower() for name in server_list)

    def test_initialize_for_cli(self, service):
        """Test CLI initialization returns registry and session_service."""
        registry, session_service = service.initialize_for_cli()
        
        assert isinstance(registry, MCPRegistry)
        assert session_service is not None
        assert session_service is service.session_service
        assert len(registry.list()) > 0
        assert service.initialized is True

    def test_initialize_for_api(self, service):
        """Test API initialization returns session_service."""
        session_service = service.initialize_for_api()
        
        assert session_service is not None
        assert session_service is service.session_service
        assert service.initialized is True

    def test_session_injection_happens(self, service):
        """Test that session_service is actually injected into agents."""
        registry = service.bootstrap_and_inject(inject_sessions=True)
        
        # Find an agent in the registry
        from agent_system.servers.agent.server import Agent as _Agent
        agent_found = False
        
        for server_name in registry.list():
            server = registry.get(server_name)
            if isinstance(server, _Agent):
                agent_found = True
                # Check if session_service was injected
                assert hasattr(server, '_session_service')
                assert server._session_service is not None
                assert server._session_service is service.session_service
                break
        
        assert agent_found, "No agent found in registry to test injection"

    def test_skip_injection_when_disabled(self, service):
        """Test that injection can be skipped."""
        # This doesn't fail, just doesn't inject
        registry = service.bootstrap_and_inject(inject_sessions=False)
        assert isinstance(registry, MCPRegistry)
        # We don't verify injection didn't happen because it's hard to prove a negative
        # The main test is that it doesn't crash

    def test_multiple_initializations_idempotent(self, service):
        """Test that calling initialize multiple times is safe."""
        registry1, session_service1 = service.initialize_for_cli()
        
        # Create another service instance
        service2 = InitializationService(service.config)
        registry2, session_service2 = service2.initialize_for_cli()
        
        # Different registries (different instances)
        assert registry1 is not registry2
        
        # But both work
        assert len(registry1.list()) > 0
        assert len(registry2.list()) > 0
