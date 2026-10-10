"""Tests for the OKF tool server — sandboxed tools + consumer hook.

Exercises each tool against a real bundle in a tmp sandbox, the sandbox
boundary, the write conformance gate, and the pre_llm_call context injection.
"""
from types import SimpleNamespace

import pytest

from agent_system.llm.message_roles import DEVELOPER
from unittest.mock import MagicMock

from agent_system.config.models import ToolServerConfig
from plugins.okf.server import OkfServer


@pytest.fixture
def mock_system_config():
    c = MagicMock()
    c.ssl_verify = True
    # Real strings, not Mocks: the server reads context.timezone to stamp log
    # times, and a Mock there would silently fall back to UTC — which differs
    # from the local date for two hours of every day.
    c.context.timezone = "Europe/Berlin"
    c.context.location = "Germany"
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
    cfg = ToolServerConfig(type="okf", enabled=True,
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
    async def test_foreign_status_value_is_warned_about(self, server, bundle):
        """``status`` belongs to the format (§5.4), not to the producer.

        Measured on 2026-08-20: a bundle had stored its publish verdict there
        (``status: publishable``) -- every reader got a lifecycle state the
        vocabulary does not know, and nobody noticed. A warning, not an
        error: extra keys are allowed, this one is merely taken."""
        (bundle / "tables" / "occupied.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Occupied\n"
            "description: d\nstatus: publishable\n---\n\n# S\n",
            encoding="utf-8")

        res = await server.validate({"bundle": str(bundle)})
        assert res["conformant"] is True, "extra keys are allowed -- not an error"
        assert res["errors"] == 0
        hits = [f for f in res["findings"]
                if f["rule"] == "status-vocabulary"]
        assert len(hits) == 1, res["findings"]
        assert "publishable" in hits[0]["message"]

    @pytest.mark.asyncio
    async def test_non_string_status_is_warned_about_too(self, server, bundle):
        """YAML types the value, not the producer: ``status: true`` becomes
        a bool, ``status: 2026-01-01`` a date. If the rule checked the
        normalised value it would see "stable" everywhere and stay silent
        in exactly the cases it exists for."""
        for i, raw in enumerate(("true", "1", "2026-01-01", "[draft]")):
            (bundle / "tables" / f"odd_{i}.md").write_text(
                f"---\ntype: BigQuery Table\ntitle: Odd{i}\n"
                f"description: d\nstatus: {raw}\n---\n\n# S\n",
                encoding="utf-8")

        res = await server.validate({"bundle": str(bundle)})
        warned = {f["path"] for f in res["findings"]
                  if f["rule"] == "status-vocabulary"}
        assert len(warned) == 4, res["findings"]

    @pytest.mark.asyncio
    async def test_blank_status_counts_as_absent(self, server, bundle):
        """A blanked field says nothing unknown -- it says nothing.
        If it warned, tidying up (clearing `status`) would itself be a
        violation."""
        (bundle / "tables" / "blank.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Blank\n"
            "description: d\nstatus: '   '\n---\n\n# S\n",
            encoding="utf-8")

        res = await server.validate({"bundle": str(bundle)})
        assert [f for f in res["findings"]
                if f["rule"] == "status-vocabulary"] == []
        lst = await server.list({"bundle": str(bundle)})
        entry = next(c for c in lst["concepts"] if c["path"] == "/tables/blank.md")
        assert entry["lifecycle"] == "stable"

    @pytest.mark.asyncio
    async def test_lifecycle_vocabulary_passes_without_warning(self, server, bundle):
        """The three allowed values must NOT warn -- otherwise the warning
        would be noise and the curator would stop looking."""
        for i, value in enumerate(("draft", "stable", "deprecated")):
            (bundle / "tables" / f"ok_{i}.md").write_text(
                f"---\ntype: BigQuery Table\ntitle: T{i}\n"
                f"description: d\nstatus: {value}\n---\n\n# S\n",
                encoding="utf-8")

        res = await server.validate({"bundle": str(bundle)})
        # Without this assertion the test would also pass if the fixture
        # never arrived -- an empty findings list is then no evidence, just
        # an absence.
        assert res["concepts"] == 5, res
        assert [f for f in res["findings"] if f["rule"] == "status-vocabulary"] == []

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

        # Unreadable frontmatter reports ``lifecycle: stable`` -- a default,
        # not a verdict. That is defensible only BECAUSE the same file
        # announces itself as broken next to it (type=None + this error).
        # If either goes away, the bundle claims currency for a file nobody
        # could read.
        entry = next(c for c in (await server.list({"bundle": str(bundle)}))["concepts"]
                     if c["path"] == "/broken.md")
        assert (entry["lifecycle"], entry["type"]) == ("stable", None)

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
    async def test_list_reports_the_lifecycle_status(self, server, bundle):
        """Spec §5.4: OKF does not delete, it sets ``deprecated`` ("kept for
        links and history"). If the overview does not show it, a reader would
        have to open every concept to spot retired knowledge -- and cites it
        as valid until then."""
        (bundle / "tables" / "legacy.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Legacy\n"
            "description: Superseded by orders.\nstatus: deprecated\n---\n\n"
            "# Schema\nOld.\n",
            encoding="utf-8")

        res = await server.list({"bundle": str(bundle)})
        by_path = {c["path"]: c for c in res["concepts"]}
        assert by_path["/tables/legacy.md"]["lifecycle"] == "deprecated"
        # A missing field means ``stable`` per spec -- NOT empty/None, or
        # every consumer would have to know the default itself.
        assert by_path["/tables/orders.md"]["lifecycle"] == "stable"

    @pytest.mark.asyncio
    async def test_lifecycle_is_always_one_of_the_three(self, server, bundle):
        """The tool description promises the model three values -- so no
        fourth may come out. Real bundles exist whose producer stored its own
        state in ``status``; the model cannot use that and guesses. The raw
        value is not lost: ``validate`` warns about it."""
        (bundle / "tables" / "foreign.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Foreign\n"
            "description: d\nstatus: blockiert\n---\n\n# S\n",
            encoding="utf-8")
        # Letter case must not slip through: the injection hook compares
        # against the exact word.
        (bundle / "tables" / "shouty.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Shouty\n"
            "description: d\nstatus: Deprecated\n---\n\n# S\n",
            encoding="utf-8")

        res = await server.list({"bundle": str(bundle)})
        by_path = {c["path"]: c for c in res["concepts"]}
        assert by_path["/tables/foreign.md"]["lifecycle"] == "stable"
        assert by_path["/tables/shouty.md"]["lifecycle"] == "deprecated"
        assert {c["lifecycle"] for c in res["concepts"]} <= {
            "draft", "stable", "deprecated"}

    @pytest.mark.asyncio
    async def test_deprecation_round_trips_through_the_real_tools(
        self, server, bundle,
    ):
        """The seam the whole chain hangs on: a curator SETS ``status`` via
        ``write_concept``, a reader sees it in ``list``, and a later
        take-back undoes it. Without this path "deprecate instead of delete"
        would be a statement of intent -- the fields are not in
        ``RESERVED_FIELDS``, they only survive because the format passes
        extra keys through."""
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/tables/orders.md",
            "frontmatter": {"type": "BigQuery Table", "status": "deprecated"},
            "body": "Superseded.",
        })
        assert res["status"] == "ok", res

        lst = await server.list({"bundle": str(bundle)})
        entry = next(c for c in lst["concepts"] if c["path"] == "/tables/orders.md")
        assert entry["lifecycle"] == "deprecated"
        assert entry["title"] == "Orders", "merge lost existing keys"

        # Take back: ``status`` removed -> default ``stable`` again.
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/tables/orders.md",
            "frontmatter": {"type": "BigQuery Table", "status": None},
            "body": "Back in service.",
        })
        assert res["status"] == "ok", res
        lst = await server.list({"bundle": str(bundle)})
        entry = next(c for c in lst["concepts"] if c["path"] == "/tables/orders.md")
        assert entry["lifecycle"] == "stable"

        # Fresh creation instead of overwrite: there is no existing
        # frontmatter to merge -- that branch must take ``status`` just the
        # same, otherwise a concept can only be retired if it already
        # existed.
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/tables/fresh.md",
            "frontmatter": {"type": "BigQuery Table", "status": "deprecated"},
            "body": "New but already retired.",
        })
        assert res["status"] == "ok", res
        lst = await server.list({"bundle": str(bundle)})
        fresh = next(c for c in lst["concepts"] if c["path"] == "/tables/fresh.md")
        assert fresh["lifecycle"] == "deprecated"

    @pytest.mark.asyncio
    async def test_index_marks_retired_concepts_too(self, server, bundle):
        """index.md is the entry point a reader opens first. If the marker
        were reserved for `list`/`search`, it would be missing exactly where
        the overview is formed."""
        (bundle / "tables" / "old.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Old\n"
            "description: Superseded table.\nstatus: deprecated\n---\n\n# S\n",
            encoding="utf-8")

        await server.reindex({"bundle": str(bundle)})
        idx = (bundle / "tables" / "index.md").read_text(encoding="utf-8")
        assert "[old](/tables/old.md) - [deprecated] Superseded table." in idx, idx
        # Current concepts stay unmarked -- otherwise the marker would be
        # worthless, since it would distinguish nothing.
        assert idx.count("[deprecated]") == 1, idx

    @pytest.mark.asyncio
    async def test_search_marks_retired_concepts_too(self, server, bundle):
        """For most readers search is the ENTRY into the bundle -- agents are
        explicitly sent here for details. Without a marker they would cite
        retired knowledge as valid; ``deprecated`` would be invisible exactly
        where it matters."""
        (bundle / "tables" / "retired.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Retired Orders\n"
            "description: Superseded order table.\nstatus: deprecated\n---\n\n"
            "# Schema\nOrders, old.\n",
            encoding="utf-8")

        res = await server.search({"bundle": str(bundle), "query": "orders"})
        hits = {r["path"]: r for r in res["results"]}
        assert "/tables/retired.md" in hits, res
        assert hits["/tables/retired.md"]["lifecycle"] == "deprecated"
        assert hits["/tables/orders.md"]["lifecycle"] == "stable"

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
    async def test_root_reindex_recurses_and_cross_links(self, server, bundle):
        """Without dir: EVERY directory gets its index.md, and the root links
        the child indexes with a subtree count -- before, a bundle organised
        in folders read like an empty library at the root."""
        res = await server.reindex({"bundle": str(bundle)})
        assert res["status"] == "ok"
        assert res["entries"] == 2
        assert res["indexes"] == 2

        root_idx = (bundle / "index.md").read_text(encoding="utf-8")
        assert "[tables](/tables/index.md) - 2 concept(s)" in root_idx

        sub_idx = (bundle / "tables" / "index.md").read_text(encoding="utf-8")
        assert "[orders](/tables/orders.md)" in sub_idx
        assert "One row per completed order." in sub_idx

    @pytest.mark.asyncio
    async def test_root_reindex_handles_deep_nesting(self, server, tmp_path):
        """a/b/c.md: the intermediate level (a) without direct concepts also
        gets an index.md that links on to a/b -- the chain does not break."""
        root = tmp_path / "deep"
        (root / "a" / "b").mkdir(parents=True)
        (root / "a" / "b" / "c.md").write_text(
            "---\ntype: note\ndescription: deep leaf\n---\n\nx\n",
            encoding="utf-8")
        (root / "top.md").write_text(
            "---\ntype: note\ndescription: top leaf\n---\n\ny\n",
            encoding="utf-8")

        res = await server.reindex({"bundle": str(root)})
        assert res["entries"] == 2
        assert res["indexes"] == 3  # "", "a", "a/b"

        root_idx = (root / "index.md").read_text(encoding="utf-8")
        assert "[top](/top.md)" in root_idx
        assert "[a](/a/index.md) - 1 concept(s)" in root_idx
        mid_idx = (root / "a" / "index.md").read_text(encoding="utf-8")
        assert "[b](/a/b/index.md) - 1 concept(s)" in mid_idx
        leaf_idx = (root / "a" / "b" / "index.md").read_text(encoding="utf-8")
        assert "[c](/a/b/c.md) - deep leaf" in leaf_idx

    @pytest.mark.asyncio
    async def test_dir_reindex_keeps_single_level_semantics(self, server, tmp_path):
        """With dir the historical behaviour stays: one level, NO child-index
        links, no indexes in deeper levels."""
        root = tmp_path / "single"
        (root / "t" / "deep").mkdir(parents=True)
        (root / "t" / "x.md").write_text(
            "---\ntype: note\ndescription: shallow\n---\n\nx\n",
            encoding="utf-8")
        (root / "t" / "deep" / "y.md").write_text(
            "---\ntype: note\ndescription: hidden\n---\n\ny\n",
            encoding="utf-8")

        res = await server.reindex({"bundle": str(root), "dir": "/t"})
        assert res["entries"] == 1
        idx = (root / "t" / "index.md").read_text(encoding="utf-8")
        assert "[x](/t/x.md)" in idx
        assert "index.md" not in idx.replace("(/t/x.md)", "")
        assert not (root / "t" / "deep" / "index.md").exists()

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
        cfg = ToolServerConfig(type="okf", enabled=True, config={
            "allowed_directories": [str(tmp_path)], "read_only": True})
        ro = OkfServer("okf", mock_system_config, cfg)
        res = await ro.write_concept({
            "bundle": str(bundle), "path": "/x.md",
            "frontmatter": {"type": "T"}, "body": "x"})
        assert res["status"] == "error"
        assert "read-only" in res["error"]


class TestExcludedDirectories:
    """A bundle another instance owns, carved out of a wider root: the shared
    ``okf`` keeps ``data/okf`` but must not reach the sysadmin's ``infra``."""

    @pytest.fixture
    def carved(self, mock_system_config, tmp_path, bundle):
        cfg = ToolServerConfig(type="okf", enabled=True, config={
            "allowed_directories": [str(tmp_path)],
            "excluded_directories": [str(bundle)]})
        return OkfServer("okf", mock_system_config, cfg)

    @pytest.mark.asyncio
    async def test_the_excluded_bundle_cannot_be_written(self, carved, bundle):
        res = await carved.write_concept({
            "bundle": str(bundle), "path": "/x.md",
            "frontmatter": {"type": "T"}, "body": "x"})
        assert res["status"] == "error"
        assert not (bundle / "x.md").exists()

    @pytest.mark.asyncio
    async def test_a_parent_bundle_cannot_reach_into_it(self, carved, bundle, tmp_path):
        """Bundle = the root, concept path = into the carve-out."""
        res = await carved.write_concept({
            "bundle": str(tmp_path), "path": f"/{bundle.name}/x.md",
            "frontmatter": {"type": "T"}, "body": "x"})
        assert res["status"] == "error"
        assert not (bundle / "x.md").exists()

    @pytest.mark.asyncio
    async def test_a_sibling_bundle_still_works(self, carved, tmp_path):
        res = await carved.write_concept({
            "bundle": str(tmp_path / "other"), "path": "/x.md",
            "frontmatter": {"type": "T"}, "body": "x"})
        assert res["status"] == "ok", res
        assert (tmp_path / "other" / "x.md").exists()


# ---------------------------------------------------------------------------
# consumer hook
# ---------------------------------------------------------------------------

class TestContextHook:
    def _server_with_hook(self, mock_system_config, tmp_path, bundle):
        cfg = ToolServerConfig(type="okf", enabled=True, config={
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
    async def test_injected_context_marks_retired_knowledge(
        self, mock_system_config, tmp_path, bundle,
    ):
        """The most dangerous read path: this text lands unasked in the
        system prompt. Retired knowledge (spec §5.4) stays in the bundle
        "for links and history" -- injected unmarked, a model reads it as
        current."""
        (bundle / "tables" / "customers.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Customers\n"
            "description: One row per customer.\nstatus: deprecated\n---\n\n"
            "# Schema\nid, name.\n",
            encoding="utf-8")
        srv = self._server_with_hook(mock_system_config, tmp_path, bundle)
        ctx = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content="tell me about customers")],
            session_id="s1", hook_config={})

        result = await srv.on_pre_llm_call(ctx)
        assert result.success
        injected = self._injected(ctx)
        assert injected and "Customers" in injected, "fixture injected nothing"
        assert "Orders" in injected, (
            "fixture needs BOTH concepts -- otherwise the comparison is empty"
        )
        # Exactly ONE: "marker present" would also stay green if the marker
        # were attached unconditionally to EVERY header -- then every concept
        # in the system prompt would be marked retired, the opposite damage.
        # What is measured is the difference, not the presence.
        assert injected.count("DEPRECATED") == 1, injected

    @pytest.mark.asyncio
    async def test_a_note_the_loop_added_does_not_reseed(self, mock_system_config, tmp_path, bundle):
        """The block sits right behind the system prompt: re-seeded from the
        step budget note it changed on the last calls of a run and re-billed
        the whole conversation."""
        srv = self._server_with_hook(mock_system_config, tmp_path, bundle)
        request = [SimpleNamespace(role="user", content="tell me about customers"),
                   SimpleNamespace(role="assistant", content="reading")]
        note = SimpleNamespace(role="user", injected_by="agent.step_budget",
                               content="This is step 30 of 30, the last one. Finish now.")
        plain = SimpleNamespace(messages=list(request), session_id="s1", hook_config={})
        noted = SimpleNamespace(messages=[*request, note], session_id="s1", hook_config={})

        await srv.on_pre_llm_call(plain)
        await srv.on_pre_llm_call(noted)

        assert self._injected(plain) and "Customers" in self._injected(plain), "fixture: nothing injected"
        assert self._injected(noted) == self._injected(plain)

    @pytest.mark.asyncio
    async def test_hook_config_per_agent_overrides(self, mock_system_config, tmp_path, bundle):
        # Server has NO hook_bundle default; the per-agent hooks.overrides supplies it.
        cfg = ToolServerConfig(type="okf", enabled=True,
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
        second = await srv.on_pre_llm_call(ctx)  # second step
        okf_blocks = [m for m in ctx.messages if getattr(m, "injected_by", None) == "okf"]
        assert len(okf_blocks) == 1  # no stacking
        assert second.modified is False, "an unchanged block is not written again"

    @pytest.mark.asyncio
    async def test_another_question_appends_the_concepts_it_selects(
        self, mock_system_config, tmp_path, bundle,
    ):
        """A guard on mere EXISTENCE would freeze the first selection forever."""
        # A concept the customers/orders pair does not link to: the two-file
        # bundle answers every question with the same subgraph, so it cannot
        # show a selection changing.
        (bundle / "tables" / "warehouses.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Warehouses\n"
            "description: One row per warehouse.\n---\n\n# Schema\nid, city.\n",
            encoding="utf-8")
        srv = self._server_with_hook(mock_system_config, tmp_path, bundle)
        ctx = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content="tell me about customers",
                                      injected_by=None)],
            session_id="s1", hook_config={})
        await srv.on_pre_llm_call(ctx)
        first = self._injected(ctx)
        assert first is not None, "fixture: nothing was injected"
        assert "Warehouses" not in first, "fixture: the first answer already had it"

        ctx.messages.append(SimpleNamespace(role="assistant", content="here you are",
                                            injected_by=None))
        ctx.messages.append(SimpleNamespace(role="user", content="and the warehouses",
                                            injected_by=None))
        result = await srv.on_pre_llm_call(ctx)

        blocks = [m for m in ctx.messages if getattr(m, "injected_by", None) == "okf"]
        assert result.modified is True, (
            "a question about warehouses selects another concept than one "
            "about customers -- if this is False the guard stopped reading the text")
        assert len(blocks) == 2, "the new selection is appended, the old block stays"
        assert blocks[0].content == first
        assert blocks[-1].content != first
        assert ctx.messages[-1] is blocks[-1]

    @pytest.mark.asyncio
    async def test_the_block_is_appended_not_pushed_in_at_the_head(
        self, mock_system_config, tmp_path, bundle,
    ):
        """Position is the point: at the head the block is hoisted into the
        prompt by Anthropic and Gemini, and every rebuild invalidates the
        cached prefix behind it."""
        srv = self._server_with_hook(mock_system_config, tmp_path, bundle)
        before = [SimpleNamespace(role="system", content="you are an agent"),
                  SimpleNamespace(role="user", content="tell me about customers")]
        ctx = SimpleNamespace(messages=list(before), session_id="s1", hook_config={})

        await srv.on_pre_llm_call(ctx)

        assert getattr(ctx.messages[-1], "injected_by", None) == "okf"
        assert ctx.messages[-1].role == DEVELOPER
        assert [m.content for m in ctx.messages[:2]] == [m.content for m in before],             "everything that was there before must stay byte-identical"

    @pytest.mark.asyncio
    async def test_disabled_without_bundle(self, mock_system_config, tmp_path, bundle):
        cfg = ToolServerConfig(type="okf", enabled=True,
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
        "from agent_system.config.models import ToolServerConfig\n"
        "from plugins.okf.server import OkfServer\n"
        "root, tag = sys.argv[1], sys.argv[2]\n"
        "cfg = ToolServerConfig(type='okf', enabled=True,\n"
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


class TestAppendLogTime:
    """The date alone cannot order a burst of entries written the same day."""

    @pytest.mark.asyncio
    async def test_time_is_stamped_automatically(self, server, tmp_path):
        from datetime import datetime

        import pytz
        root = tmp_path / "t1"
        root.mkdir()
        # today IN THE CONFIGURED ZONE — the same date the agent would pass
        today = datetime.now(pytz.timezone("Europe/Berlin")).strftime("%Y-%m-%d")
        res = await server.append_log({"bundle": str(root), "date": today,
                                       "action": "Creation", "description": "x"})
        assert res["status"] == "ok"
        assert res["time"] and len(res["time"]) == len("HH:MM:SS")
        log = (root / "log.md").read_text(encoding="utf-8")
        assert f"* {res['time']} **Creation**: x" in log

    @pytest.mark.asyncio
    async def test_explicit_time_wins(self, server, tmp_path):
        root = tmp_path / "t2"
        root.mkdir()
        res = await server.append_log({"bundle": str(root), "date": "2026-08-04",
                                       "time": "07:15:00", "action": "A",
                                       "description": "x"})
        assert res["time"] == "07:15:00"
        assert "* 07:15:00 **A**: x" in (root / "log.md").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_past_date_gets_no_invented_time(self, server, tmp_path):
        """Stamping the current clock onto a backfilled entry would not be a
        missing detail but a wrong one."""
        root = tmp_path / "t3"
        root.mkdir()
        res = await server.append_log({"bundle": str(root), "date": "2001-01-01",
                                       "action": "Backfill", "description": "x"})
        assert res["time"] is None
        assert "* **Backfill**: x" in (root / "log.md").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_malformed_time_is_rejected(self, server, tmp_path):
        root = tmp_path / "t4"
        root.mkdir()
        res = await server.append_log({"bundle": str(root), "date": "2026-08-04",
                                       "time": "half past two", "description": "x"})
        assert res["status"] == "error"
        assert "HH:MM" in res["error"]
        assert not (root / "log.md").exists()

    @pytest.mark.asyncio
    async def test_time_follows_the_configured_timezone(
            self, mock_system_config, tmp_path):
        """The caller's date comes from {{ current_date }}, which uses the
        agent's configured zone. A UTC time beside a local date would disagree
        by hours — and around midnight by a whole day."""
        from datetime import datetime
        import pytz
        mock_system_config.context.timezone = "Pacific/Kiritimati"  # UTC+14
        cfg = ToolServerConfig(type="okf", enabled=True,
                        config={"allowed_directories": [str(tmp_path)]})
        srv = OkfServer("okf", mock_system_config, cfg)
        root = tmp_path / "t5"
        root.mkdir()

        there = datetime.now(pytz.timezone("Pacific/Kiritimati"))
        res = await srv.append_log({"bundle": str(root),
                                    "date": there.strftime("%Y-%m-%d"),
                                    "action": "A", "description": "x"})
        assert res["time"] is not None, "date valid in that zone was treated as past"
        assert res["time"][:2] == there.strftime("%H")


class TestNotFoundGuidance:
    """A miss should cost one turn, not a guessing game — but a wrong guess
    costs more than the miss."""

    @pytest.mark.asyncio
    async def test_typo_gets_a_suggestion(self, server, bundle):
        res = await server.read_concept(
            {"bundle": str(bundle), "path": "/tables/oders.md"})
        assert res["status"] == "error"
        assert res["did_you_mean"] == "/tables/orders.md"
        assert "did you mean" in res["error"]

    @pytest.mark.asyncio
    async def test_numeric_sibling_gets_no_suggestion(self, server, tmp_path):
        """kapitel_15 missing, kapitel_16 present: different chapters."""
        root = tmp_path / "buch"
        (root / "kapitel").mkdir(parents=True)
        for i in (11, 16, 17):
            (root / "kapitel" / f"kapitel_{i}.md").write_text(
                "---\ntype: kapitel\n---\n\nx\n", encoding="utf-8")
        res = await server.read_concept(
            {"bundle": str(root), "path": "/kapitel/kapitel_15.md"})
        assert res["did_you_mean"] is None
        assert "did you mean" not in res["error"]
        # ... but what DOES exist is listed, so the agent sees the gap
        assert res["available"] == ["/kapitel/kapitel_11.md",
                                    "/kapitel/kapitel_16.md",
                                    "/kapitel/kapitel_17.md"]

    @pytest.mark.asyncio
    async def test_neighbors_guides_too(self, server, bundle):
        res = await server.neighbors(
            {"bundle": str(bundle), "path": "/tables/oders.md"})
        assert res["did_you_mean"] == "/tables/orders.md"


class TestReadConceptPagination:
    """A wiki page can grow large without breaking the hard read limit: a
    generated ledger page measured 70 KB (2026-08-06). Whoever wants to read
    it piece by piece must be able to -- and whoever gets only a piece MUST
    be told, otherwise they judge text they never saw."""

    @pytest.fixture
    def long_page(self, tmp_path):
        """50 lines WITH a trailing newline -- the way generated ledger pages
        are written, the file paging was built for. A body without a final
        newline is the special case, not the rule."""
        root = tmp_path / "gross_bundle"
        root.mkdir()
        (root / "lang.md").write_text(
            "---\ntype: notiz\n---\n"
            + "".join(f"Zeile {i}\n" for i in range(1, 51)),
            encoding="utf-8")
        return root

    @pytest.mark.asyncio
    async def test_no_arguments_returns_everything(self, server, long_page):
        """The default must NOT change -- existing callers keep getting the
        whole page."""
        res = await server.read_concept({"bundle": str(long_page),
                                         "path": "lang.md"})
        assert res["status"] == "ok"
        assert res["lines_total"] == 50
        assert "lines_remaining" not in res
        assert res["body"].splitlines()[-1] == "Zeile 50"

    @pytest.mark.asyncio
    async def test_slice_and_hint_for_the_rest(self, server, long_page):
        res = await server.read_concept({
            "bundle": str(long_page), "path": "lang.md",
            "start_line": 11, "line_count": 5,
        })
        assert res["body"].splitlines() == [f"Zeile {i}" for i in range(11, 16)]
        assert res["lines_returned"] == 5
        assert res["lines_remaining"] == 35
        assert "start_line=16" in res["hint"], (
            "the hint must say HOW to continue -- otherwise the caller "
            "guesses the next position"
        )

    @pytest.mark.asyncio
    async def test_last_slice_reports_no_remainder(self, server, long_page):
        res = await server.read_concept({
            "bundle": str(long_page), "path": "lang.md", "start_line": 16,
        })
        assert res["body"].splitlines()[-1] == "Zeile 50"
        assert "lines_remaining" not in res, (
            "a fully read remainder must not carry a keep-reading hint -- "
            "otherwise an agent loops"
        )

    @pytest.mark.asyncio
    async def test_trailing_newline_is_not_a_line(self, server, long_page):
        """Whoever has read the last line is done. If the empty tail of
        `split` counts, the answer still reports a remainder and sends the
        reader off to a line that does not exist."""
        res = await server.read_concept({
            "bundle": str(long_page), "path": "lang.md",
            "start_line": 46, "line_count": 5,
        })
        assert res["lines_total"] == 50
        assert res["lines_returned"] == 5
        assert "lines_remaining" not in res
        assert "hint" not in res

    @pytest.mark.asyncio
    async def test_round_trip_keeps_the_trailing_newline(self, server, long_page):
        """`dump_frontmatter` writes the body verbatim: whatever reading cuts
        off is gone after a write-back."""
        r1 = await server.read_concept({"bundle": str(long_page), "path": "lang.md"})
        assert r1["body"].endswith("Zeile 50\n")
        await server.write_concept({
            "bundle": str(long_page), "path": "/lang.md",
            "frontmatter": r1["frontmatter"], "body": r1["body"]})
        r2 = await server.read_concept({"bundle": str(long_page), "path": "lang.md"})
        assert r2["body"] == r1["body"]
        assert r2["lines_total"] == 50, "the page must not grow during the round trip"

    @pytest.mark.asyncio
    async def test_start_past_the_end_is_an_error(self, server, long_page):
        """The silent zero case: empty body, status ok, no remainder, no
        hint -- indistinguishable from 'the page is finished'. A page can
        shrink between two calls (the ledger is rewritten at every
        measuring point), so a remembered line number can go stale."""
        res = await server.read_concept({
            "bundle": str(long_page), "path": "lang.md", "start_line": 51,
        })
        assert res["status"] == "error"
        assert "51" in res["error"] and "50" in res["error"]
        assert res["lines_total"] == 50

    @pytest.mark.asyncio
    async def test_empty_page_is_not_an_error(self, server, tmp_path):
        """Nothing to read is not an overshoot: the check must not confuse
        the empty page with a stray line number."""
        root = tmp_path / "leer_bundle"
        root.mkdir()
        (root / "leer.md").write_text("---\ntype: notiz\n---\n", encoding="utf-8")
        res = await server.read_concept({"bundle": str(root), "path": "leer.md"})
        assert res["status"] == "ok"
        assert res["body"] == ""
        assert "lines_remaining" not in res

    @pytest.mark.asyncio
    async def test_line_count_larger_than_the_rest(self, server, long_page):
        res = await server.read_concept({
            "bundle": str(long_page), "path": "lang.md",
            "start_line": 48, "line_count": 999,
        })
        assert res["lines_returned"] == 3
        assert "lines_remaining" not in res

    @pytest.mark.asyncio
    async def test_negative_line_count_does_not_cut_from_the_end(self, server, long_page):
        """`minimum: 1` is in the schema, but nobody enforces it on this
        path. Without a clamp `all_lines[0:-5]` would apply and return 45
        lines as 'the first -5'."""
        res = await server.read_concept({
            "bundle": str(long_page), "path": "lang.md", "line_count": -5,
        })
        assert res["body"].splitlines() == ["Zeile 1"]
        assert res["lines_remaining"] == 49


# ---------------------------------------------------------------------------
# bugs found while writing the guide
# ---------------------------------------------------------------------------

class TestHostPathsAreRefusedUnopened:
    """``\\\\host\\share`` is opened by resolve() -- a connection to the host
    with the user's credentials -- so it must be refused on the text alone."""

    @pytest.fixture
    def opened(self, monkeypatch):
        import pathlib
        seen = []
        real = pathlib.Path.resolve

        def recording(self, *a, **kw):
            seen.append(str(self))
            return real(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "resolve", recording)
        return seen

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", [r"\\evilhost\share\b", "//evilhost/share/b"])
    async def test_bundle(self, server, opened, host):
        res = await server.validate({"bundle": host})
        assert res["status"] == "error"
        assert not [p for p in opened if "evilhost" in p]

    @pytest.mark.asyncio
    async def test_concept_path(self, server, bundle, opened):
        res = await server.read_concept(
            {"bundle": str(bundle), "path": r"\\evilhost\share\x.md"})
        assert res["status"] == "error"
        assert not [p for p in opened if "evilhost" in p]

    @pytest.mark.asyncio
    async def test_log_dir(self, server, bundle, opened):
        res = await server.append_log({
            "bundle": str(bundle), "dir": r"\\evilhost\share",
            "date": "2026-01-01", "description": "x"})
        assert res["status"] == "error"
        assert not [p for p in opened if "evilhost" in p]


class TestBundleCache:
    @pytest.mark.asyncio
    async def test_a_renamed_concept_is_seen(self, server, bundle):
        """A rename keeps the file count and every mtime."""
        await server.list({"bundle": str(bundle)})
        (bundle / "tables" / "orders.md").rename(bundle / "tables" / "sales.md")
        res = await server.list({"bundle": str(bundle)})
        assert {c["path"] for c in res["concepts"]} == {
            "/tables/sales.md", "/tables/customers.md"}


class TestArgumentBounds:
    @pytest.fixture
    def many(self, tmp_path):
        root = tmp_path / "many"
        root.mkdir()
        for i in range(60):
            nxt = f"[n](/c{i + 1}.md)" if i < 59 else ""
            (root / f"c{i}.md").write_text(
                f"---\ntype: t\n---\n\napple {nxt}\n", encoding="utf-8")
        return root

    @pytest.mark.asyncio
    async def test_search_limit_is_clamped(self, server, many):
        res = await server.search({"bundle": str(many), "query": "apple", "limit": 100})
        assert res["count"] == 50
        res = await server.search({"bundle": str(many), "query": "apple", "limit": -1})
        assert res["count"] == 1

    @pytest.mark.asyncio
    async def test_subgraph_depth_is_clamped(self, server, many):
        res = await server.subgraph({"bundle": str(many), "seed": "/c0.md", "depth": 99})
        assert res["depth"] == 5
        assert res["count"] == 6
        res = await server.subgraph({"bundle": str(many), "seed": "/c0.md", "depth": None})
        assert res["count"] == 2

    @pytest.mark.asyncio
    async def test_graph_tools_take_a_path_without_slash(self, server, bundle):
        """read/write accept it; subgraph dropped such a seed silently."""
        res = await server.neighbors({"bundle": str(bundle), "path": "tables/orders.md"})
        assert res["neighbors"] == ["/tables/customers.md"]
        res = await server.subgraph({"bundle": str(bundle), "seed": "tables/orders.md"})
        assert res["count"] == 2


class TestWriteKeepsData:
    @pytest.mark.asyncio
    async def test_frontmatter_update_without_body_keeps_the_body(self, server, bundle):
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/tables/orders.md",
            "frontmatter": {"status": "deprecated"}})
        assert res["status"] == "ok"
        read = await server.read_concept({"bundle": str(bundle), "path": "/tables/orders.md"})
        assert "Joined with [customers]" in read["body"]
        assert read["frontmatter"]["status"] == "deprecated"

    @pytest.mark.asyncio
    async def test_an_empty_body_still_clears(self, server, bundle):
        await server.write_concept({
            "bundle": str(bundle), "path": "/tables/orders.md",
            "frontmatter": {"type": "T"}, "body": ""})
        read = await server.read_concept({"bundle": str(bundle), "path": "/tables/orders.md"})
        assert read["body"] == ""

    @pytest.mark.asyncio
    async def test_a_non_markdown_path_is_refused(self, server, bundle):
        """Answered ok before, and the file never showed up as a concept."""
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/notes.txt",
            "frontmatter": {"type": "T"}, "body": "x"})
        assert res["status"] == "error"
        assert not (bundle / "notes.txt").exists()


class TestReadSizeCap:
    @pytest.fixture
    def big(self, mock_system_config, tmp_path):
        cfg = ToolServerConfig(type="okf", enabled=True, config={
            "allowed_directories": [str(tmp_path)], "max_concept_file_kb": 1})
        srv = OkfServer("okf", mock_system_config, cfg)
        root = tmp_path / "b"
        root.mkdir()
        (root / "big.md").write_text(
            "---\ntype: t\n---\n\n" + "".join(f"{'x' * 99}\n" for _ in range(30)),
            encoding="utf-8")
        return srv, root

    @pytest.mark.asyncio
    async def test_a_whole_read_over_the_cap_is_refused(self, big):
        srv, root = big
        res = await srv.read_concept({"bundle": str(root), "path": "/big.md"})
        assert res["status"] == "error"
        assert "body" not in res
        assert res["lines_total"] == 30

    @pytest.mark.asyncio
    async def test_a_large_page_stays_readable_in_slices(self, big):
        srv, root = big
        res = await srv.read_concept({"bundle": str(root), "path": "/big.md",
                                      "start_line": 21, "line_count": 5})
        assert res["status"] == "ok"
        assert res["lines_returned"] == 5

class TestLogDateIsReal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [
        {"date": "2026-13-45"},
        {"date": "2026-01-01", "time": "25:61"},
    ])
    async def test_impossible_values_are_refused(self, server, bundle, args):
        res = await server.append_log({"bundle": str(bundle), "description": "x", **args})
        assert res["status"] == "error"
        assert not (bundle / "log.md").exists()


class TestHookSeedConcept:
    @pytest.mark.asyncio
    async def test_anchor_without_slash_is_found(self, mock_system_config, tmp_path, bundle):
        """Written as tables/customers.md it was silently ignored."""
        cfg = ToolServerConfig(type="okf", enabled=True,
                               config={"allowed_directories": [str(tmp_path)]})
        srv = OkfServer("okf", mock_system_config, cfg)
        ctx = SimpleNamespace(
            messages=[SimpleNamespace(role="user", content="nothing matches here")],
            session_id="s1",
            hook_config={"hook_bundle": str(bundle),
                         "hook_seed_concept": "tables/customers.md"})
        result = await srv.on_pre_llm_call(ctx)
        assert result.modified is True
        assert "Customers" in ctx.messages[-1].content


class TestReindexKeepsIndexFrontmatter:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [{}, {"dir": "/tables"}])
    async def test_okf_version_survives(self, server, bundle, args):
        index = bundle / "tables" / "index.md" if args else bundle / "index.md"
        index.write_text("---\nokf_version: '0.1'\n---\n\n# Old\n", encoding="utf-8")
        res = await server.reindex({"bundle": str(bundle), **args})
        assert res["status"] == "ok"
        text = index.read_text(encoding="utf-8")
        assert text.startswith("---\nokf_version: '0.1'\n---\n")
        assert "](/tables/" in text
        assert "# Old" not in text


class TestSearchWords:
    @pytest.mark.asyncio
    async def test_non_ascii_words_stay_whole(self, server, tmp_path):
        root = tmp_path / "words"
        root.mkdir()
        (root / "a.md").write_text("---\ntype: t\n---\n\nStraße\n", encoding="utf-8")
        (root / "b.md").write_text("---\ntype: t\n---\n\nstra e\n", encoding="utf-8")
        res = await server.search({"bundle": str(root), "query": "Straße"})
        assert [r["path"] for r in res["results"]] == ["/a.md"]


class _Recorder:
    def __init__(self):
        self.lines = []

    async def progress(self, message, meta=None):
        self.lines.append(message)

    async def end(self, message="completed", meta=None):
        self.lines.append(message)

    async def error(self, message, meta=None):
        self.lines.append(message)


class TestReadTextsAreEnglish:
    @pytest.fixture
    def page(self, tmp_path):
        root = tmp_path / "page"
        root.mkdir()
        (root / "p.md").write_text(
            "---\ntype: t\n---\n" + "".join(f"line {i}\n" for i in range(1, 21)),
            encoding="utf-8")
        return root

    @pytest.mark.asyncio
    async def test_slice_hint_and_status_line(self, server, page):
        rec = _Recorder()
        res = await server.read_concept({"bundle": str(page), "path": "/p.md",
                                         "start_line": 3, "line_count": 5,
                                         "_status": rec})
        assert "continue with start_line=8" in res["hint"]
        assert "lines 3-7 of 20" in rec.lines[-1]

    @pytest.mark.asyncio
    async def test_start_past_the_end(self, server, page):
        res = await server.read_concept({"bundle": str(page), "path": "/p.md",
                                         "start_line": 21})
        assert res["error"] == ("start_line=21 is past the end: the page has "
                                "20 line(s). Nothing read.")


class TestListIsBounded:
    @pytest.mark.asyncio
    async def test_a_large_bundle_is_cut_and_says_so(self, server, tmp_path):
        from plugins.okf import server as okf_server
        root = tmp_path / "large"
        root.mkdir()
        for i in range(okf_server.LIST_LIMIT + 5):
            (root / f"c{i:04d}.md").write_text("---\ntype: t\n---\n", encoding="utf-8")
        res = await server.list({"bundle": str(root)})
        assert res["count"] == okf_server.LIST_LIMIT + 5
        assert len(res["concepts"]) == okf_server.LIST_LIMIT
        assert res["omitted"] == 5
        assert "'dir'" in res["hint"]


class TestReadOnlySchema:
    def _names(self, mock_system_config, tmp_path, read_only):
        cfg = ToolServerConfig(type="okf", enabled=True, config={
            "allowed_directories": [str(tmp_path)], "read_only": read_only})
        srv = OkfServer("okf", mock_system_config, cfg)
        return {t["function"]["name"] for t in srv.get_tools()}

    def test_write_tools_are_not_offered_when_read_only(self, mock_system_config, tmp_path):
        writes = {"okf_write_concept", "okf_append_log", "okf_reindex"}
        assert writes <= self._names(mock_system_config, tmp_path, False)
        ro = self._names(mock_system_config, tmp_path, True)
        assert not writes & ro
        assert {"okf_list", "okf_read_concept", "okf_validate"} <= ro


class TestUndecodableFiles:
    @pytest.mark.asyncio
    async def test_one_non_utf8_concept_is_skipped_not_fatal(self, server, bundle):
        (bundle / "tables" / "bad.md").write_bytes(b"---\ntype: t\n---\n\n\xff\xfe\n")
        res = await server.list({"bundle": str(bundle)})
        assert res["status"] == "ok"
        assert "/tables/bad.md" not in {c["path"] for c in res["concepts"]}
        assert res["count"] == 2

    @pytest.mark.asyncio
    async def test_a_broken_index_is_rewritten(self, server, bundle):
        (bundle / "index.md").write_bytes(b"\xff\xfe broken")
        res = await server.reindex({"bundle": str(bundle)})
        assert res["status"] == "ok"
        assert (bundle / "index.md").read_text(encoding="utf-8").startswith("# Contents")


class TestRepairKeepsBodyExact:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", ["type: [unclosed", "- a list"])
    async def test_no_blank_line_is_added(self, server, tmp_path, header):
        root = tmp_path / "repair"
        root.mkdir()
        (root / "c.md").write_text(f"---\n{header}\n---\n\nBody\n", encoding="utf-8")
        for _ in range(2):
            res = await server.write_concept({"bundle": str(root), "path": "/c.md",
                                              "frontmatter": {"type": "T"}})
            assert res["status"] == "ok"
        assert (root / "c.md").read_text(encoding="utf-8") == "---\ntype: T\n---\n\nBody\n"
