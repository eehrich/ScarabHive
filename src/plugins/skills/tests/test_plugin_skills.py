"""Tests for the skills plugin (tool surface over the core skill registry)."""
import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.skills.server import MAX_READ_CHARS, SkillsServer


def _write_skill(root, name, body, *, description="d", extra=None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"metadata:\n  version: '1.0.0'\n---\n\n{body}",
        encoding="utf-8",
    )
    for rel, content in (extra or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return d


class MockStatus:
    """Captures status events so tests can assert what the operator sees."""

    def __init__(self):
        self.messages = []

    async def progress(self, message: str, meta=None):
        self.messages.append(("progress", message))

    async def end(self, message: str = None, meta=None):
        self.messages.append(("end", message))

    async def error(self, message: str, meta=None):
        self.messages.append(("error", message))

    def text(self) -> str:
        return " | ".join(m for _, m in self.messages if m)


@pytest.fixture
def status():
    return MockStatus()


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Server bound to an isolated skill root (and an isolated registry)."""
    from agent_system.skills.registry import SkillRegistry

    root = tmp_path / "skills"
    _write_skill(
        root, "alpha", "ALPHA-BODY",
        description="Alpha skill.",
        extra={"reference/deep.md": "DEEP-CONTENT"},
    )
    monkeypatch.setattr(
        "plugins.skills.server.get_skill_registry",
        lambda *a, **k: SkillRegistry(),
    )
    cfg = AgentSystemConfig(skills={"skill_dirs": [str(root)]})
    srv = SkillsServer("skills", cfg, ToolServerConfig(type="skills", enabled=True))
    return srv, root


class TestList:
    async def test_lists_all_skills_compact(self, server, status):
        """Without a name only name + description: with every file of every
        skill the real answer was ~25,700 chars for 42 skills."""
        srv, _ = server
        res = await srv.list({"_status": status})
        assert res["status"] == "success"
        assert res["count"] == 1
        assert res["skills"] == [{"name": "alpha", "description": "Alpha skill."}]

    async def test_single_skill_detail(self, server, status):
        srv, _ = server
        res = await srv.list({"name": "alpha", "_status": status})
        assert res["status"] == "success"
        assert res["skill"]["version"] == "1.0.0"
        assert res["skill"]["files"] == ["SKILL.md", "reference/deep.md"]

    async def test_unknown_skill_reports_available(self, server, status):
        srv, _ = server
        res = await srv.list({"name": "ghost", "_status": status})
        assert res["status"] == "error"
        assert res["available"] == ["alpha"]


class TestRead:
    async def test_reads_skill_md_by_default(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "_status": status})
        assert res["status"] == "success"
        assert res["content"] == "ALPHA-BODY"
        assert res["truncated"] is False

    async def test_reads_bundled_reference_file(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": "reference/deep.md", "_status": status})
        assert res["status"] == "success"
        assert res["content"] == "DEEP-CONTENT"

    async def test_traversal_is_refused_and_lists_files(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": "../../etc/passwd", "_status": status})
        assert res["status"] == "error"
        assert "escapes" in res["error"]
        assert res["files"] == ["SKILL.md", "reference/deep.md"]

    async def test_absolute_path_is_refused(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": "/etc/passwd", "_status": status})
        assert res["status"] == "error"

    async def test_missing_file_reports_what_exists(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": "reference/nope.md", "_status": status})
        assert res["status"] == "error"
        assert "reference/deep.md" in res["files"]

    async def test_missing_name_is_an_error(self, server, status):
        srv, _ = server
        res = await srv.read({"_status": status})
        assert res["status"] == "error"
        assert res["available"] == ["alpha"]

    async def test_oversized_file_is_truncated_not_dumped(self, server, status):
        """A huge reference file must not blow up the context in one call."""
        srv, root = server
        big = "x" * (MAX_READ_CHARS + 500)
        (root / "alpha" / "reference" / "big.md").write_text(big, encoding="utf-8")
        res = await srv.read({"name": "alpha", "path": "reference/big.md", "_status": status})
        assert res["status"] == "success"
        assert res["truncated"] is True
        assert len(res["content"]) == MAX_READ_CHARS


class TestReadOffset:
    """A file longer than the cap is read in pieces via ``next_offset``."""

    @pytest.fixture
    def big(self, server):
        srv, root = server
        text = "".join(chr(ord("a") + i % 26) for i in range(MAX_READ_CHARS + 500))
        (root / "alpha" / "reference" / "big.md").write_text(text, encoding="utf-8")
        return srv, text

    async def test_cut_read_names_next_offset_and_the_rest_follows(self, big, status):
        srv, text = big
        first = await srv.read({"name": "alpha", "path": "reference/big.md", "_status": status})
        assert first["truncated"] is True
        assert first["next_offset"] == MAX_READ_CHARS
        second = await srv.read({"name": "alpha", "path": "reference/big.md",
                                 "offset": first["next_offset"], "_status": status})
        assert second["status"] == "success"
        assert second["truncated"] is False
        assert "next_offset" not in second
        assert first["content"] + second["content"] == text

    async def test_whole_read_has_no_next_offset(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": "reference/deep.md", "_status": status})
        assert "next_offset" not in res

    async def test_offset_applies_to_skill_md_too(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "offset": 6, "_status": status})
        assert res["content"] == "BODY"

    async def test_offset_as_digit_text_is_taken(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": "reference/deep.md",
                              "offset": "5", "_status": status})
        assert res["content"] == "CONTENT"

    async def test_offset_at_the_end_is_empty(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": "reference/deep.md",
                              "offset": len("DEEP-CONTENT"), "_status": status})
        assert res["status"] == "success"
        assert res["content"] == ""
        assert res["truncated"] is False

    async def test_offset_past_the_end_is_an_error(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": "reference/deep.md",
                              "offset": len("DEEP-CONTENT") + 1, "_status": status})
        assert res["status"] == "error"
        assert "past the end" in res["error"]

    @pytest.mark.parametrize("bad", [-1, "-1", "abc", 1.5, True, "²", "9" * 5000])
    async def test_bad_offset_is_an_error(self, server, status, bad):
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": "reference/deep.md",
                              "offset": bad, "_status": status})
        assert res["status"] == "error"
        assert "offset" in res["error"]
        assert any(kind == "error" for kind, _ in status.messages)


class TestStatusMessages:
    """Operators must see WHICH skill/file was touched, not just 'completed'."""

    async def test_list_names_the_skills_found(self, server, status):
        srv, _ = server
        await srv.list({"_status": status})
        assert "alpha" in status.text()

    async def test_list_of_one_skill_names_its_files(self, server, status):
        srv, _ = server
        await srv.list({"name": "alpha", "_status": status})
        assert "reference/deep.md" in status.text()

    async def test_read_names_skill_and_file(self, server, status):
        srv, _ = server
        await srv.read({"name": "alpha", "path": "reference/deep.md", "_status": status})
        assert "alpha/reference/deep.md" in status.text()

    async def test_read_reports_truncation(self, server, status):
        srv, root = server
        (root / "alpha" / "reference" / "big.md").write_text(
            "x" * (MAX_READ_CHARS + 10), encoding="utf-8")
        await srv.read({"name": "alpha", "path": "reference/big.md", "_status": status})
        assert "truncated" in status.text()

    async def test_read_names_next_offset(self, server, status):
        srv, root = server
        (root / "alpha" / "reference" / "big.md").write_text(
            "x" * (MAX_READ_CHARS + 10), encoding="utf-8")
        await srv.read({"name": "alpha", "path": "reference/big.md", "_status": status})
        assert f"next_offset {MAX_READ_CHARS}" in status.text()

    async def test_long_lines_fit_one_row(self, server, status):
        """42 real skills made a 604-char list line, docx's 61 files 3,174."""
        srv, root = server
        for i in range(40):
            _write_skill(root, f"skill-with-a-long-name-{i:02d}", "b")
        _write_skill(root, "many", "b", extra={f"scripts/file_{i:02d}.py": "" for i in range(40)})
        await srv.list({"_status": status})
        await srv.list({"name": "many", "_status": status})
        ends = [m for kind, m in status.messages if kind == "end"]
        assert len(ends) == 2
        # The row budget of tests/plugins/test_status_end_lines.py (MAX_LINE).
        assert all(len(m) <= 140 for m in ends), ends
        assert ends[0].startswith("42 skill(s)")
        assert ends[1].startswith("many v1.0.0: 41 file(s)")

    async def test_a_long_path_is_cut_not_dropped(self, server, status):
        """textwrap.shorten turned a line whose first word is too long into a bare '…'."""
        srv, root = server
        rel = "reference/" + "p" * 150 + ".md"
        (root / "alpha" / rel).write_text("x", encoding="utf-8")
        await srv.read({"name": "alpha", "path": rel, "_status": status})
        ends = [m for kind, m in status.messages if kind == "end"]
        assert len(ends) == 1 and len(ends[0]) <= 140
        assert ends[0].startswith("alpha/reference/ppp"), ends

    async def test_errors_are_surfaced_as_status_errors(self, server, status):
        srv, _ = server
        await srv.read({"name": "ghost", "_status": status})
        assert any(kind == "error" for kind, _ in status.messages)
        assert "ghost" in status.text()


class TestNotFoundGuidance:
    """From a real run: the agent asked for 'reference/seitenformat.md' where
    the bundle holds 'references/seitenformat.md', and burned a turn finding
    out. Both spellings exist across our own skills, which is what invites it."""

    async def test_singular_plural_slip_is_named(self, server, status):
        srv, _ = server                       # 'alpha' holds reference/deep.md
        res = await srv.read({"name": "alpha", "path": "references/deep.md",
                              "_status": status})
        assert res["status"] == "error"
        assert res["did_you_mean"] == "reference/deep.md"
        assert "did you mean" in res["error"]
        assert "did you mean" in status.text()   # visible in the panel too

    async def test_unknown_skill_name_is_guided(self, server, status):
        srv, root = server
        _write_skill(root, "zustandsgraph", "body")
        res = await srv.read({"name": "zustandgraph", "_status": status})
        assert res["did_you_mean"] == "zustandsgraph"

    async def test_very_short_names_get_no_guess(self, server, status):
        """One character off a four-letter name is not a typo the machine can
        tell from a different name — 'alfa' is as close to 'alpha' as to
        'alba'. Guessing there buys a wrong answer, not a saved turn."""
        srv, _ = server
        res = await srv.read({"name": "alfa", "_status": status})
        assert res["did_you_mean"] is None
        assert res["available"] == ["alpha"]

    async def test_unrelated_path_gets_no_guess(self, server, status):
        """Better a plain not-found than a confident wrong answer."""
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": "voellig/anderes.md",
                              "_status": status})
        assert res["did_you_mean"] is None
        assert res["files"] == ["SKILL.md", "reference/deep.md"]


class TestArgumentsThatAreNotText:
    """The framework does not check arguments against the schema: a model
    that sends a number must get an answer, not an AttributeError."""

    async def test_list_with_a_number_as_name_answers(self, server, status):
        srv, _ = server
        res = await srv.list({"name": 5, "_status": status})
        assert res["status"] == "error"
        assert res["available"] == ["alpha"]

    async def test_read_with_a_number_as_name_answers(self, server, status):
        srv, _ = server
        res = await srv.read({"name": 5, "_status": status})
        assert res["status"] == "error"
        assert res["available"] == ["alpha"]

    async def test_read_with_a_number_as_path_answers(self, server, status):
        srv, _ = server
        res = await srv.read({"name": "alpha", "path": 5, "_status": status})
        assert res["status"] == "error"
        assert res["files"] == ["SKILL.md", "reference/deep.md"]
