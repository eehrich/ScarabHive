"""Tests for the skills plugin (tool surface over the core skill registry)."""
import pytest

from agent_system.config.models import AgentSystemConfig, MCPConfig
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
    srv = SkillsServer("skills", cfg, MCPConfig(type="skills", enabled=True))
    return srv, root


class TestList:
    async def test_lists_all_skills_with_files(self, server, status):
        srv, _ = server
        res = await srv.list({"_status": status})
        assert res["status"] == "success"
        assert res["count"] == 1
        entry = res["skills"][0]
        assert entry["name"] == "alpha"
        assert entry["description"] == "Alpha skill."
        assert entry["files"] == ["SKILL.md", "reference/deep.md"]

    async def test_single_skill_detail(self, server, status):
        srv, _ = server
        res = await srv.list({"name": "alpha", "_status": status})
        assert res["status"] == "success"
        assert res["skill"]["version"] == "1.0.0"

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

    async def test_errors_are_surfaced_as_status_errors(self, server, status):
        srv, _ = server
        await srv.read({"name": "ghost", "_status": status})
        assert any(kind == "error" for kind, _ in status.messages)
        assert "ghost" in status.text()
