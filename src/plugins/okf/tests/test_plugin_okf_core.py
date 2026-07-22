"""Unit tests for the OKF core library (src/plugins/okf/core.py).

Covers the load-bearing spec guarantees: frontmatter round-trip fidelity
(unknown keys / order / YAML 1.2 preserved), conformance validation (type MUST),
bundle-relative link resolution, graph traversal, and index/log conventions.
"""
from plugins.okf import core


# --------------------------------------------------------------------------
# Frontmatter round-trip
# --------------------------------------------------------------------------

class TestFrontmatterRoundTrip:
    def test_parse_basic(self):
        text = "---\ntype: BigQuery Table\ntitle: Orders\n---\n\n# Schema\nbody\n"
        fm, body, err = core.parse_frontmatter(text)
        assert err is None
        assert fm["type"] == "BigQuery Table"
        assert fm["title"] == "Orders"
        assert body.startswith("# Schema")

    def test_no_frontmatter_is_tolerated(self):
        fm, body, err = core.parse_frontmatter("# Just a heading\ntext")
        assert fm is None and err is None
        assert body.startswith("# Just a heading")

    def test_malformed_yaml_reports_not_raises(self):
        fm, _body, err = core.parse_frontmatter("---\ntype: [unclosed\n---\nbody")
        assert fm is None
        assert err is not None and "YAML" in err

    def test_unknown_keys_preserved(self):
        text = ("---\ntype: Metric\ncustom_key: 42\nnested:\n  a: 1\n"
                "tags: [x, y]\n---\n\nbody\n")
        fm, body, _ = core.parse_frontmatter(text)
        out = core.dump_frontmatter(fm, body)
        fm2, _, _ = core.parse_frontmatter(out)
        assert fm2["custom_key"] == 42
        assert fm2["nested"]["a"] == 1
        assert list(fm2["tags"]) == ["x", "y"]

    def test_key_order_preserved(self):
        text = "---\ntype: T\nzebra: 1\napple: 2\nmango: 3\n---\n\nbody\n"
        fm, body, _ = core.parse_frontmatter(text)
        out = core.dump_frontmatter(fm, body)
        # zebra/apple/mango must NOT be alphabetized
        assert out.index("zebra") < out.index("apple") < out.index("mango")

    def test_yaml_12_no_norway_problem(self):
        # YAML 1.1 would coerce NO/yes/off to booleans; ruamel (1.2) keeps strings.
        text = "---\ntype: T\ncountry: NO\nflag: yes\nmode: off\n---\n\nbody\n"
        fm, _body, err = core.parse_frontmatter(text)
        assert err is None
        assert fm["country"] == "NO"
        assert fm["flag"] == "yes"
        assert fm["mode"] == "off"

    def test_round_trip_is_idempotent(self):
        text = "---\ntype: T\ntitle: X\n---\n\n# Body\ncontent here\n"
        fm, body, _ = core.parse_frontmatter(text)
        out1 = core.dump_frontmatter(fm, body)
        fm2, body2, _ = core.parse_frontmatter(out1)
        out2 = core.dump_frontmatter(fm2, body2)
        assert out1 == out2

    def test_comment_preserved_in_frontmatter(self):
        text = "---\ntype: T  # inline note\ntitle: X\n---\n\nbody\n"
        fm, body, _ = core.parse_frontmatter(text)
        out = core.dump_frontmatter(fm, body)
        assert "# inline note" in out

    def test_crlf_separator_stripped(self):
        fm, body, err = core.parse_frontmatter(
            "---\r\ntype: T\r\n---\r\n\r\n# Body\r\nx\r\n")
        assert err is None and fm["type"] == "T"
        assert body.startswith("# Body")  # no spurious leading \r\n

    def test_leading_blank_body_preserved(self):
        # a body that legitimately starts with a blank line survives round-trip
        text = "---\ntype: T\n---\n\n\nHello\n"
        fm, body, _ = core.parse_frontmatter(text)
        assert body.startswith("\nHello")  # one intentional blank kept
        out = core.dump_frontmatter(fm, body)
        fm2, body2, _ = core.parse_frontmatter(out)
        assert body2 == body  # idempotent


class TestMergeFrontmatter:
    def test_deep_merge_keeps_siblings(self):
        base = {"type": "T", "meta": {"a": 1, "b": 2}}
        core.merge_frontmatter(base, {"meta": {"b": 9, "c": 3}})
        assert base["meta"] == {"a": 1, "b": 9, "c": 3}

    def test_none_clears_key(self):
        base = {"type": "T", "drop": 1}
        core.merge_frontmatter(base, {"drop": None})
        assert "drop" not in base

    def test_scalar_replaces(self):
        base = {"type": "T", "tags": ["a"]}
        core.merge_frontmatter(base, {"tags": ["b", "c"]})
        assert base["tags"] == ["b", "c"]

    def test_base_none_copies_updates(self):
        out = core.merge_frontmatter(None, {"type": "T"})
        assert out == {"type": "T"}


# --------------------------------------------------------------------------
# Link resolution
# --------------------------------------------------------------------------

class TestLinkResolution:
    def test_bundle_relative(self):
        assert core._resolve_link("/tables/orders.md", "/tables/customers.md") == \
            "/tables/customers.md"

    def test_relative_dot(self):
        assert core._resolve_link("/tables/orders.md", "./customers.md") == \
            "/tables/customers.md"

    def test_relative_parent(self):
        assert core._resolve_link("/tables/orders.md", "../metrics/wau.md") == \
            "/metrics/wau.md"

    def test_anchor_stripped(self):
        assert core._resolve_link("/a.md", "/tables/x.md#schema") == "/tables/x.md"

    def test_external_is_none(self):
        assert core._resolve_link("/a.md", "https://example.com") is None
        assert core._resolve_link("/a.md", "mailto:x@y.z") is None

    def test_pure_anchor_is_none(self):
        assert core._resolve_link("/a.md", "#section") is None

    def test_concept_links_extracted(self):
        c = core.Concept(
            path="/tables/orders.md",
            frontmatter={"type": "Table"},
            body="Joined with [customers](/tables/customers.md) on id. "
                 "See [ext](https://x.com) and ![img](/pic.png).",
        )
        links = c.links()
        assert "/tables/customers.md" in links
        assert "https://x.com" not in links
        assert "/pic.png" not in links  # image excluded


# --------------------------------------------------------------------------
# Graph traversal
# --------------------------------------------------------------------------

def _concept(path, body="", **fm):
    fm.setdefault("type", "T")
    return core.Concept(path=path, frontmatter=fm, body=body)


class TestGraph:
    def _bundle(self):
        b = core.Bundle()
        b.concepts["/a.md"] = _concept("/a.md", "link [b](/b.md) and [c](/c.md)")
        b.concepts["/b.md"] = _concept("/b.md", "link [d](/d.md)")
        b.concepts["/c.md"] = _concept("/c.md", "no links")
        b.concepts["/d.md"] = _concept("/d.md", "link [missing](/gone.md)")
        return b

    def test_neighbors(self):
        b = self._bundle()
        assert set(b.neighbors("/a.md")) == {"/b.md", "/c.md"}

    def test_broken_link_reported_not_in_neighbors(self):
        b = self._bundle()
        assert b.neighbors("/d.md") == []
        assert b.broken_links("/d.md") == ["/gone.md"]

    def test_subgraph_depth_1(self):
        b = self._bundle()
        sub = b.subgraph(["/a.md"], depth=1)
        assert sub[0] == "/a.md"
        assert set(sub) == {"/a.md", "/b.md", "/c.md"}

    def test_subgraph_depth_2(self):
        b = self._bundle()
        sub = b.subgraph(["/a.md"], depth=2)
        assert set(sub) == {"/a.md", "/b.md", "/c.md", "/d.md"}

    def test_subgraph_depth_0_seeds_only(self):
        b = self._bundle()
        assert b.subgraph(["/a.md"], depth=0) == ["/a.md"]

    def test_subgraph_ignores_missing_seed(self):
        b = self._bundle()
        assert b.subgraph(["/nope.md"], depth=1) == []


# --------------------------------------------------------------------------
# Conformance validation
# --------------------------------------------------------------------------

class TestValidation:
    def test_missing_type_is_error(self):
        findings = core.validate_concept_text("/x.md", "---\ntitle: X\n---\nbody")
        assert any(f.rule == "type-required" and f.severity == "error"
                   for f in findings)

    def test_empty_type_is_error(self):
        findings = core.validate_concept_text("/x.md", "---\ntype: '  '\n---\nbody")
        assert any(f.rule == "type-required" for f in findings)

    def test_no_frontmatter_is_error(self):
        findings = core.validate_concept_text("/x.md", "# just a body")
        assert any(f.rule == "frontmatter-required" for f in findings)

    def test_valid_concept_no_findings(self):
        findings = core.validate_concept_text("/x.md", "---\ntype: Table\n---\nbody")
        assert findings == []

    def test_bundle_broken_link_is_warning_not_error(self):
        b = core.Bundle()
        b.concepts["/a.md"] = _concept("/a.md", "link [gone](/gone.md)")
        report = core.validate_bundle(b)
        assert report.conformant  # warnings don't break conformance
        assert report.to_dict()["warnings"] == 1

    def test_bundle_missing_type_breaks_conformance(self):
        b = core.Bundle()
        c = core.Concept(path="/a.md", frontmatter={"title": "X"}, body="")
        b.concepts["/a.md"] = c
        report = core.validate_bundle(b)
        assert not report.conformant
        assert report.to_dict()["errors"] == 1

    def test_is_reserved(self):
        assert core.is_reserved("/sales/index.md")
        assert core.is_reserved("/log.md")
        assert not core.is_reserved("/sales/orders.md")


# --------------------------------------------------------------------------
# index.md / log.md conventions
# --------------------------------------------------------------------------

class TestIndexAndLog:
    def test_render_index(self):
        out = core.render_index(
            [("/tables/orders.md", "One row per order."),
             ("/tables/customers.md", None)],
            heading="Tables",
        )
        assert "# Tables" in out
        assert "* [orders](/tables/orders.md) - One row per order." in out
        assert "* [customers](/tables/customers.md)" in out

    def test_append_log_fresh(self):
        out = core.append_log_entry(None, "2026-07-22", "Creation", "Added orders.")
        assert "# Update Log" in out
        assert "## 2026-07-22" in out
        assert "* **Creation**: Added orders." in out

    def test_append_log_new_date_newest_first(self):
        existing = "# Update Log\n\n## 2026-07-20\n* **Creation**: old.\n"
        out = core.append_log_entry(existing, "2026-07-22", "Update", "new.")
        # new date section must appear BEFORE the older one
        assert out.index("2026-07-22") < out.index("2026-07-20")

    def test_append_log_same_date_appends(self):
        existing = "# Update Log\n\n## 2026-07-22\n* **Creation**: first.\n"
        out = core.append_log_entry(existing, "2026-07-22", "Update", "second.")
        assert out.count("## 2026-07-22") == 1
        assert "first." in out and "second." in out
