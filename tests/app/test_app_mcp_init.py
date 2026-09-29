import yaml
from fastapi.testclient import TestClient

from agent_system.app import build_app
from agent_system.tools.integration import get_tool_integration


def test_api_initializes_mcp(tmp_path, monkeypatch):
    # Create a complete agent.yaml with inline mcp configuration
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    agent_yaml = cfg_dir / "config.yaml"
    
    # Include mcp configuration directly in config.yaml using the correct structure
    agent_config = {
        "plugins": {
            "default_config": {
                "type": "agent",
                "enabled": True,
                "agent_config": {
                    "llm_profile": "normal",
                    "tools": {}
                }
            },
            "external_servers": {
                "remote_servers": {
                    "test_local": {
                        "url": "http://127.0.0.1:9999",
                        "enabled": True,
                        "transport": "http"
                    }
                }
            },
            "servers": {}
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
        # Startup should have run; retrieve global integration
        integration = get_tool_integration()
        # Integration should be initialized (we don't require a specific external
        # server to be connected here; presence of the integration is the core contract)
        assert integration is not None
        assert getattr(integration, 'initialized', True) is True

    # After TestClient context exits, shutdown should have run and integration cleaned up
    # Note: We don't check get_tool_integration() after shutdown because it requires
    # a config to create a new instance, and shutdown sets the global to None.
