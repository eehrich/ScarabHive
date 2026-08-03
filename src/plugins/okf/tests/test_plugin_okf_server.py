"""Tests for the OKF MCP server — sandboxed tools + consumer hook.

Exercises each tool against a real bundle in a tmp sandbox, the sandbox
boundary, the write conformance gate, and the pre_llm_call context injection.
"""
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock

from agent_system.config.models import MCPConfig
from plugins.okf.server import OkfServer


@pytest.fixture
def mock_system_config():
    c = MagicMock()
    c.ssl_verify = True
    return c


@pytest.fixture
def bundle(tmp_path):
    """A small conformant bundle: /tables/orders.md links to customers.md."""
    root = tmp_path / "sales"
    (root / "tables").mkdir(parents=True)
    (root / "tables" / "orders.md").write_text(
        "---\ntype: BigQuery Table\ntitle: Orders\n"
        "description: One row per completed order.\ntags: [sales, revenue]\n---\n\n"
        "# Schema\nJoined with [customers](/tables/customers.md) on customer_id.\n",
        encoding="utf-8")
    (root / "tables" / "customers.md").write_text(
        "---\ntype: BigQuery Table\ntitle: Customers\n"
        "description: One row per customer.\n---\n\n# Schema\nid, name.\n",
        encoding="utf-8")
    return root


@pytest.fixture
def server(mock_system_config, tmp_path):
    cfg = MCPConfig(type="okf", enabled=True,
                    config={"allowed_directories": [str(tmp_path)]})
    return OkfServer("okf", mock_system_config, cfg)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

class TestValidate:
    @pytest.mark.asyncio
    async def test_conformant_bundle(self, server, bundle):
        res = await server.validate({"bundle": str(bundle)})
        assert res["status"] == "ok"
        assert res["conformant"] is True
        assert res["concepts"] == 2
        assert res["errors"] == 0

    @pytest.mark.asyncio
    async def test_missing_type_is_error(self, server, bundle):
        (bundle / "bad.md").write_text("---\ntitle: No Type\n---\n\nbody\n",
                                       encoding="utf-8")
        res = await server.validate({"bundle": str(bundle)})
        assert res["conformant"] is False
        assert res["errors"] == 1

    @pytest.mark.asyncio
    async def test_broken_link_is_warning_only(self, server, bundle):
        (bundle / "tables" / "orders.md").write_text(
            "---\ntype: Table\n---\n\nSee [gone](/tables/nope.md).\n", encoding="utf-8")
        res = await server.validate({"bundle": str(bundle)})
        assert res["conformant"] is True  # warnings don't break conformance
        assert res["warnings"] >= 1

    @pytest.mark.asyncio
    async def test_outside_sandbox_rejected(self, server):
        res = await server.validate({"bundle": "/etc"})
        assert res["status"] == "error"
        assert "outside" in res["error"]


# ---------------------------------------------------------------------------
# read / write
# ---------------------------------------------------------------------------

class TestReadWrite:
    @pytest.mark.asyncio
    async def test_read_concept(self, server, bundle):
        res = await server.read_concept(
            {"bundle": str(bundle), "path": "/tables/orders.md"})
        assert res["status"] == "ok"
        assert res["frontmatter"]["type"] == "BigQuery Table"
        assert "# Schema" in res["body"]

    @pytest.mark.asyncio
    async def test_write_concept_requires_type(self, server, bundle):
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/metrics/wau.md",
            "frontmatter": {"title": "WAU"}, "body": "# Def\nweekly active users",
        })
        assert res["status"] == "error"
        assert "type" in res["error"].lower()

    @pytest.mark.asyncio
    async def test_write_concept_ok(self, server, bundle):
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/metrics/wau.md",
            "frontmatter": {"type": "Metric", "title": "WAU"},
            "body": "# Definition\nweekly active users",
        })
        assert res["status"] == "ok"
        back = await server.read_concept(
            {"bundle": str(bundle), "path": "/metrics/wau.md"})
        assert back["frontmatter"]["type"] == "Metric"

    @pytest.mark.asyncio
    async def test_overwrite_preserves_extra_keys(self, server, bundle):
        # write with a custom key, then overwrite touching only title
        await server.write_concept({
            "bundle": str(bundle), "path": "/x.md",
            "frontmatter": {"type": "T", "owner": "team-sales"}, "body": "a"})
        await server.write_concept({
            "bundle": str(bundle), "path": "/x.md",
            "frontmatter": {"type": "T", "title": "X"}, "body": "b"})
        back = await server.read_concept({"bundle": str(bundle), "path": "/x.md"})
        assert back["frontmatter"]["owner"] == "team-sales"  # preserved
        assert back["frontmatter"]["title"] == "X"

    @pytest.mark.asyncio
    async def test_write_path_traversal_rejected(self, server, bundle):
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/../escape.md",
            "frontmatter": {"type": "T"}, "body": "x"})
        assert res["status"] == "error"
        assert "escape" in res["error"]

    @pytest.mark.asyncio
    async def test_write_reserved_filename_rejected(self, server, bundle):
        for p in ("/index.md", "/tables/log.md"):
            res = await server.write_concept({
                "bundle": str(bundle), "path": p,
                "frontmatter": {"type": "T"}, "body": "x"})
            assert res["status"] == "error"
            assert "reserved" in res["error"]

    @pytest.mark.asyncio
    async def test_write_body_null_no_crash(self, server, bundle):
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/n.md",
            "frontmatter": {"type": "T"}, "body": None})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_overwrite_deep_merges_nested(self, server, bundle):
        await server.write_concept({
            "bundle": str(bundle), "path": "/d.md",
            "frontmatter": {"type": "T", "meta": {"a": 1, "b": 2}}, "body": "x"})
        await server.write_concept({
            "bundle": str(bundle), "path": "/d.md",
            "frontmatter": {"type": "T", "meta": {"b": 9}}, "body": "x"})
        back = await server.read_concept({"bundle": str(bundle), "path": "/d.md"})
        # sibling 'a' survives a nested update to 'b'
        assert back["frontmatter"]["meta"] == {"a": 1, "b": 9}

    @pytest.mark.asyncio
    async def test_overwrite_preserves_frontmatter_comment(self, server, bundle):
        # write a concept with a comment via raw file, then overwrite via tool
        (bundle / "c.md").write_text(
            "---\ntype: T  # canonical type\nowner: sales\n---\n\nbody\n",
            encoding="utf-8")
        await server.write_concept({
            "bundle": str(bundle), "path": "/c.md",
            "frontmatter": {"type": "T", "title": "New"}, "body": "body"})
        raw = (bundle / "c.md").read_text(encoding="utf-8")
        assert "# canonical type" in raw   # comment survived overwrite
        assert "owner: sales" in raw       # untouched key survived

    @pytest.mark.asyncio
    async def test_validate_reports_yaml_error_not_type(self, server, bundle):
        (bundle / "broken.md").write_text(
            "---\ntype: [unclosed\n---\nbody\n", encoding="utf-8")
        res = await server.validate({"bundle": str(bundle)})
        assert res["conformant"] is False
        rules = {f["rule"] for f in res["findings"]}
        assert "frontmatter-parseable" in rules  # true failure, not 'type-required'

    @pytest.mark.asyncio
    async def test_crlf_body_round_trips(self, server, bundle):
        (bundle / "w.md").write_bytes(
            b"---\r\ntype: T\r\n---\r\n\r\n# Body\r\nline\r\n")
        r1 = await server.read_concept({"bundle": str(bundle), "path": "/w.md"})
        assert r1["body"].startswith("# Body")  # no spurious leading blank
        # rewrite then read again — stable
        await server.write_concept({
            "bundle": str(bundle), "path": "/w.md",
            "frontmatter": r1["frontmatter"], "body": r1["body"]})
        r2 = await server.read_concept({"bundle": str(bundle), "path": "/w.md"})
        assert r2["body"] == r1["body"]


# ---------------------------------------------------------------------------
# list / neighbors / subgraph / search
# ---------------------------------------------------------------------------

class TestGraphTools:
    @pytest.mark.asyncio
    async def test_list(self, server, bundle):
        res = await server.list({"bundle": str(bundle)})
        assert res["count"] == 2
        paths = {c["path"] for c in res["concepts"]}
        assert paths == {"/tables/orders.md", "/tables/customers.md"}

    @pytest.mark.asyncio
    async def test_list_subdir(self, server, bundle):
        res = await server.list({"bundle": str(bundle), "dir": "/tables"})
        assert res["count"] == 2

    @pytest.mark.asyncio
    async def test_neighbors(self, server, bundle):
        res = await server.neighbors(
            {"bundle": str(bundle), "path": "/tables/orders.md"})
        assert res["neighbors"] == ["/tables/customers.md"]
        assert res["broken_links"] == []

    @pytest.mark.asyncio
    async def test_subgraph(self, server, bundle):
        res = await server.subgraph(
            {"bundle": str(bundle), "seed": "/tables/orders.md", "depth": 1})
        assert set(res["concepts"]) == {"/tables/orders.md", "/tables/customers.md"}

    @pytest.mark.asyncio
    async def test_search_ranks_relevant(self, server, bundle):
        res = await server.search(
            {"bundle": str(bundle), "query": "customer", "limit": 5})
        assert res["status"] == "ok"
        assert res["results"]
        # customers.md should outrank on the term "customer"
        assert res["results"][0]["path"] == "/tables/customers.md"


# ---------------------------------------------------------------------------
# append_log / reindex
# ---------------------------------------------------------------------------

class TestLogAndIndex:
    @pytest.mark.asyncio
    async def test_append_log_requires_iso_date(self, server, bundle):
        res = await server.append_log(
            {"bundle": str(bundle), "date": "yesterday", "description": "x"})
        assert res["status"] == "error"

    @pytest.mark.asyncio
    async def test_append_log_writes(self, server, bundle):
        res = await server.append_log({
            "bundle": str(bundle), "date": "2026-07-22",
            "action": "Creation", "description": "Added orders."})
        assert res["status"] == "ok"
        log = (bundle / "log.md").read_text(encoding="utf-8")
        assert "## 2026-07-22" in log
        assert "**Creation**: Added orders." in log

    @pytest.mark.asyncio
    async def test_reindex(self, server, bundle):
        res = await server.reindex({"bundle": str(bundle), "dir": "/tables"})
        assert res["status"] == "ok"
        idx = (bundle / "tables" / "index.md").read_text(encoding="utf-8")
        assert "[orders](/tables/orders.md)" in idx
        assert "One row per completed order." in idx


# ---------------------------------------------------------------------------
# read-only mode
# ---------------------------------------------------------------------------

class TestSymlinkSafety:
    @pytest.mark.asyncio
    async def test_symlink_escaping_bundle_is_skipped(self, server, bundle, tmp_path):
        # A secret outside the bundle, and a symlink inside pointing to it.
        secret = tmp_path / "secret.md"
        secret.write_text("---\ntype: Secret\n---\n\nSSH KEY MATERIAL\n", encoding="utf-8")
        link = bundle / "tables" / "leak.md"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this platform")
        res = await server.list({"bundle": str(bundle)})
        paths = {c["path"] for c in res["concepts"]}
        assert "/tables/leak.md" not in paths  # escape skipped
        s = await server.search({"bundle": str(bundle), "query": "SSH KEY MATERIAL"})
        assert all("leak" not in r["path"] for r in s["results"])  # not exfiltrated


class TestReadOnly:
    @pytest.mark.asyncio
    async def test_write_blocked(self, mock_system_config, tmp_path, bundle):
        cfg = MCPConfig(type="okf", enabled=True, config={
            "allowed_directories": [str(tmp_path)], "read_only": True})
        ro = OkfServer("okf", mock_system_config, cfg)
        res = await ro.write_concept({
            "bundle": str(bundle), "path": "/x.md",
            "frontmatter": {"type": "T"}, "body": "x"})
        assert res["status"] == "error"
        assert "read-only" in res["error"]


# ---------------------------------------------------------------------------
# consumer hook
# ---------------------------------------------------------------------------

class TestContextHook:
    def _server_with_hook(self, mock_system_config, tmp_path, bundle):
        cfg = MCPConfig(type="okf", enabled=True, config={
            "allowed_directories": [str(tmp_path)],
            "hook_bundle": str(bundle),
            "hook_max_concepts": 4,
            "hook_graph_depth": 1,
        })
        return OkfServer("okf", mock_system_config, cfg)

    @staticmethod
    def _injected(ctx):
        for m in ctx.messages:
            if getattr(m, "injected_by", None) == "okf":
                return m.content
        return None

    @pytest.mark.asyncio
    async def test_injects_relevant_context(self, mock_system_config, tmp_path, bundle):
        srv = self._server_with_hook(mock_system_config, tmp_path, bundle)
        ctx = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content="tell me about customers")],
            session_id="s1", hook_config={})
        result = await srv.on_pre_llm_call(ctx)
        assert result.success
        assert result.modified is True
        injected = self._injected(ctx)
        assert injected is not None and "OKF knowledge context" in injected
        assert "Customers" in injected  # relevant concept folded in

    @pytest.mark.asyncio
    async def test_hook_config_per_agent_overrides(self, mock_system_config, tmp_path, bundle):
        # Server has NO hook_bundle default; the per-agent hooks.overrides supplies it.
        cfg = MCPConfig(type="okf", enabled=True,
                        config={"allowed_directories": [str(tmp_path)]})
        srv = OkfServer("okf", mock_system_config, cfg)
        ctx = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content="customers")],
            session_id="s1",
            hook_config={"hook_bundle": str(bundle), "hook_max_concepts": 3})
        result = await srv.on_pre_llm_call(ctx)
        assert result.modified is True
        assert self._injected(ctx) is not None

    @pytest.mark.asyncio
    async def test_graph_expansion_pulls_linked_concept(self, mock_system_config, tmp_path, bundle):
        # A query that hits ONLY orders must still pull in customers via the link.
        srv = self._server_with_hook(mock_system_config, tmp_path, bundle)
        ctx = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content="orders revenue")],
            session_id="s1",
            hook_config={"hook_seed_count": 1, "hook_graph_depth": 1})
        await srv.on_pre_llm_call(ctx)
        injected = self._injected(ctx)
        assert injected is not None
        assert "Orders" in injected and "Customers" in injected  # linked concept expanded in

    @pytest.mark.asyncio
    async def test_reinjection_dedups(self, mock_system_config, tmp_path, bundle):
        srv = self._server_with_hook(mock_system_config, tmp_path, bundle)
        ctx = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content="customers")],
            session_id="s1", hook_config={})
        await srv.on_pre_llm_call(ctx)
        await srv.on_pre_llm_call(ctx)  # second step
        okf_blocks = [m for m in ctx.messages if getattr(m, "injected_by", None) == "okf"]
        assert len(okf_blocks) == 1  # no stacking

    @pytest.mark.asyncio
    async def test_disabled_without_bundle(self, mock_system_config, tmp_path, bundle):
        cfg = MCPConfig(type="okf", enabled=True,
                        config={"allowed_directories": [str(tmp_path)]})
        srv = OkfServer("okf", mock_system_config, cfg)
        ctx = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content="hi")], session_id="s1")
        result = await srv.on_pre_llm_call(ctx)
        assert result.modified is False

    @pytest.mark.asyncio
    async def test_no_relevant_match_no_injection(self, mock_system_config, tmp_path, bundle):
        srv = self._server_with_hook(mock_system_config, tmp_path, bundle)
        ctx = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content="zzz quantum chromodynamics")],
            session_id="s1")
        result = await srv.on_pre_llm_call(ctx)
        # no lexical overlap -> nothing injected
        assert result.modified is False


# ---------------------------------------------------------------------------
# Concurrency — an agent that spawns itself writes the SAME bundle
# ---------------------------------------------------------------------------

class TestConcurrentWrites:
    """Parallel tool calls of one turn, spawned sub-agents and other services
    all write one bundle. Every write path here is read-modify-write, so an
    interleaving loses data silently: the tool answers ok to both callers.

    Each test WIDENS the critical section artificially. Without that the race
    is invisible on a fast machine — and it would have stayed invisible, since
    the pre-lock code was safe only by accident (no await between read and
    write). The delay makes the guarantee testable instead of incidental.
    """

    @staticmethod
    def _slow(monkeypatch, target: str, seconds: float = 0.02):
        """Make one core step slow, so any unguarded interleaving happens."""
        import time
        from plugins.okf import core
        real = getattr(core, target)

        def slowed(*a, **kw):
            time.sleep(seconds)
            return real(*a, **kw)

        monkeypatch.setattr(core, target, slowed)

    @pytest.mark.asyncio
    async def test_concurrent_log_appends_keep_every_entry(
            self, server, tmp_path, monkeypatch):
        import asyncio
        root = tmp_path / "conc"
        root.mkdir()
        self._slow(monkeypatch, "append_log_entry")

        n = 8
        results = await asyncio.gather(*[
            server.append_log({"bundle": str(root), "date": "2026-08-03",
                               "action": f"A{i}", "description": f"entry {i}"})
            for i in range(n)])

        assert all(r["status"] == "ok" for r in results)
        log = (root / "log.md").read_text(encoding="utf-8")
        missing = [i for i in range(n) if f"**A{i}**" not in log]
        assert not missing, f"log entries lost: {missing}"

    @pytest.mark.asyncio
    async def test_concurrent_writes_keep_every_frontmatter_key(
            self, server, tmp_path, monkeypatch):
        """Overwrite merges into the EXISTING frontmatter, so a lost update
        drops another writer's keys — the sub-agent's contribution vanishes."""
        import asyncio
        root = tmp_path / "conc2"
        root.mkdir()
        self._slow(monkeypatch, "merge_frontmatter")

        n = 8
        results = await asyncio.gather(*[
            server.write_concept({"bundle": str(root), "path": "/shared.md",
                                  "frontmatter": {"type": "note", f"key{i}": i},
                                  "body": f"body {i}"})
            for i in range(n)])

        assert all(r["status"] == "ok" for r in results)
        text = (root / "shared.md").read_text(encoding="utf-8")
        missing = [i for i in range(n) if f"key{i}:" not in text]
        assert not missing, f"frontmatter keys lost: {missing}"

    def test_scratch_name_is_unique_per_writer(self, tmp_path, monkeypatch):
        """A FIXED temp name is worse than none: two writers interleave their
        bytes into one scratch file, then both rename it over the target — a
        corrupted concept rather than a merely lost one.

        Tested on _atomic_write directly: the bundle lock now makes this
        unreachable WITHIN a process, so the property only shows across
        processes, where no in-process test can observe it.
        """
        import threading
        from plugins.okf import server as srv

        target = tmp_path / "c.md"
        names = []
        real_write = type(target).write_text

        def capture(self, *a, **kw):
            names.append(self.name)
            return real_write(self, *a, **kw)

        monkeypatch.setattr(type(target), "write_text", capture)
        srv._atomic_write(target, "one")
        t = threading.Thread(target=srv._atomic_write, args=(target, "two"))
        t.start(); t.join()

        scratch = [n for n in names if n.endswith(".tmp")]
        assert len(scratch) == 2
        assert scratch[0] != scratch[1], f"writers shared a scratch file: {scratch}"

    @pytest.mark.asyncio
    async def test_no_scratch_files_are_left_behind(self, server, tmp_path):
        import asyncio
        root = tmp_path / "conc3"
        root.mkdir()
        await asyncio.gather(*[
            server.write_concept({"bundle": str(root), "path": f"/c{i}.md",
                                  "frontmatter": {"type": "note"},
                                  "body": "x" * 2000})
            for i in range(6)])
        assert not list(root.glob("*.tmp")), "scratch files left in the bundle"
        for i in range(6):
            assert (root / f"c{i}.md").read_text(encoding="utf-8").count("---") == 2

    @pytest.mark.asyncio
    async def test_lock_file_is_not_mistaken_for_a_concept(self, server, tmp_path):
        """The guard file lives IN the bundle (services run under different
        accounts, so a temp-dir lock would not be the same lock). Whether it
        survives a release is platform-dependent — Windows removes it, POSIX
        leaves it — so what must hold is that a PRESENT one is invisible to
        every read path."""
        root = tmp_path / "conc4"
        root.mkdir()
        await server.write_concept({"bundle": str(root), "path": "/a.md",
                                    "frontmatter": {"type": "note"}, "body": "x"})
        (root / ".okf.lock").write_text("", encoding="utf-8")  # as POSIX leaves it

        listed = await server.list({"bundle": str(root)})
        assert [c["path"] for c in listed["concepts"]] == ["/a.md"]
        report = await server.validate({"bundle": str(root)})
        assert report["conformant"] is True

    def test_lock_survives_a_new_event_loop(self, server, tmp_path):
        """The guard is reached again on a LATER event loop: the bundle path is
        stable in production, and a process may run more than one loop over its
        lifetime. An asyncio.Lock binds to the loop of its first use and then
        raises 'bound to a different event loop' — a threading.Lock does not.

        The per-test tmp bundle hides this: every test gets a fresh key. Only a
        second loop on the SAME bundle shows it.
        """
        import asyncio
        root = tmp_path / "loops"
        root.mkdir()

        async def round_of(tag):
            await asyncio.gather(*[
                server.append_log({"bundle": str(root), "date": "2026-08-03",
                                   "action": f"{tag}{i}", "description": "x"})
                for i in range(3)])

        asyncio.run(round_of("A"))
        asyncio.run(round_of("B"))      # new loop, same lock object

        log = (root / "log.md").read_text(encoding="utf-8")
        missing = [f"{t}{i}" for t in ("A", "B") for i in range(3)
                   if f"**{t}{i}**" not in log]
        assert not missing, f"entries lost across event loops: {missing}"

    @pytest.mark.asyncio
    async def test_reindex_sees_concepts_written_just_before_it(
            self, server, tmp_path, monkeypatch):
        """The bundle scan runs INSIDE the lock: an index built from a listing
        taken before a concurrent write would be published as current while
        already missing that concept."""
        import asyncio
        root = tmp_path / "conc5"
        root.mkdir()
        await asyncio.gather(
            *[server.write_concept({"bundle": str(root), "path": f"/n{i}.md",
                                    "frontmatter": {"type": "note",
                                                    "description": f"d{i}"},
                                    "body": "x"}) for i in range(5)],
        )
        result = await server.reindex({"bundle": str(root)})
        assert result["entries"] == 5
        index = (root / "index.md").read_text(encoding="utf-8")
        for i in range(5):
            assert f"/n{i}.md" in index


class TestCrossProcessWrites:
    """agent-api, the writer worker and a developer's CLI share data/okf. The
    asyncio lock does not reach across processes — only the file lock does, and
    only a real second process can show it."""

    WORKER = (
        "import sys, asyncio\n"
        "sys.path.insert(0, 'src')\n"
        "from unittest.mock import MagicMock\n"
        "from agent_system.config.models import MCPConfig\n"
        "from plugins.okf.server import OkfServer\n"
        "root, tag = sys.argv[1], sys.argv[2]\n"
        "cfg = MCPConfig(type='okf', enabled=True,\n"
        "                config={'allowed_directories': [root]})\n"
        "s = OkfServer('okf', MagicMock(), cfg)\n"
        "async def main():\n"
        "    await asyncio.gather(*[\n"
        "        s.append_log({'bundle': root, 'date': '2026-08-03',\n"
        "                      'action': f'{tag}{i}', 'description': 'x'})\n"
        "        for i in range(4)])\n"
        "asyncio.run(main())\n"
    )

    @pytest.mark.timeout(120)
    def test_two_processes_lose_no_log_entries(self, tmp_path):
        import subprocess
        import sys
        root = tmp_path / "shared"
        root.mkdir()
        script = tmp_path / "worker.py"
        script.write_text(self.WORKER, encoding="utf-8")

        procs = [subprocess.Popen([sys.executable, str(script), str(root), tag],
                                  cwd=".", stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for tag in ("P", "Q")]
        for p in procs:
            _out, err = p.communicate(timeout=110)
            assert p.returncode == 0, f"worker failed: {err[-800:]}"

        log = (root / "log.md").read_text(encoding="utf-8")
        missing = [f"{t}{i}" for t in ("P", "Q") for i in range(4)
                   if f"**{t}{i}**" not in log]
        assert not missing, f"entries lost across processes: {missing}"
