"""No enabled file_ops instance of the shipped config reaches the secrets or the user store.

workspace_file_ops shipped enabled with allowed_directories ["."]: the whole
checkout, config/secrets.env and data/users.db included, for any agent given
it. This builds every enabled file_ops-type server of the shipped config --
inheritance resolved -- in a directory standing in for the checkout, and asks
its own path validator.

The coder harness is the one deliberate exception: it works on this
repository itself, and src/plugins/coder/agents/tools.yaml says so and how to
take it back. It is named here, so any other instance that reaches these
paths -- and a narrowed coder_fs that no longer does -- turns this red.
"""
from __future__ import annotations


from agent_system.config import settings
from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.config.models import AgentSystemConfig

PRIVATE = ("config/secrets.env", "data/users.db", "data/sessions/alice/s1.json")

#: The coder harness's read-write and read-only file access include "." on purpose.
REPOSITORY_WIDE = {"coder_fs", "coder_fs_ro"}


def _enabled_file_ops_instances(monkeypatch):
    monkeypatch.setattr(settings, "_load_secrets_file", lambda path: None)  # keys stay out of this process
    config = load_settings(str(settings.Path(__file__).resolve().parents[2] / "config" / "config.yaml"))
    names = [name for name, raw in (config.plugins.servers or {}).items() if raw.enabled]
    resolved = {name: get_tool_server_config(name, config) for name in names}
    return {name: cfg for name, cfg in resolved.items() if cfg is not None and cfg.type == "file_ops"}


def test_only_the_coder_harness_reaches_secrets_or_users(monkeypatch, tmp_path):
    from plugins.file_ops.security import SecurityError
    from plugins.file_ops.server import FileOpsServer

    instances = _enabled_file_ops_instances(monkeypatch)
    assert "file_ops" in instances, sorted(instances)
    monkeypatch.chdir(tmp_path)  # the checkout the server would run in
    reached = []
    for name, cfg in instances.items():
        validator = FileOpsServer(name, AgentSystemConfig(), cfg).validator
        for path in PRIVATE:
            try:
                validator.validate_path(path)
            except SecurityError:
                continue
            reached.append(name)
    assert set(reached) == REPOSITORY_WIDE, sorted(set(reached))
