"""Runtime._check_required_plugins: a declared server whose manifest names a plugin that is not installed is a
problem at start, not an ImportError later."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_system.runtime import Runtime, ServerDecl


def _runtime(decls: dict[str, ServerDecl], factories: dict) -> Runtime:
    runtime = Runtime.__new__(Runtime)
    runtime._decls = decls
    runtime._plugins = factories
    runtime.problems = []
    return runtime


def _decl(name: str, needs: list[str]) -> ServerDecl:
    return ServerDecl(name=name, type=name, server_config=SimpleNamespace(), factory=None,
                      plugin_metadata={"plugins": needs})


def test_a_missing_plugin_is_a_problem(tmp_path: Path):
    (tmp_path / "inventory_scan").mkdir()
    (tmp_path / "inventory_scan" / "plugin.toml").write_text("[plugin]\nname = 'inventory_scan'\n", encoding="utf-8")
    runtime = _runtime({"report_drafts": _decl("report_drafts", ["issue_tracker", "inventory_scan"])}, {})

    runtime._check_required_plugins([tmp_path])

    assert runtime.problems == ["server 'report_drafts': needs plugin 'issue_tracker', which is not installed"]


def test_a_library_or_disabled_plugin_counts_as_installed(tmp_path: Path):
    """Installed is enough: a library has a manifest and no factory, a disabled plugin a factory and no server."""
    (tmp_path / "llm_common").mkdir()
    (tmp_path / "llm_common" / "plugin.toml").write_text("[plugin]\nname = 'llm_common'\n", encoding="utf-8")
    runtime = _runtime({"llm_openrouter": _decl("llm_openrouter", ["llm_common", "ticket_client"])}, {"ticket_client": object()})

    runtime._check_required_plugins([tmp_path])

    assert runtime.problems == []
