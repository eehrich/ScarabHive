import logging
from pathlib import Path

from agent_system.config.settings import load_settings


def test_logging_explicit(tmp_path, monkeypatch):
    # Create a minimal config with explicit file_cli and file_api
    cfg_text = """
logging:
  enabled: true
  level: DEBUG
  file: logs/agent.log
  file_cli: logs/cli-explicit.log
  file_api: logs/api-explicit.log
"""
    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text(cfg_text)

    cfg = load_settings(str(cfg_file))
    assert cfg.logging.file_cli == "logs/cli-explicit.log"
    assert cfg.logging.file_api == "logs/api-explicit.log"


def test_logging_fallback(tmp_path):
    # Create a minimal config with only single file set
    cfg_text = """
logging:
  enabled: true
  level: DEBUG
  file: logs/agent.log
"""
    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text(cfg_text)

    cfg = load_settings(str(cfg_file))
    # file_cli and file_api default to None; callers should derive role-specific names
    assert cfg.logging.file_cli is None
    assert cfg.logging.file_api is None
    # Ensure base file is present
    assert cfg.logging.file == "logs/agent.log"
