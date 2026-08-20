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
    async def test_foreign_status_value_is_warned_about(self, server, bundle):
        """``status`` gehoert dem Format (§5.4), nicht dem Produzenten.

        Gemessen am 20.08.2026: ein Bundle hatte sein Publish-Urteil dort
        abgelegt (``status: publishable``) — jeder Leser bekam damit einen
        Lebenszustand, den das Vokabular nicht kennt, und niemand merkte
        es. Warnung, kein Fehler: Extra-Keys sind erlaubt, dieser ist nur
        belegt."""
        (bundle / "tables" / "occupied.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Occupied\n"
            "description: d\nstatus: publishable\n---\n\n# S\n",
            encoding="utf-8")

        res = await server.validate({"bundle": str(bundle)})
        assert res["conformant"] is True, "Extra-Keys sind erlaubt — kein Fehler"
        assert res["errors"] == 0
        hits = [f for f in res["findings"]
                if f["rule"] == "status-vocabulary"]
        assert len(hits) == 1, res["findings"]
        assert "publishable" in hits[0]["message"]

    @pytest.mark.asyncio
    async def test_non_string_status_is_warned_about_too(self, server, bundle):
        """YAML typisiert den Wert, nicht der Produzent: ``status: true``
        wird bool, ``status: 2026-01-01`` ein Datum. Pruefte die Regel den
        normalisierten Wert, saehe sie ueberall "stable" und schwiege
        ausgerechnet bei den Faellen, fuer die sie existiert."""
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
        """Ein geleertes Feld sagt nichts Unbekanntes — es sagt nichts.
        Warnte es, waere das Aufraeumen (`status` leeren) selbst ein
        Verstoss."""
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
        """Die drei erlaubten Werte duerfen NICHT warnen — sonst waere die
        Warnung Rauschen und der Kurator gewoehnte sich ab hinzusehen."""
        for i, value in enumerate(("draft", "stable", "deprecated")):
            (bundle / "tables" / f"ok_{i}.md").write_text(
                f"---\ntype: BigQuery Table\ntitle: T{i}\n"
                f"description: d\nstatus: {value}\n---\n\n# S\n",
                encoding="utf-8")

        res = await server.validate({"bundle": str(bundle)})
        # Ohne diese Zusicherung bestuende der Test auch, wenn die Fixture
        # nie angekommen waere — eine leere Findings-Liste ist dann kein
        # Beleg, sondern eine Abwesenheit.
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

        # Unlesbares Frontmatter meldet ``lifecycle: stable`` — ein Default,
        # kein Urteil. Vertretbar ist das nur, WEIL dieselbe Datei sich
        # nebenan als kaputt zu erkennen gibt (type=None + dieser Fehler).
        # Faellt eines der beiden weg, behauptet das Bundle Aktualitaet
        # ueber eine Datei, die niemand lesen konnte.
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
        """Spec §5.4: OKF loescht nicht, es setzt ``deprecated`` ("kept for
        links and history"). Steht das nicht in der Uebersicht, muesste ein
        Leser jedes Konzept einzeln oeffnen, um zurueckgezogenes Wissen zu
        erkennen — und zitiert es bis dahin als gueltig."""
        (bundle / "tables" / "legacy.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Legacy\n"
            "description: Superseded by orders.\nstatus: deprecated\n---\n\n"
            "# Schema\nOld.\n",
            encoding="utf-8")

        res = await server.list({"bundle": str(bundle)})
        by_path = {c["path"]: c for c in res["concepts"]}
        assert by_path["/tables/legacy.md"]["lifecycle"] == "deprecated"
        # Fehlendes Feld heisst laut Spec ``stable`` — NICHT leer/None, sonst
        # muesste jeder Konsument den Default selbst kennen.
        assert by_path["/tables/orders.md"]["lifecycle"] == "stable"

    @pytest.mark.asyncio
    async def test_lifecycle_is_always_one_of_the_three(self, server, bundle):
        """Die Tool-Beschreibung sagt dem Modell drei Werte zu — dann darf
        hier kein vierter herauskommen. Im Repo liegen echte Bundles, deren
        Produzent seinen eigenen Zustand in ``status`` abgelegt hat; das
        Modell kann damit nichts anfangen und raet. Der Rohwert geht nicht
        verloren: ``validate`` warnt darueber."""
        (bundle / "tables" / "foreign.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Foreign\n"
            "description: d\nstatus: blockiert\n---\n\n# S\n",
            encoding="utf-8")
        # Gross-/Kleinschreibung darf nicht durchrutschen: der
        # Injection-Hook vergleicht auf das exakte Wort.
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
        """Die Naht, an der die ganze Kette haengt: ein Kurator SETZT
        ``status`` per ``write_concept``, ein Leser sieht es in ``list``,
        und ein spaeteres Zuruecknehmen macht es rueckgaengig. Ohne diesen
        Weg waere „deprecated statt loeschen" eine Absichtserklaerung —
        die Felder sind nicht in ``RESERVED_FIELDS``, sie ueberleben nur,
        weil das Format Extra-Keys durchreicht."""
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/tables/orders.md",
            "frontmatter": {"type": "BigQuery Table", "status": "deprecated"},
            "body": "Superseded.",
        })
        assert res["status"] == "ok", res

        lst = await server.list({"bundle": str(bundle)})
        entry = next(c for c in lst["concepts"] if c["path"] == "/tables/orders.md")
        assert entry["lifecycle"] == "deprecated"
        assert entry["title"] == "Orders", "Merge hat bestehende Keys verloren"

        # Zuruecknehmen: ``status`` raus -> wieder Default ``stable``.
        res = await server.write_concept({
            "bundle": str(bundle), "path": "/tables/orders.md",
            "frontmatter": {"type": "BigQuery Table", "status": None},
            "body": "Back in service.",
        })
        assert res["status"] == "ok", res
        lst = await server.list({"bundle": str(bundle)})
        entry = next(c for c in lst["concepts"] if c["path"] == "/tables/orders.md")
        assert entry["lifecycle"] == "stable"

        # Neuanlage statt Overwrite: dort gibt es kein bestehendes
        # Frontmatter zum Mergen — der Zweig muss ``status`` genauso
        # uebernehmen, sonst laesst sich ein Konzept nur zurueckziehen,
        # wenn es vorher schon existierte.
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
        """index.md ist der Einstieg, den ein Leser zuerst oeffnet. Bliebe
        die Markierung `list`/`search` vorbehalten, fehlte sie genau dort,
        wo der Ueberblick entsteht."""
        (bundle / "tables" / "old.md").write_text(
            "---\ntype: BigQuery Table\ntitle: Old\n"
            "description: Superseded table.\nstatus: deprecated\n---\n\n# S\n",
            encoding="utf-8")

        await server.reindex({"bundle": str(bundle)})
        idx = (bundle / "tables" / "index.md").read_text(encoding="utf-8")
        assert "[old](/tables/old.md) - [deprecated] Superseded table." in idx, idx
        # Aktuelle Konzepte bleiben unmarkiert — sonst waere die Markierung
        # wertlos, weil sie nichts unterscheidet.
        assert idx.count("[deprecated]") == 1, idx

    @pytest.mark.asyncio
    async def test_search_marks_retired_concepts_too(self, server, bundle):
        """Die Suche ist fuer die meisten Leser der EINSTIEG ins Bundle —
        der book_launcher wird fuer Details ausdruecklich hierher
        geschickt. Ohne Kennzeichnung zitierte er zurueckgezogenes Wissen
        als gueltig; ``deprecated`` waere genau dort unsichtbar, wo es
        zaehlt."""
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
        """Ohne dir: JEDES Verzeichnis bekommt sein index.md, und die Wurzel
        verlinkt die Kind-Indizes mit Teilbaum-Zahl — vorher las sich ein in
        Ordner organisiertes Bundle an der Wurzel wie eine leere Bibliothek."""
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
        """a/b/c.md: auch die Zwischenebene (a) ohne direkte Konzepte bekommt
        ein index.md, das auf a/b weiterverlinkt — die Kette reisst nicht."""
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
        """Mit dir bleibt das historische Verhalten: eine Ebene, KEINE
        Kind-Index-Links, keine Indizes in Untertiefen."""
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
    async def test_injected_context_marks_retired_knowledge(
        self, mock_system_config, tmp_path, bundle,
    ):
        """Der gefaehrlichste Lesepfad: dieser Text landet ungefragt im
        System-Prompt. Zurueckgezogenes Wissen (spec §5.4) bleibt im
        Bundle „for links and history" — unmarkiert injiziert liest es ein
        Modell aber als geltend."""
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
        assert injected and "Customers" in injected, "Fixture hat nichts injiziert"
        assert "Orders" in injected, (
            "Fixture braucht BEIDE Konzepte — sonst ist der Vergleich leer"
        )
        # Genau EINS: „Marker vorhanden" wuerde auch gruen bleiben, wenn der
        # Marker unbedingt an JEDEN Header ginge — dann waere jedes Konzept
        # im System-Prompt als zurueckgezogen markiert, der umgekehrte
        # Schaden. Gemessen wird der Unterschied, nicht die Anwesenheit.
        assert injected.count("DEPRECATED") == 1, injected

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
        cfg = MCPConfig(type="okf", enabled=True,
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


class TestReadConceptPaginierung:
    """Eine Wiki-Seite kann gross werden, ohne die harte Lesegrenze zu
    reissen: der Fall-Ledger eines Buchs lag gemessen bei 70 KB
    (2026-08-06). Wer sie stueckweise lesen will, muss das koennen — und
    wer nur ein Stueck bekommt, MUSS es erfahren, sonst urteilt er ueber
    Text, den er nie gesehen hat."""

    @pytest.fixture
    def gross(self, tmp_path):
        """50 Zeilen MIT abschliessendem Umbruch — so schreibt
        `render_faelle_md` (graph_ledger.py), die Datei, fuer die das
        Blaettern gebaut wurde. Ein Body ohne Schluss-Umbruch ist der
        Sonderfall, nicht der Regelfall."""
        root = tmp_path / "gross_bundle"
        root.mkdir()
        (root / "lang.md").write_text(
            "---\ntype: notiz\n---\n"
            + "".join(f"Zeile {i}\n" for i in range(1, 51)),
            encoding="utf-8")
        return root

    @pytest.mark.asyncio
    async def test_ohne_angabe_kommt_alles(self, server, gross):
        """Der Default darf sich NICHT aendern — bestehende Aufrufer
        bekommen weiter die ganze Seite."""
        res = await server.read_concept({"bundle": str(gross),
                                         "path": "lang.md"})
        assert res["status"] == "ok"
        assert res["lines_total"] == 50
        assert "lines_remaining" not in res
        assert res["body"].splitlines()[-1] == "Zeile 50"

    @pytest.mark.asyncio
    async def test_ausschnitt_und_hinweis_auf_den_rest(self, server, gross):
        res = await server.read_concept({
            "bundle": str(gross), "path": "lang.md",
            "start_line": 11, "line_count": 5,
        })
        assert res["body"].splitlines() == [f"Zeile {i}" for i in range(11, 16)]
        assert res["lines_returned"] == 5
        assert res["lines_remaining"] == 35
        assert "start_line=16" in res["hint"], (
            "der Hinweis muss sagen, WIE es weitergeht — sonst raet der "
            "Aufrufer die naechste Position"
        )

    @pytest.mark.asyncio
    async def test_letzter_ausschnitt_meldet_keinen_rest(self, server, gross):
        res = await server.read_concept({
            "bundle": str(gross), "path": "lang.md", "start_line": 16,
        })
        assert res["body"].splitlines()[-1] == "Zeile 50"
        assert "lines_remaining" not in res, (
            "ein vollstaendig gelesener Rest darf keinen Weiterlese-Hinweis "
            "tragen — sonst laeuft ein Agent im Kreis"
        )

    @pytest.mark.asyncio
    async def test_schluss_umbruch_ist_keine_zeile(self, server, gross):
        """Wer die letzte Zeile gelesen hat, ist fertig. Zaehlt das leere
        Endstueck von `split` mit, meldet die Antwort noch einen Rest und
        schickt den Leser auf eine Zeile los, die es nicht gibt."""
        res = await server.read_concept({
            "bundle": str(gross), "path": "lang.md",
            "start_line": 46, "line_count": 5,
        })
        assert res["lines_total"] == 50
        assert res["lines_returned"] == 5
        assert "lines_remaining" not in res
        assert "hint" not in res

    @pytest.mark.asyncio
    async def test_umlauf_verliert_den_schluss_umbruch_nicht(self, server, gross):
        """`dump_frontmatter` schreibt den Body verbatim: was das Lesen
        abschneidet, ist nach einem Rueckschreiben weg."""
        r1 = await server.read_concept({"bundle": str(gross), "path": "lang.md"})
        assert r1["body"].endswith("Zeile 50\n")
        await server.write_concept({
            "bundle": str(gross), "path": "/lang.md",
            "frontmatter": r1["frontmatter"], "body": r1["body"]})
        r2 = await server.read_concept({"bundle": str(gross), "path": "lang.md"})
        assert r2["body"] == r1["body"]
        assert r2["lines_total"] == 50, "die Seite darf beim Umlauf nicht wachsen"

    @pytest.mark.asyncio
    async def test_start_hinter_dem_ende_ist_ein_fehler(self, server, gross):
        """Der stille Null-Fall: leerer Body, status ok, kein Rest, kein
        Hinweis — nicht von 'die Seite ist zu Ende' zu unterscheiden. Eine
        Seite kann zwischen zwei Aufrufen schrumpfen (der Fall-Ledger wird
        an jedem Messpunkt neu geschrieben), eine gemerkte Zeilennummer
        also veralten."""
        res = await server.read_concept({
            "bundle": str(gross), "path": "lang.md", "start_line": 51,
        })
        assert res["status"] == "error"
        assert "51" in res["error"] and "50" in res["error"]
        assert res["lines_total"] == 50

    @pytest.mark.asyncio
    async def test_leere_seite_ist_kein_fehler(self, server, tmp_path):
        """Nichts zu lesen ist kein Ueberschiessen: die Abfrage darf die
        leere Seite nicht mit einer verlaufenen Zeilennummer verwechseln."""
        root = tmp_path / "leer_bundle"
        root.mkdir()
        (root / "leer.md").write_text("---\ntype: notiz\n---\n", encoding="utf-8")
        res = await server.read_concept({"bundle": str(root), "path": "leer.md"})
        assert res["status"] == "ok"
        assert res["body"] == ""
        assert "lines_remaining" not in res

    @pytest.mark.asyncio
    async def test_line_count_groesser_als_der_rest(self, server, gross):
        res = await server.read_concept({
            "bundle": str(gross), "path": "lang.md",
            "start_line": 48, "line_count": 999,
        })
        assert res["lines_returned"] == 3
        assert "lines_remaining" not in res

    @pytest.mark.asyncio
    async def test_negatives_line_count_schneidet_nicht_vom_ende(self, server, gross):
        """`minimum: 1` steht im Schema, aber in diesem Pfad prueft es
        niemand nach. Ohne Klammer wuerde `alle[0:-5]` greifen und 45
        Zeilen als 'die ersten -5' ausgeben."""
        res = await server.read_concept({
            "bundle": str(gross), "path": "lang.md", "line_count": -5,
        })
        assert res["body"].splitlines() == ["Zeile 1"]
        assert res["lines_remaining"] == 49
