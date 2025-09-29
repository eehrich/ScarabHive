import os
from pathlib import Path
import yaml

from agent_system.config.settings import load_settings


def test_load_settings_from_default_file(tmp_path, monkeypatch):
    cfg = {
        "llm_system": {
            "models": {
                "gpt-test": {"provider": "openai", "model": "gpt-test", "openai_api_key": "file-key"}
            },
            "profiles": {
                "normal": {"model_ref": "gpt-test"}
            },
            "default_profile": "normal"
        },
        "max_steps": 2,
    }
    cfg_path = tmp_path / "agent.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    # Ensure loader picks up file when provided path
    result = load_settings(str(cfg_path))
    assert result.llm_system.models["gpt-test"].model == "gpt-test"
    assert result.max_steps == 2


def test_env_overrides_openai_key(tmp_path, monkeypatch):
    cfg = {
        "llm_system": {
            "models": {
                "gpt-test": {"provider": "openai", "model": "gpt-test", "openai_api_key": "file-key"}
            },
            "profiles": {
                "normal": {"model_ref": "gpt-test"}
            },
            "default_profile": "normal"
        }
    }
    cfg_path = tmp_path / "agent.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    result = load_settings(str(cfg_path))
    # The loader expands ${...} but does not automatically replace the value by env var name;
    # this test ensures the config still contains the file key unless the file uses ${OPENAI_API_KEY}.
    assert result.llm_system.models["gpt-test"].openai_api_key == "file-key"
