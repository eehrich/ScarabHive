"""Tests for the skills feature (docs/skills_design.md).

Covers discovery/manifest handling and the prompt merge, including the
properties that matter operationally: deterministic order (prompt-cache
stability), loud failure on a missing skill, and a broken skill never taking
the run down.
"""
import pytest

from agent_system.config.models import AgentConfig, AgentSystemConfig, SkillsConfig
from agent_system.servers.agent.prompt_strategies import PromptContext, PromptRenderer
from agent_system.skills.registry import SkillRegistry, default_skill_dirs


def _write_skill(root, name, body, *, manifest=None, entry="SKILL.md"):
    """Create a skill directory; returns its path."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / entry).write_text(body, encoding="utf-8")
    (d / "skill.toml").write_text(
        manifest if manifest is not None
        else f'[skill]\nname = "{name}"\nversion = "1.0.0"\ndescription = "d"\n',
        encoding="utf-8",
    )
    return d


@pytest.fixture
def skill_root(tmp_path):
    return tmp_path / "skills"


def _context(agent_config, skill_dirs=None):
    return PromptContext(
        agent_name="test_agent",
        agent_config=agent_config,
        system_config=AgentSystemConfig(
            skills={"skill_dirs": [str(d) for d in (skill_dirs or [])]}
        ),
        available_tools=[],
        max_steps=5,
        current_step=1,
        agent_instance=object(),  # no get_custom_system_prompt -> strategy skipped
    )


class TestDiscovery:
    def test_discovers_skill(self, skill_root):
        _write_skill(skill_root, "alpha", "ALPHA-BODY")
        reg = SkillRegistry()
        reg.discover([str(skill_root)])

        skill = reg.get("alpha")
        assert skill is not None
        assert skill.version == "1.0.0"
        assert reg.names() == ["alpha"]

    def test_name_from_manifest_wins_over_dirname(self, skill_root):
        _write_skill(
            skill_root, "dir-name", "B",
            manifest='[skill]\nname = "manifest-name"\n',
        )
        reg = SkillRegistry()
        reg.discover([str(skill_root)])
        assert reg.names() == ["manifest-name"]

    def test_directory_without_manifest_is_ignored(self, skill_root):
        (skill_root / "no-manifest").mkdir(parents=True)
        (skill_root / "no-manifest" / "SKILL.md").write_text("x", encoding="utf-8")
        reg = SkillRegistry()
        reg.discover([str(skill_root)])
        assert reg.names() == []

    def test_broken_manifest_is_skipped_not_raised(self, skill_root):
        _write_skill(skill_root, "good", "GOOD")
        _write_skill(skill_root, "bad", "BAD", manifest="this is not toml {{{")
        reg = SkillRegistry()
        reg.discover([str(skill_root)])  # must not raise
        assert reg.names() == ["good"]

    def test_missing_entry_file_is_skipped(self, skill_root):
        d = skill_root / "no-body"
        d.mkdir(parents=True)
        (d / "skill.toml").write_text('[skill]\nname = "no-body"\n', encoding="utf-8")
        reg = SkillRegistry()
        reg.discover([str(skill_root)])
        assert reg.names() == []

    def test_missing_root_is_tolerated(self, tmp_path):
        reg = SkillRegistry()
        reg.discover([str(tmp_path / "does-not-exist")])
        assert reg.names() == []

    def test_first_root_wins_on_duplicate(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        _write_skill(a, "dup", "FROM-A")
        _write_skill(b, "dup", "FROM-B")
        reg = SkillRegistry()
        reg.discover([str(a), str(b)])
        assert reg.get("dup").entry_path.read_text(encoding="utf-8") == "FROM-A"

    def test_rediscover_replaces_contents(self, skill_root):
        _write_skill(skill_root, "alpha", "A")
        reg = SkillRegistry()
        reg.discover([str(skill_root)])
        assert reg.names() == ["alpha"]
        reg.discover([])  # operator pointed it elsewhere
        assert reg.names() == []

    def test_env_var_overrides_default_dirs(self, monkeypatch):
        monkeypatch.delenv("AGENT_SKILL_DIRS", raising=False)
        assert default_skill_dirs() == ("skills",)
        import os
        monkeypatch.setenv("AGENT_SKILL_DIRS", f"one{os.pathsep}two")
        assert default_skill_dirs() == ("one", "two")


class TestConfig:
    def test_bare_list_shorthand(self):
        cfg = AgentConfig(skills=["a", "b"])
        assert cfg.skills.always == ["a", "b"]

    def test_structured_form(self):
        cfg = AgentConfig(skills={"always": ["a"]})
        assert cfg.skills.always == ["a"]

    def test_absent_by_default(self):
        assert AgentConfig().skills is None

    def test_empty_config_has_no_skills(self):
        assert SkillsConfig().always == []


class TestSkillDirsConfig:
    """skills.skill_dirs is configurable like plugins.plugin_dirs."""

    def test_default_is_empty_so_fallback_applies(self):
        assert AgentSystemConfig().skills.skill_dirs == []

    def test_dirs_from_config(self):
        cfg = AgentSystemConfig(skills={"skill_dirs": ["a", "b"]})
        assert cfg.skills.skill_dirs == ["a", "b"]

    def test_multiple_roots_are_scanned(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        _write_skill(a, "from-a", "A")
        _write_skill(b, "from-b", "B")
        reg = SkillRegistry()
        reg.discover([str(a), str(b)])
        assert reg.names() == ["from-a", "from-b"]

    def test_ensure_discovered_skips_rescan_for_same_dirs(self, skill_root):
        """The render path calls this per LLM call — it must not re-scan disk."""
        _write_skill(skill_root, "alpha", "A")
        reg = SkillRegistry()
        reg.ensure_discovered([str(skill_root)])
        assert reg.names() == ["alpha"]

        # Add a skill on disk; same roots -> no re-scan, so it stays invisible.
        _write_skill(skill_root, "beta", "B")
        reg.ensure_discovered([str(skill_root)])
        assert reg.names() == ["alpha"]

        # Explicit discover() is the reload path and does pick it up.
        reg.discover([str(skill_root)])
        assert reg.names() == ["alpha", "beta"]

    def test_ensure_discovered_rescans_when_dirs_change(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        _write_skill(a, "from-a", "A")
        _write_skill(b, "from-b", "B")
        reg = SkillRegistry()
        reg.ensure_discovered([str(a)])
        assert reg.names() == ["from-a"]
        reg.ensure_discovered([str(b)])
        assert reg.names() == ["from-b"]


class TestBundleAccess:
    """A skill is a bundle: SKILL.md plus reference files loaded on demand."""

    def _skill(self, root, name="alpha"):
        d = _write_skill(root, name, "BODY")
        (d / "reference").mkdir()
        (d / "reference" / "deep.md").write_text("DEEP-CONTENT", encoding="utf-8")
        reg = SkillRegistry()
        reg.discover([str(root)])
        return reg.get(name)

    def test_lists_bundled_files_without_manifest(self, skill_root):
        skill = self._skill(skill_root)
        assert skill.list_files() == ["SKILL.md", "reference/deep.md"]

    def test_resolves_and_reads_bundled_file(self, skill_root):
        skill = self._skill(skill_root)
        assert skill.resolve("reference/deep.md").read_text(encoding="utf-8") == "DEEP-CONTENT"

    def test_rejects_parent_traversal(self, skill_root):
        skill = self._skill(skill_root)
        with pytest.raises(ValueError):
            skill.resolve("../../etc/passwd")

    @pytest.mark.parametrize("attack", [
        "/etc/passwd",              # NOT absolute on Windows (no drive) —
                                    # must still be caught by the containment
                                    # check, not only by is_absolute()
        "C:/Windows/win.ini",
        "../../config/config.yaml",
        "reference/../../../outside.md",
    ])
    def test_rejects_paths_outside_the_bundle(self, skill_root, attack):
        skill = self._skill(skill_root)
        with pytest.raises((ValueError, FileNotFoundError)):
            skill.resolve(attack)

    def test_missing_file_raises_not_found(self, skill_root):
        skill = self._skill(skill_root)
        with pytest.raises(FileNotFoundError):
            skill.resolve("reference/nope.md")


class TestOnDemandIndex:
    """on_demand contributes ONLY a description index — the agent must know a
    skill exists, or it will never fetch it."""

    def _render(self, monkeypatch, skill_root, skills):
        skill_root.mkdir(parents=True, exist_ok=True)
        reg = SkillRegistry()
        monkeypatch.setattr(
            "agent_system.skills.get_skill_registry", lambda *a, **k: reg
        )
        cfg = AgentConfig(system_prompt="BASE-PROMPT", skills=skills)
        return PromptRenderer().render(_context(cfg, [skill_root]))[0]

    def test_index_lists_description_not_body(self, monkeypatch, skill_root):
        _write_skill(
            skill_root, "alpha", "FULL-BODY-TEXT",
            manifest='[skill]\nname = "alpha"\ndescription = "Covers X, use when Y."\n',
        )
        out = self._render(monkeypatch, skill_root, {"on_demand": ["alpha"]})
        assert "Covers X, use when Y." in out
        assert "FULL-BODY-TEXT" not in out  # body stays out of the prompt

    def test_always_and_on_demand_combine(self, monkeypatch, skill_root):
        _write_skill(skill_root, "core", "CORE-BODY")
        _write_skill(
            skill_root, "deep", "DEEP-BODY",
            manifest='[skill]\nname = "deep"\ndescription = "Deep material."\n',
        )
        out = self._render(
            monkeypatch, skill_root, {"always": ["core"], "on_demand": ["deep"]}
        )
        assert "CORE-BODY" in out          # always -> full body
        assert "Deep material." in out     # on_demand -> description only
        assert "DEEP-BODY" not in out

    def test_no_index_when_only_always(self, monkeypatch, skill_root):
        _write_skill(skill_root, "core", "CORE-BODY")
        out = self._render(monkeypatch, skill_root, {"always": ["core"]})
        assert "Available skills" not in out

    def test_missing_on_demand_skill_is_reported(self, monkeypatch, skill_root, caplog):
        import agent_system.servers.agent.prompt_strategies as ps
        ps._reported_missing_skills.clear()
        skill_root.mkdir(parents=True, exist_ok=True)
        with caplog.at_level("ERROR"):
            out = self._render(monkeypatch, skill_root, {"on_demand": ["ghost"]})
        assert "ghost" in caplog.text
        assert out.strip() == "BASE-PROMPT"  # empty index is not appended


class TestPromptMerge:
    """The merge sits in PromptRenderer.render(), so it applies to every
    prompt strategy — here exercised through the raw-prompt strategy."""

    def _render(self, monkeypatch, skill_root, skills):
        """Render through the real path: roots come from system_config, and the
        renderer discovers them itself (only the process-wide singleton is
        swapped for a fresh one so tests stay isolated)."""
        skill_root.mkdir(parents=True, exist_ok=True)
        reg = SkillRegistry()
        monkeypatch.setattr(
            "agent_system.skills.get_skill_registry", lambda *a, **k: reg
        )
        cfg = AgentConfig(system_prompt="BASE-PROMPT", skills=skills)
        return PromptRenderer().render(_context(cfg, [skill_root]))[0]

    def test_skill_body_is_appended(self, monkeypatch, skill_root):
        _write_skill(skill_root, "alpha", "ALPHA-BODY")
        out = self._render(monkeypatch, skill_root, ["alpha"])
        assert "BASE-PROMPT" in out
        assert "ALPHA-BODY" in out
        assert out.index("BASE-PROMPT") < out.index("ALPHA-BODY")

    def test_order_follows_config_not_disk(self, monkeypatch, skill_root):
        """Deterministic order keeps the cached prefix byte-identical."""
        _write_skill(skill_root, "alpha", "A-BODY")
        _write_skill(skill_root, "beta", "B-BODY")
        out = self._render(monkeypatch, skill_root, ["beta", "alpha"])
        assert out.index("B-BODY") < out.index("A-BODY")

    def test_render_is_stable_across_calls(self, monkeypatch, skill_root):
        _write_skill(skill_root, "alpha", "A-BODY")
        first = self._render(monkeypatch, skill_root, ["alpha"])
        second = self._render(monkeypatch, skill_root, ["alpha"])
        assert first == second  # byte-identical -> prompt cache survives

    def test_no_skills_leaves_prompt_untouched(self, monkeypatch, skill_root):
        skill_root.mkdir(parents=True, exist_ok=True)
        assert self._render(monkeypatch, skill_root, None) == "BASE-PROMPT"

    def test_missing_skill_is_omitted_and_does_not_raise(self, monkeypatch, skill_root, caplog):
        _write_skill(skill_root, "alpha", "ALPHA-BODY")
        import agent_system.servers.agent.prompt_strategies as ps
        ps._reported_missing_skills.clear()
        with caplog.at_level("ERROR"):
            out = self._render(monkeypatch, skill_root, ["alpha", "ghost"])
        assert "ALPHA-BODY" in out
        assert "ghost" in caplog.text  # loud, not silent

    def test_jinja_variables_render_in_skill_body(self, monkeypatch, skill_root):
        _write_skill(skill_root, "alpha", "steps={{ max_steps }}")
        out = self._render(monkeypatch, skill_root, ["alpha"])
        assert "steps=5" in out

    def test_include_works_in_skill_body(self, monkeypatch, skill_root):
        d = _write_skill(skill_root, "alpha", 'X {% include "partial.md" %} Y')
        (d / "partial.md").write_text("PARTIAL-CONTENT", encoding="utf-8")
        out = self._render(monkeypatch, skill_root, ["alpha"])
        assert "PARTIAL-CONTENT" in out

    def test_broken_skill_body_does_not_kill_the_run(self, monkeypatch, skill_root):
        _write_skill(skill_root, "alpha", "ALPHA-BODY")
        _write_skill(skill_root, "broken", '{% include "nope-missing.md" %}')
        out = self._render(monkeypatch, skill_root, ["alpha", "broken"])
        assert "BASE-PROMPT" in out and "ALPHA-BODY" in out
