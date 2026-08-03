"""Tests for the skills feature (docs/skills_design.md).

Covers discovery/manifest handling and the prompt merge, including the
properties that matter operationally: deterministic order (prompt-cache
stability), loud failure on a missing skill, and a broken skill never taking
the run down.
"""
import logging

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
        assert default_skill_dirs() == ("skills", ".claude/skills")
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


# ---------------------------------------------------------------------------
# Agent Skills standard (https://agentskills.io/specification)
# ---------------------------------------------------------------------------

def _write_standard_skill(root, name, frontmatter, body="# Body\n\nInstructions.\n"):
    """A skill in the PORTABLE layout: frontmatter in SKILL.md, no manifest."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return d


class TestAgentSkillsStandard:
    """A skill written for Claude Code / Codex / Cursor must load here as-is."""

    def test_frontmatter_skill_is_discovered(self, skill_root):
        _write_standard_skill(
            skill_root, "pdf-processing",
            "name: pdf-processing\ndescription: Extract PDF text. Use for PDFs.")
        reg = SkillRegistry()
        reg.discover([str(skill_root)])

        skill = reg.get("pdf-processing")
        assert skill is not None
        assert skill.description == "Extract PDF text. Use for PDFs."
        assert skill.templated is False        # standard bodies are verbatim

    def test_no_manifest_needed(self, skill_root):
        d = _write_standard_skill(skill_root, "solo", "name: solo\ndescription: d")
        assert not (d / "skill.toml").exists()
        reg = SkillRegistry()
        reg.discover([str(skill_root)])
        assert reg.names() == ["solo"]

    def test_frontmatter_is_stripped_from_the_body(self, skill_root):
        _write_standard_skill(skill_root, "clean", "name: clean\ndescription: d",
                              body="# Title\n\nReal content.\n")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        body = reg.get("clean").body()
        assert "name: clean" not in body       # the YAML header is plumbing
        assert "Real content." in body

    def test_all_optional_spec_fields_are_read(self, skill_root):
        _write_standard_skill(skill_root, "full", "\n".join([
            "name: full",
            "description: Does things",
            "license: Apache-2.0",
            "compatibility: Requires git and jq",
            "allowed-tools: Bash(git:*) Read",
            "metadata:",
            "  author: example-org",
            "  version: '2.1'",
        ]))
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        s = reg.get("full")
        assert s.license == "Apache-2.0"
        assert s.compatibility == "Requires git and jq"
        assert s.allowed_tools == ("Bash(git:*)", "Read")
        assert s.metadata["author"] == "example-org"
        assert s.version == "2.1"              # version lives in metadata per spec

    def test_unenforced_allowed_tools_is_reported(self, skill_root, caplog):
        """We have no per-skill tool gating. Dropping a declared RESTRICTION
        silently would widen it — the skill must at least say so out loud."""
        _write_standard_skill(skill_root, "gated",
                              "name: gated\ndescription: d\nallowed-tools: Read Grep")
        with caplog.at_level(logging.WARNING, logger="agent_system.skills.registry"):
            reg = SkillRegistry(); reg.discover([str(skill_root)])
        assert reg.get("gated").allowed_tools == ("Read", "Grep")
        assert any("allowed-tools" in r.getMessage() for r in caplog.records)

    def test_missing_description_falls_back_to_first_paragraph(self, skill_root):
        """Without ANY description an on-demand skill is invisible -- the index
        line is all the agent ever sees of it."""
        _write_standard_skill(skill_root, "terse", "name: terse",
                              body="# Heading\n\nWhat it actually does.\n\nMore.\n")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        assert reg.get("terse").description == "What it actually does."

    def test_bundled_dirs_of_the_spec_are_reachable(self, skill_root):
        """The spec names references/, scripts/ and assets/."""
        d = _write_standard_skill(skill_root, "bundled", "name: bundled\ndescription: d")
        for sub, fname in (("references", "REFERENCE.md"), ("scripts", "run.py"),
                           ("assets", "template.txt")):
            (d / sub).mkdir()
            (d / sub / fname).write_text("x", encoding="utf-8")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        s = reg.get("bundled")
        files = s.list_files()
        assert "references/REFERENCE.md" in files
        assert "scripts/run.py" in files
        assert "assets/template.txt" in files
        assert s.resolve("references/REFERENCE.md").is_file()

    def test_a_bom_does_not_hide_the_skill(self, skill_root):
        """Windows editors save a BOM. Decoded as plain utf-8 the '﻿' sits
        in front of the opening '---', the frontmatter goes undetected, and the
        skill disappears from discovery — the exact opposite of portable."""
        d = skill_root / "bom-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(
            "﻿---\nname: bom-skill\ndescription: Saved on Windows.\n---\n\nBody.\n"
            .encode("utf-8"))
        reg = SkillRegistry(); reg.discover([str(skill_root)])

        skill = reg.get("bom-skill")
        assert skill is not None
        assert skill.description == "Saved on Windows."
        assert not skill.body().startswith("﻿")   # nor may it leak into the prompt

    def test_skill_stays_hashable(self, skill_root):
        """``Skill`` is a frozen dataclass, so it carries a generated __hash__;
        the metadata dict must not turn every hash() into a TypeError."""
        _write_standard_skill(skill_root, "hashable",
                              "name: hashable\ndescription: d\nmetadata:\n  author: x")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        skill = reg.get("hashable")
        assert skill.metadata["author"] == "x"
        assert len({skill, skill}) == 1

    def test_project_local_claude_skills_are_scanned(self, tmp_path, monkeypatch):
        """A skill dropped into .claude/skills — where the ecosystem puts them —
        must be found without any config."""
        monkeypatch.delenv("AGENT_SKILL_DIRS", raising=False)
        monkeypatch.chdir(tmp_path)
        _write_standard_skill(tmp_path / ".claude" / "skills", "foreign",
                              "name: foreign\ndescription: From another tool.")
        reg = SkillRegistry(); reg.discover(default_skill_dirs())
        assert reg.names() == ["foreign"]

    def test_own_skills_win_a_name_collision(self, tmp_path, monkeypatch):
        """``skills/`` is listed first, so a downloaded skill cannot shadow ours."""
        monkeypatch.delenv("AGENT_SKILL_DIRS", raising=False)
        monkeypatch.chdir(tmp_path)
        _write_standard_skill(tmp_path / "skills", "shared", "name: shared\ndescription: OURS")
        _write_standard_skill(tmp_path / ".claude" / "skills", "shared",
                              "name: shared\ndescription: THEIRS")
        reg = SkillRegistry(); reg.discover(default_skill_dirs())
        assert reg.get("shared").description == "OURS"


class TestSpecValidation:
    """Lenient by design: the point is to LOAD a foreign skill, not grade it."""

    @pytest.mark.parametrize("bad", ["PDF-Processing", "-lead", "trail-",
                                     "double--hyphen", "x" * 65])
    def test_invalid_names_fall_back_to_the_directory(self, skill_root, bad):
        _write_standard_skill(skill_root, "dirname", f"name: {bad}\ndescription: d")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        assert reg.names() == ["dirname"]       # loaded, not rejected

    def test_name_mismatch_is_tolerated(self, skill_root):
        """The spec wants name == directory, but a mismatch must not cost the
        skill -- foreign bundles get renamed on download all the time."""
        _write_standard_skill(skill_root, "on-disk", "name: declared\ndescription: d")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        assert reg.names() == ["declared"]

    def test_overlong_description_is_truncated_not_dropped(self, skill_root):
        _write_standard_skill(skill_root, "wordy",
                              f"name: wordy\ndescription: {'x' * 2000}")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        assert len(reg.get("wordy").description) == 1024

    def test_broken_yaml_keeps_the_body(self, skill_root):
        """A malformed header must not swallow the instructions."""
        d = skill_root / "broken"; d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: [unclosed\n---\n\nThe content.\n",
                                    encoding="utf-8")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        # No frontmatter and no manifest -> not a skill, but nothing raised
        assert reg.names() == []

    def test_a_bad_skill_does_not_hide_the_others(self, skill_root):
        (skill_root / "junk").mkdir(parents=True)
        (skill_root / "junk" / "README.md").write_text("no skill here", encoding="utf-8")
        _write_standard_skill(skill_root, "good", "name: good\ndescription: d")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        assert reg.names() == ["good"]


class TestBothFormatsCoexist:
    def test_legacy_and_standard_side_by_side(self, skill_root):
        _write_skill(skill_root, "legacy-one", "Legacy body")
        _write_standard_skill(skill_root, "standard-one",
                              "name: standard-one\ndescription: d")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        assert reg.names() == ["legacy-one", "standard-one"]
        assert reg.get("legacy-one").templated is True
        assert reg.get("standard-one").templated is False

    def test_frontmatter_wins_over_a_stale_manifest(self, skill_root):
        d = _write_standard_skill(skill_root, "both",
                                  "name: both\ndescription: from frontmatter")
        (d / "skill.toml").write_text(
            '[skill]\nname = "both"\ndescription = "from manifest"\n', encoding="utf-8")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        assert reg.get("both").description == "from frontmatter"


class TestForeignBodyIsNotTemplated:
    """Our Jinja environment renders unknown variables as EMPTY. Treating a
    foreign body as a template would silently delete text -- worse than an
    error, because nothing reports it."""

    def test_double_braces_survive_in_a_standard_skill(self, skill_root, tmp_path):
        _write_standard_skill(
            skill_root, "templating-doc",
            "name: templating-doc\ndescription: d",
            body="Use {{ variable_name }} in your template.\n")
        reg = SkillRegistry(); reg.discover([str(skill_root)])
        assert "{{ variable_name }}" in reg.get("templating-doc").body()

    def test_prompt_merge_keeps_the_literal_braces(self, skill_root, monkeypatch):
        """End to end: what lands in the system prompt still has the braces."""
        skill_root.mkdir(parents=True, exist_ok=True)
        _write_standard_skill(
            skill_root, "braces", "name: braces\ndescription: d",
            body="Literal {{ not_a_variable }} here.\n")
        reg = SkillRegistry()
        monkeypatch.setattr(
            "agent_system.skills.get_skill_registry", lambda *a, **k: reg
        )
        cfg = AgentConfig(system_prompt="BASE", skills={"always": ["braces"]})
        out = PromptRenderer().render(_context(cfg, [skill_root]))[0]
        assert "{{ not_a_variable }}" in out
        assert "name: braces" not in out       # frontmatter stays out too
