"""A command line (agent-run, agent-cli) builds its agents in a registry of its own: a plugin that looks up a
sibling server -- the subscription provider its coding_cli instance, the MCP gateway its runner agent -- finds it in
``app_state.registry``. ``app_state.app_registry`` stays the API's: plugins read it as the sign that they run there.
"""
from agent_system import app_state
from agent_system.config.models import AgentSystemConfig
from agent_system.services.initialization_service import InitializationService


def test_the_cli_names_its_registry_without_claiming_the_api(monkeypatch):
    monkeypatch.setattr(InitializationService, "bootstrap_and_inject", lambda self, inject_sessions: "the registry")
    registry, _ = InitializationService(AgentSystemConfig()).initialize_for_cli()
    assert registry == "the registry"
    assert app_state.registry == "the registry"
    assert app_state.app_registry is None
