import os
from pathlib import Path

from agent_system.config.loader import load_config


def test_load_config_merges_includes_and_prefers_included(tmp_path):
    # Create a master manifest that includes mcp.yaml
    master = tmp_path / "agent.yaml"
    included = tmp_path / "mcp.yaml"

    master.write_text(
        """
llm_system:
  models:
    master-model:
      provider: "openai"
      model: "master-model"
  profiles:
    normal:
      model_ref: "master-model"
  default_profile: "normal"
includes:
  - mcp.yaml
""",
        encoding="utf-8",
    )

    included.write_text(
        """
mcp:
  enabled_servers:
    - custom_plugin
servers:
  custom_plugin:
    type: custom_plugin
""",
        encoding="utf-8",
    )

    cfg = load_config(str(master))

    # The included mcp.yaml should be merged into the returned config
    # Check the model through the new llm_system structure
    default_profile = cfg.llm_system.default_profile
    model_ref = cfg.llm_system.profiles[default_profile].model_ref
    assert cfg.llm_system.models[model_ref].model == "master-model"
    assert isinstance(cfg.mcp.enabled_servers, list)
    assert cfg.mcp.enabled_servers == ["custom_plugin"]
    assert "custom_plugin" in cfg.servers
