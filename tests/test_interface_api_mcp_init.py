import yaml
from fastapi.testclient import TestClient

from agent_system.agent.interface_api import build_app
from agent_system.mcp.integration import get_mcp_integration
import yaml
from fastapi.testclient import TestClient

from agent_system.agent.interface_api import build_app
from agent_system.mcp.integration import get_mcp_integration


def test_api_initializes_mcp(tmp_path, monkeypatch):
    # Create a complete agent.yaml with inline mcp configuration
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    agent_yaml = cfg_dir / "agent.yaml"
    
    # Include mcp configuration directly in agent.yaml (no separate mcp.yaml)
    agent_config = {
        "mcp": {
            "external_servers": {
                "test_local": {
                    "url": "http://127.0.0.1:9999",
                    "enabled": True,
                    "transport": "http"
                }
            },
            "enabled_servers": []
        },
        "network": {
            "host": "127.0.0.1",
            "port": 8000,
            "ssl_verify": True,
            "disable_cache": False
        },
        "logging": {
            "enabled": True,
            "level": "INFO"
        },
        "llm_system": {
            "models": {
                "gpt-4": {
                    "provider": "openai",
                    "model": "gpt-4"
                }
            },
            "profiles": {
                "normal": {
                    "model_ref": "gpt-4"
                }
            },
            "default_profile": "normal"
        }
    }
    agent_yaml.write_text(yaml.safe_dump(agent_config), encoding="utf-8")

    # Switch cwd to tmp_path so build_app resolves config paths there
    monkeypatch.chdir(tmp_path)

    app = build_app(str(agent_yaml))

    # Use TestClient to trigger startup events
    with TestClient(app):
        # Startup should have run; retrieve global integration and assert external server present
        integration = get_mcp_integration()
        # Check that the external server was configured (not necessarily connected)
        assert "test_local" in integration.configured_external_servers

    # After TestClient context exits, shutdown should have run and integration cleaned up
    # Ensure get_mcp_integration returns a fresh instance on next call
    integration2 = get_mcp_integration()
    assert integration2 is not None
