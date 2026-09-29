"""The schema loader must build ONE Jinja environment per plugin directory and
still render per call -- 203 basic_agent instances share a schema.yaml but
each renders its own ``{{ name }}``.
"""
from __future__ import annotations

from pathlib import Path

import jinja2

from agent_system.plugins import schema_loader

BASIC_AGENT_DIR = Path(__file__).resolve().parents[2] / "src" / "plugins" / "basic_agent"


def test_environment_is_built_once_per_directory_and_still_renders_per_call(monkeypatch):
    built = []
    original_init = jinja2.Environment.__init__

    def counting_init(self, *args, **kwargs):
        built.append(1)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(jinja2.Environment, "__init__", counting_init)
    schema_loader._environment.cache_clear()

    first = schema_loader.load_schema_from_dir(BASIC_AGENT_DIR, template_vars={
        "name": "alpha", "llm_profiles": ["x"], "has_advanced": False})
    second = schema_loader.load_schema_from_dir(BASIC_AGENT_DIR, template_vars={
        "name": "beta", "llm_profiles": ["x"], "has_advanced": False})

    assert first and second, "fixture rendered nothing"
    names_first = [t["function"]["name"] for t in first["tools"]]
    names_second = [t["function"]["name"] for t in second["tools"]]
    assert "alpha_execute_task" in names_first
    assert "beta_execute_task" in names_second
    assert len(built) == 1, f"expected one Environment for one directory, built {len(built)}"


def test_editing_the_template_is_seen_by_the_cached_environment(tmp_path):
    """auto_reload: a cached environment must not pin a stale template."""
    schema = tmp_path / "schema.yaml"
    schema.write_text("tools:\n  - type: function\n    function:\n      name: '{{ name }}_one'\n",
                      encoding="utf-8")
    schema_loader._environment.cache_clear()
    before = schema_loader.load_schema_from_dir(tmp_path, template_vars={"name": "n"})
    assert before["tools"][0]["function"]["name"] == "n_one"

    import os
    import time
    schema.write_text("tools:\n  - type: function\n    function:\n      name: '{{ name }}_two'\n",
                      encoding="utf-8")
    # mtime resolution on some filesystems is coarse; push it forward explicitly
    later = time.time() + 5
    os.utime(schema, (later, later))

    after = schema_loader.load_schema_from_dir(tmp_path, template_vars={"name": "n"})
    assert after["tools"][0]["function"]["name"] == "n_two"
