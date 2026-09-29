"""Tests for near-miss path suggestions.

The two cases are taken from a real run: one where a suggestion saves a turn,
and one where it would have done damage.
"""
import pytest

from agent_system.utils.suggest import (
    differ_only_in_digits,
    siblings_of,
    suggest_path,
)


class TestTheRealSlip:
    """`reference/seitenformat.md` asked for, `references/seitenformat.md` held."""

    def test_singular_plural_typo_is_caught(self):
        files = ["SKILL.md", "references/checks.md", "references/seitenformat.md",
                 "scripts/verdachtsliste.py"]
        assert suggest_path("reference/seitenformat.md", files) == \
            "references/seitenformat.md"

    def test_case_slip_is_taken_as_is(self):
        assert suggest_path("References/Checks.md",
                            ["references/checks.md"]) == "references/checks.md"

    def test_a_genuinely_unrelated_name_gets_no_suggestion(self):
        """Better a plain not-found than a confident wrong answer."""
        assert suggest_path("völlig/anderes.md",
                            ["references/checks.md", "SKILL.md"]) is None

    def test_no_candidates_no_crash(self):
        assert suggest_path("x.md", []) is None
        assert suggest_path("", ["a.md"]) is None


class TestNumericSiblingsAreNotTypos:
    """kapitel_15.md missing, kapitel_16.md present — two different chapters.

    Suggesting the neighbour invites the agent to write the right content into
    the wrong document, silently and past every conformance check. That is worse
    than the not-found it would replace.
    """

    def test_neighbouring_chapter_is_not_offered(self):
        present = [f"/kapitel/kapitel_{i:02d}.md" for i in (11, 16, 17)]
        assert suggest_path("/kapitel/kapitel_15.md", present) is None

    @pytest.mark.parametrize("a,b", [
        ("kapitel_15.md", "kapitel_16.md"),
        ("kapitel_15.md", "kapitel_5.md"),
        ("/a/b_1.md", "/a/b_22.md"),
    ])
    def test_pairs_that_differ_only_in_digits(self, a, b):
        assert differ_only_in_digits(a, b) is True

    @pytest.mark.parametrize("a,b", [
        ("reference/x.md", "references/x.md"),   # the letters differ
        ("kapitel_15.md", "kapitel15.md"),       # a separator differs
        ("a1.md", "b1.md"),                      # the stem differs
        ("same.md", "same.md"),                  # identical is not "differing"
    ])
    def test_pairs_that_differ_elsewhere(self, a, b):
        assert differ_only_in_digits(a, b) is False

    def test_a_typo_is_still_caught_among_numeric_siblings(self):
        """The digit rule must not swallow a real typo that sits next to
        numbered files."""
        present = ["/kapitel/kapitel_16.md", "/kapitel/index.md"]
        assert suggest_path("/kapitel/idnex.md", present) == "/kapitel/index.md"


class TestSiblings:
    """When it is not a typo, the listing is the useful answer."""

    def test_lists_the_same_directory(self):
        known = ["/pfad.md", "/kapitel/kapitel_11.md", "/kapitel/kapitel_16.md",
                 "/figuren/a.md"]
        assert siblings_of("/kapitel/kapitel_15.md", known) == [
            "/kapitel/kapitel_11.md", "/kapitel/kapitel_16.md"]

    def test_falls_back_to_everything_when_the_directory_is_empty(self):
        known = ["/pfad.md", "/befunde.md"]
        assert siblings_of("/neu/ordner/x.md", known) == ["/befunde.md", "/pfad.md"]

    def test_is_bounded(self):
        known = [f"/k/{i}.md" for i in range(100)]
        assert len(siblings_of("/k/x.md", known, limit=5)) == 5
