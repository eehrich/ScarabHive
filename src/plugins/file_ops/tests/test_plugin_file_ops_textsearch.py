"""The index-free search: what it finds, and what it admits to skipping.

Two properties are pinned here, and both were broken in production:

1. **Glob patterns understand path separators.** ``fnmatch`` does not, and the
   consequence was measured on this repository: the same query under the same
   directory found 0 matches with ``src/agent_system/hooks/**/*.py`` and 61
   with ``src/agent_system/hooks/*.py``. The idiomatic pattern — the one an
   agent writes — was the one that failed, without a word.

2. **A skipped file is reported, not swallowed.** An empty result must be
   distinguishable from "it is there, behind a filter". No comparable agent
   tool reports this, which is why it is pinned rather than left to habit.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from plugins.file_ops import textsearch


# --- glob semantics ---------------------------------------------------------

class TestGlobUnderstandsPaths:
    """``**/`` means zero or more directories — the regression that hid files."""

    @pytest.mark.parametrize("rel,pattern,expected", [
        # The measured failure: a file directly in the named directory.
        ("hooks/registry.py", "hooks/**/*.py", True),
        ("hooks/deep/registry.py", "hooks/**/*.py", True),
        ("hooks/registry.py", "hooks/*.py", True),
        # A single star must NOT cross a separator, or "src/*.py" pulls in the
        # whole tree and the pattern stops meaning anything.
        ("src/deep/a.py", "src/*.py", False),
        ("src/a.py", "src/*.py", True),
        # No separator at all: match the file name at any depth.
        ("a/b/c/thing.py", "*.py", True),
        ("a/b/c/thing.txt", "*.py", False),
        # A leading **/ still matches at the top.
        ("thing.py", "**/*.py", True),
        ("a/thing.py", "**/*.py", True),
        # ? is one character and never a separator.
        ("ab.py", "?b.py", True),
        ("a/b.py", "?b.py", False),
    ])
    def test_pattern_matching(self, rel, pattern, expected):
        name = rel.rsplit("/", 1)[-1]
        assert textsearch.path_matches(rel, name, pattern) is expected

    def test_dot_is_literal(self):
        """A dot in the pattern must not act as the regex wildcard."""
        assert not textsearch.path_matches("axpy", "axpy", "a.py")
        assert textsearch.path_matches("a.py", "a.py", "a.py")


class TestRootHandling:
    def test_overlapping_roots_collapse(self, tmp_path):
        """The repository root swallows its own subdirectories.

        Four configured roots where the last contains the other three meant the
        same tree was walked four times per instance.
        """
        (tmp_path / "src" / "deep").mkdir(parents=True)
        roots = [tmp_path / "src" / "deep", tmp_path / "src", tmp_path]
        assert textsearch.deduplicate_roots(roots) == [tmp_path.resolve()]

    def test_unrelated_roots_are_kept(self, tmp_path):
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        kept = textsearch.deduplicate_roots([first, second])
        assert sorted(kept) == sorted([first.resolve(), second.resolve()])

    def test_pattern_repeating_the_root_is_stripped(self, tmp_path):
        """A model writes the root's own path into the pattern."""
        root = tmp_path / "data" / "workspace"
        root.mkdir(parents=True)
        assert textsearch.relativize_pattern("data/workspace/*.txt", root) == "*.txt"
        assert textsearch.relativize_pattern("*.txt", root) == "*.txt"

    def test_the_stripped_remainder_keeps_forward_slashes(self, tmp_path):
        """More than one part must survive as a GLOB, not as a Windows path.

        ``str(Path(*rest))`` produced "sub\\*.txt", where the backslash is a
        glob escape rather than a separator — measured: 0 files instead of 5.
        The single-part case cannot catch this, which is why it has its own
        test.
        """
        root = tmp_path / "data" / "workspace"
        root.mkdir(parents=True)
        assert textsearch.relativize_pattern(
            "data/workspace/godot/*.gd", root) == "godot/*.gd"
        assert "\\" not in textsearch.relativize_pattern(
            "data/workspace/a/b/*.gd", root)

    def test_root_order_is_deterministic(self, tmp_path):
        """Ties used to be broken by the per-process string hash seed.

        Pinned to an explicit order: two calls in ONE process share the seed,
        so comparing them with each other passes even with the bug back.
        """
        roots = [tmp_path / name for name in
                 ("echo", "alpha", "delta", "bravo", "charlie")]
        for root in roots:
            root.mkdir()
        expected = [tmp_path.resolve() / name for name in
                    ("alpha", "bravo", "charlie", "delta", "echo")]
        assert textsearch.deduplicate_roots(roots) == expected


# --- the walk ---------------------------------------------------------------

class TestWalkPrunes:
    def test_excluded_directory_is_never_entered(self, tmp_path, monkeypatch):
        """PRUNING, not filtering — and the difference has to be measured.

        A walker that descends everywhere and discards afterwards returns the
        same file list, so a result-only assertion cannot tell the two apart:
        it passes either way while the 150-second regression is back. What
        separates them is whether the directory was ever OPENED, so that is
        what is recorded here.
        """
        (tmp_path / "node_modules" / "deep").mkdir(parents=True)
        (tmp_path / "node_modules" / "deep" / "junk.py").write_text("x")
        (tmp_path / "keep.py").write_text("x")

        visited: list[str] = []
        real_scandir = textsearch.os.scandir

        def recording_scandir(path):
            visited.append(str(path))
            return real_scandir(path)

        monkeypatch.setattr(textsearch.os, "scandir", recording_scandir)
        found = [p.name for p in textsearch.walk_files(
            tmp_path, ["**/node_modules/**"])]

        assert "keep.py" in found
        assert "junk.py" not in found
        assert not any("node_modules" in entry for entry in visited), (
            f"excluded directory was entered: {visited}")

    def test_configured_roots_survive_an_inherited_ignore_rule(self, tmp_path):
        """An allowed root must not be hidden by an ancestor's .gitignore.

        The coder harness proved it: its roots collapse to the repository,
        whose .gitignore lists data/workspace/ — the agent's own sandbox.
        Measured before the guard: its files were unfindable by its own search.
        """
        workspace = tmp_path / "data" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "dodge.html").write_text("<html>")
        (tmp_path / ".gitignore").write_text("data/workspace/\n")

        spec = textsearch.gitignore_spec(tmp_path)
        assert spec is not None, "pathspec missing — this test measures nothing"

        without = [p.name for p in textsearch.walk_files(
            tmp_path, [], respect_ignore=True)]
        assert "dodge.html" not in without      # the rule works...

        with_protection = [p.name for p in textsearch.walk_files(
            tmp_path, [], respect_ignore=True, protected=[workspace])]
        assert "dodge.html" in with_protection  # ...but not over a real root

    def test_hidden_files_are_skipped_by_default(self, tmp_path):
        (tmp_path / ".secret").write_text("x")
        (tmp_path / "plain.txt").write_text("x")
        names = [p.name for p in textsearch.walk_files(tmp_path, [])]
        assert names == ["plain.txt"]


# --- what the answer admits -------------------------------------------------

class TestHonestAnswers:
    def test_empty_result_names_the_gitignore_filter(self, tmp_path):
        """"Nothing found" must not be confused with "nothing looked at".

        Pinned on the .gitignore filter SPECIFICALLY: asserting that any note
        mentions include_ignored passes even with gitignore support removed
        entirely, because the exclude patterns alone produce such a note.
        """
        (tmp_path / ".gitignore").write_text("secret/\n")
        (tmp_path / "secret").mkdir()
        (tmp_path / "secret" / "found_me.py").write_text("needle\n")

        result = textsearch.grep([tmp_path], "needle")
        assert result["total_matches"] == 0
        assert ".gitignore" in " ".join(result["skipped"]["filters_active"])

        # ...and the escape hatch the note advertises actually works.
        complete = textsearch.grep(
            [tmp_path], "needle", include_ignored=True)
        assert complete["total_matches"] == 1

    def test_a_configured_subroot_survives_while_the_rest_obeys_gitignore(
            self, tmp_path):
        """Both halves at once — the guard must be narrow, not an off switch.

        Measured when it was too wide: protecting the walked root itself made
        its own ignore rules cancel out, 131.616 files in 14.8 s instead of
        2.561 in 0.4 s. So the sandbox must stay visible AND the rest of the
        ignore file must keep working.
        """
        workspace = tmp_path / "data" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "sandbox.py").write_text("needle\n")
        junk = tmp_path / "logs"
        junk.mkdir()
        (junk / "noise.py").write_text("needle\n")
        (tmp_path / ".gitignore").write_text("data/workspace/\nlogs/\n")

        result = textsearch.find_files(
            [workspace, tmp_path], "*.py")
        names = {Path(f).name for f in result["files"]}
        assert "sandbox.py" in names, "a configured root must stay searchable"
        assert "noise.py" not in names, "the ignore file must still apply"

    def test_include_ignored_also_lifts_configured_excludes(self, tmp_path):
        """"Search everything" has to mean everything.

        Lifting only the .gitignore rules while the configured excludes kept
        filtering told a caller who had already passed include_ignored to pass
        it — the answer contradicting itself.
        """
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "found_me.py").write_text("x")

        hidden = textsearch.find_files(
            [tmp_path], "*.py", excludes=["**/node_modules/**"])
        assert hidden["total_found"] == 0

        complete = textsearch.find_files(
            [tmp_path], "*.py", excludes=["**/node_modules/**"],
            include_ignored=True)
        assert complete["total_found"] == 1

    def test_binary_files_are_reported_not_silently_dropped(self, tmp_path):
        (tmp_path / "code.py").write_text("needle\n")
        (tmp_path / "blob.bin").write_bytes(b"needle\x00" + b"\x00" * 300)

        result = textsearch.grep([tmp_path], "needle")
        hits = {Path(m["file_path"]).name for m in result["matches"]}
        assert hits == {"code.py"}
        # The NOTE must name the binary skip. Asserting that notes are merely
        # non-empty measures nothing: the filter line is always there.
        assert any("binary" in note.lower() for note in result["skipped"]["notes"])

    def test_truncation_is_stated(self, tmp_path):
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("needle\n")
        result = textsearch.grep(
            [tmp_path], "needle", max_results=2)
        assert result["total_matches"] == 2
        assert result["truncated"] is True
        assert any("truncated" in n.lower() for n in result["skipped"]["notes"])

    def test_exactly_max_results_is_not_truncated(self, tmp_path):
        """A complete answer must not ask the caller to narrow the pattern."""
        for i in range(3):
            (tmp_path / f"f{i}.txt").write_text("needle\n")
        result = textsearch.grep(
            [tmp_path], "needle", max_results=3)
        assert result["total_matches"] == 3
        assert result["truncated"] is False
        assert not any("truncated" in n.lower() for n in result["skipped"]["notes"])

    def test_an_exclude_does_not_match_the_roots_own_ancestors(self, tmp_path):
        """Excludes are matched RELATIVE to the search root.

        Matched against the absolute path too, ``**/build/**`` — which the
        coder harness configures — excluded every file of a checkout that
        merely lives under a directory called build.
        """
        root = tmp_path / "build" / "project"
        root.mkdir(parents=True)
        (root / "main.py").write_text("x")
        (root / "build").mkdir()
        (root / "build" / "artifact.py").write_text("x")

        result = textsearch.find_files([root], "*.py", excludes=["**/build/**"])
        names = {Path(f).name for f in result["files"]}
        assert names == {"main.py"}


class TestFindFiles:
    def test_double_star_finds_files_directly_in_the_directory(self, tmp_path):
        """The regression, end to end through find_files."""
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "registry.py").write_text("x")
        (tmp_path / "hooks" / "deep").mkdir()
        (tmp_path / "hooks" / "deep" / "other.py").write_text("x")

        result = textsearch.find_files(
            [tmp_path], "hooks/**/*.py")
        names = {Path(f).name for f in result["files"]}
        assert names == {"registry.py", "other.py"}


class TestWhatHeadFound:
    """Regressions against the indexed search, each reproduced before the fix."""

    def test_include_ignored_also_finds_hidden_files(self, tmp_path):
        """The note offers include_ignored for hidden files — it has to work."""
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text("x")

        assert textsearch.find_files([tmp_path], "ci.yml")["total_found"] == 0
        found = textsearch.find_files([tmp_path], "ci.yml", include_ignored=True)
        assert found["total_found"] == 1
        assert "hidden files" not in found["skipped"]["filters_active"]

    @pytest.mark.parametrize("pattern", ["readme.md", "*.MD", "DOCS/*.md"])
    def test_patterns_ignore_case(self, tmp_path, pattern):
        """An agent writes readme.md for README.md; a miss reads as absence."""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "README.md").write_text("x")
        assert textsearch.find_files([tmp_path], pattern)["total_found"] == 1

    @pytest.mark.parametrize("pattern", ["./src/**/*.py", r"src\a\*.py"])
    def test_shell_and_windows_spellings_match(self, tmp_path, pattern):
        (tmp_path / "src" / "a").mkdir(parents=True)
        (tmp_path / "src" / "a" / "deep.py").write_text("x")
        assert textsearch.find_files([tmp_path], pattern)["total_found"] == 1

    def test_symlinked_file_inside_the_root_is_found_one_outside_is_not(
            self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "target.md").write_text("x")
        outside = tmp_path / "secret.md"
        outside.write_text("x")
        try:
            os.symlink(root / "target.md", root / "link.md")
            os.symlink(outside, root / "escape.md")
        except OSError:
            pytest.skip("symlinks not permitted here — this test measures nothing")

        names = {Path(f).name for f in
                 textsearch.find_files([root], "*.md")["files"]}
        assert names == {"target.md", "link.md"}

    @pytest.mark.parametrize("sub,excludes", [
        (".claude/skills", []),              # below a hidden directory
        ("build/proj", ["**/build/**"]),     # below an excluded directory
    ])
    def test_a_configured_subroot_survives_every_filter_of_its_ancestor(
            self, tmp_path, sub, excludes):
        subroot = tmp_path / sub
        subroot.mkdir(parents=True)
        (subroot / "keep.md").write_text("x")
        (subroot.parent / "other.md").write_text("x")

        result = textsearch.find_files(
            [tmp_path, subroot], "*.md", excludes=excludes)
        names = {Path(f).name for f in result["files"]}
        assert names == {"keep.md"}, "sub-root visible, its pruned parent not"

    def test_line_numbers_match_read_file_across_a_form_feed(self, tmp_path):
        """splitlines() also breaks on a form feed: grep said line 4 for line 3."""
        (tmp_path / "a.py").write_text("one\ntwo\fstill two\nneedle\n")
        match = textsearch.grep([tmp_path], "needle")["matches"][0]
        assert match["line_number"] == 3


class TestReviewRoundThree:
    def test_gitignore_still_applies_inside_a_subroot_it_does_not_hide(
            self, tmp_path):
        """Only a HIDDEN sub-root becomes its own base; src/ is not hidden."""
        src = tmp_path / "src"
        (src / "logs").mkdir(parents=True)
        (src / "code.py").write_text("x")
        (src / "logs" / "run.log").write_text("x")
        (tmp_path / ".gitignore").write_text("logs/\n")

        names = {Path(f).name for f in
                 textsearch.find_files([tmp_path, src], "*")["files"]}
        assert "code.py" in names
        assert "run.log" not in names

    def test_a_symlink_into_another_allowed_root_is_found(self, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        second.mkdir()
        (second / "real.md").write_text("x")
        try:
            os.symlink(second / "real.md", first / "link.md")
        except OSError:
            pytest.skip("symlinks not permitted here — this test measures nothing")
        names = {Path(f).name for f in
                 textsearch.find_files([first, second], "*.md")["files"]}
        assert names == {"real.md", "link.md"}

    def test_a_harmless_name_linked_to_a_secret_is_not_found(self, tmp_path, monkeypatch):
        """The private-name check read the LINK's name: notes.md -> secrets/local.env
        passed, and grep and the semantic index read the secret."""
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "local.env").write_text("API_KEY=x")
        root = tmp_path / "proj"
        root.mkdir()
        try:
            os.symlink(secrets / "local.env", root / "notes.md")
        except OSError:
            pytest.skip("symlinks not permitted here — this test measures nothing")
        monkeypatch.setattr(textsearch, "is_secret",
                            lambda path: Path(path).resolve().parent == secrets.resolve())
        names = {Path(f).name for f in
                 textsearch.find_files([root, secrets], "*.md")["files"]}
        assert names == set()

    def test_excludes_respect_case(self, tmp_path):
        """**/build/** must not take a Unity/Unreal Build/ source tree."""
        (tmp_path / "Build").mkdir()
        (tmp_path / "Build" / "game.cs").write_text("x")
        result = textsearch.find_files(
            [tmp_path], "*.cs", excludes=["**/build/**"])
        assert result["total_found"] == 1

    @pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
    def test_a_junction_loop_is_not_followed(self, tmp_path):
        import subprocess
        (tmp_path / "k.txt").write_text("x")
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(tmp_path / "loop"), str(tmp_path)],
            capture_output=True)
        if made.returncode != 0:
            pytest.skip("could not create a junction — this test measures nothing")
        assert textsearch.find_files([tmp_path], "k.txt")["total_found"] == 1

    def test_include_ignored_still_prunes_git(self, tmp_path, monkeypatch):
        (tmp_path / ".git" / "objects").mkdir(parents=True)
        (tmp_path / ".git" / "objects" / "pack.txt").write_text("needle")
        (tmp_path / "a.txt").write_text("needle")
        result = textsearch.grep([tmp_path], "needle", include_ignored=True)
        assert {Path(m["file_path"]).name for m in result["matches"]} == {"a.txt"}


# --- 12.11.2026: ripgrep in front, the walker behind it ---------------------
#
# Every test of this module runs once per engine (see conftest). The ones
# below pin what made the two answer differently while they were built.

class TestBothEnginesAnswerAlike:
    def test_a_nested_gitignore_applies_below_its_directory(self, tmp_path, search_engine):
        """Only the root's ignore file used to count: a checkout below the root
        brought its build output and its own ignored data into every search."""
        checkout = tmp_path / "work" / "checkout"
        (checkout / "out").mkdir(parents=True)
        (checkout / ".gitignore").write_text("out/\n*.log\n")
        (checkout / "out" / "built.js").write_text("needle")
        (checkout / "run.log").write_text("needle")
        (checkout / "code.py").write_text("needle")
        (tmp_path / "top.log").write_text("needle")   # the rule is the checkout's, not the root's

        hits = {Path(m["file_path"]).name for m in textsearch.grep([tmp_path], "needle")["matches"]}

        assert hits == {"code.py", "top.log"}

    def test_the_deepest_ignore_file_decides(self, tmp_path, search_engine):
        """git's rule: a ``!keep.log`` below lets through what ``*.log`` above hides."""
        (tmp_path / "sub").mkdir()
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "sub" / ".gitignore").write_text("!keep.log\n")
        (tmp_path / "sub" / "keep.log").write_text("needle")
        (tmp_path / "sub" / "drop.log").write_text("needle")

        found = {Path(f).name for f in textsearch.find_files([tmp_path], "*.log")["files"]}

        assert found == {"keep.log"}

    def test_an_include_pattern_does_not_bring_ignored_files_back(self, tmp_path, search_engine):
        """Measured on ripgrep: a positive --glob is a whitelist that overrides
        the ignore files and the hidden-file rule, so "*" found a gitignored log
        and .gitignore itself."""
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "run.log").write_text("needle")
        (tmp_path / "code.py").write_text("needle")

        grep_hits = {Path(m["file_path"]).name for m in
                     textsearch.grep([tmp_path], "needle", include_pattern="*")["matches"]}
        files = {Path(f).name for f in textsearch.find_files([tmp_path], "*")["files"]}

        assert grep_hits == {"code.py"}
        assert files == {"code.py"}

    def test_braces_and_negated_classes_are_globs(self, tmp_path, search_engine):
        """``*.{ts,tsx}`` -- opencode's own example -- matched nothing at all."""
        for name in ("a.ts", "b.tsx", "c.js", "data.txt", "note.txt"):
            (tmp_path / name).write_text("x")

        braces = {Path(f).name for f in textsearch.find_files([tmp_path], "*.{ts,tsx}")["files"]}
        negated = {Path(f).name for f in textsearch.find_files([tmp_path], "[!d]*.txt")["files"]}

        assert braces == {"a.ts", "b.tsx"}
        assert negated == {"note.txt"}

    def test_a_pattern_only_python_reads_is_still_answered(self, tmp_path, search_engine):
        """ripgrep's regex dialect has no look-behind. It answered with an error
        once, and a search that errors must not read as "0 matches"."""
        (tmp_path / "a.py").write_text("price = 5\ncost = 5\n")

        result = textsearch.grep([tmp_path], r"(?<=price = )5", is_regex=True)

        assert [m["line_number"] for m in result["matches"]] == [1]
        assert result["engine"] == "python"

    def test_hits_come_sorted_by_file_and_line(self, tmp_path, search_engine):
        """ripgrep searches in parallel and hands files over in any order, and
        the walker yields a directory's files before its subdirectories, the
        last of those first. Neither order is the path order."""
        for rel in ("z.txt", "a/n.txt", "b/n.txt", "c/n.txt"):
            (tmp_path / rel).parent.mkdir(exist_ok=True)
            (tmp_path / rel).write_text("needle\nx\nneedle\n")

        result = textsearch.grep([tmp_path], "needle")

        assert [(Path(m["file_path"]).relative_to(tmp_path).as_posix(), m["line_number"])
                for m in result["matches"]] == [
            ("a/n.txt", 1), ("a/n.txt", 3), ("b/n.txt", 1), ("b/n.txt", 3),
            ("c/n.txt", 1), ("c/n.txt", 3), ("z.txt", 1), ("z.txt", 3)]

    def test_context_is_the_lines_around_each_hit(self, tmp_path, search_engine):
        (tmp_path / "a.txt").write_text("one\ntwo\nneedle\nfour\nfive\nneedle\n")

        result = textsearch.grep([tmp_path], "needle", context_lines=1)

        assert [(m["context_before"], m["context_after"]) for m in result["matches"]] == [
            (["two"], ["four"]), (["five"], [])]

    def test_a_minified_line_is_cut(self, tmp_path, search_engine):
        """One hit on a one-megabyte line used to be that megabyte in the context."""
        (tmp_path / "bundle.js").write_text("x" * 50_000 + "needle" + "y" * 50_000 + "\n")

        line = textsearch.grep([tmp_path], "needle")["matches"][0]["line_content"]

        assert len(line) <= textsearch.MAX_LINE_CHARS + 20
        assert line.endswith("[line cut]")

    def test_a_line_that_is_not_utf8_is_decoded_like_read_text(self, tmp_path, search_engine):
        (tmp_path / "latin.txt").write_bytes(b"needle caf\xe9 here\n")

        line = textsearch.grep([tmp_path], "needle")["matches"][0]["line_content"]

        assert line == "needle caf here"

    def test_a_secret_beside_the_code_is_never_a_hit(self, tmp_path, search_engine, monkeypatch):
        secret = tmp_path / "local.env"
        secret.write_text("needle=1")
        (tmp_path / "code.py").write_text("needle")
        monkeypatch.setattr(textsearch, "is_secret", lambda path: Path(path).name == "local.env")

        hits = {Path(m["file_path"]).name for m in textsearch.grep([tmp_path], "needle")["matches"]}

        assert hits == {"code.py"}


class TestReviewRoundOfTheEngines:
    """12.11.2026: what the review of the two engines found, each probed on both."""

    def test_a_line_end_anchor_matches_a_crlf_file(self, tmp_path, search_engine):
        """Without --crlf ripgrep saw the \\r, and ``pass$`` found nothing in the
        176 CRLF files of src/."""
        (tmp_path / "win.py").write_bytes(b"def f():\r\n    pass\r\n")

        result = textsearch.grep([tmp_path], r"pass$", is_regex=True)

        assert [m["line_number"] for m in result["matches"]] == [2]

    def test_a_nul_late_in_a_text_file_keeps_the_line_numbers(self, tmp_path, search_engine):
        """--binary turned a NUL into a line break and moved every later line."""
        body = b"x" * 5000 + b"\n" + b"a\x00b\n" + b"c\n" + b"needle\n"
        (tmp_path / "late.txt").write_bytes(body)

        result = textsearch.grep([tmp_path], "needle")

        assert [m["line_number"] for m in result["matches"]] == [4]

    def test_a_bom_is_not_part_of_the_first_line(self, tmp_path, search_engine):
        (tmp_path / "bom.txt").write_bytes(b"\xef\xbb\xbfneedle\n")

        result = textsearch.grep([tmp_path], r"^needle", is_regex=True)

        assert [m["line_content"] for m in result["matches"]] == ["needle"]

    def test_syntax_the_dialects_read_differently_follows_python(self, tmp_path, search_engine):
        r"""``\<`` is a word start to ripgrep and a literal "<" to Python: ``\<-``
        is an arrow to Python and can never match in ripgrep, since "-" starts
        no word."""
        (tmp_path / "a.r").write_text("x <- 1\ny = 2\n")

        result = textsearch.grep([tmp_path], r"\<-", is_regex=True)

        assert [m["line_number"] for m in result["matches"]] == [1]

    def test_ripgrep_never_reports_a_line_python_would_not_match(self, tmp_path, search_engine):
        """ripgrep's --ignore-case folds the long s onto s; Python's lower()
        keeps it. Every line ripgrep reports is held against Python's reading."""
        (tmp_path / "a.txt").write_text("the sun\nthe ſun\n")

        result = textsearch.grep([tmp_path], "sun")

        assert [m["line_number"] for m in result["matches"]] == [1]

    def test_a_configured_root_that_is_not_there_does_not_slow_the_rest(self, tmp_path, search_engine):
        """A missing sub-root failed ripgrep's cwd, and the whole search fell back."""
        (tmp_path / ".gitignore").write_text("data/\n")
        (tmp_path / "code.py").write_text("needle")
        missing = tmp_path / "data" / "workspace"

        result = textsearch.grep([tmp_path, missing], "needle")

        assert [Path(m["file_path"]).name for m in result["matches"]] == ["code.py"]
        assert result["engine"] == search_engine

    def test_a_private_folder_is_excluded_only_where_it_is(self, tmp_path, search_engine, monkeypatch):
        """The exclude glob for a private folder was not anchored: with the scope
        its parent, every folder of that name at any depth went with it."""
        data = tmp_path
        (data / "sessions").mkdir()
        (data / "sessions" / "s.json").write_text("needle")
        (data / "app" / "Sessions").mkdir(parents=True)
        (data / "app" / "Sessions" / "handler.py").write_text("needle")
        monkeypatch.setattr(textsearch, "private_places",
                            lambda: (tmp_path / "config", tmp_path / "users.db",
                                     (data / "sessions",)))
        private = data / "sessions"
        monkeypatch.setattr(textsearch, "is_secret",
                            lambda path: Path(path) == private or private in Path(path).parents)

        hits = {Path(m["file_path"]).name for m in textsearch.grep([data], "needle")["matches"]}

        assert hits == {"handler.py"}

    def test_an_ignore_file_outranks_a_gitignore_below_it(self, tmp_path, search_engine):
        """ripgrep's precedence: every .ignore before every .gitignore."""
        (tmp_path / "sub").mkdir()
        (tmp_path / ".ignore").write_text("*.log\n")
        (tmp_path / "sub" / ".gitignore").write_text("!keep.log\n")
        (tmp_path / "sub" / "keep.log").write_text("needle")

        assert textsearch.find_files([tmp_path], "*.log")["files"] == []

    @pytest.mark.skipif(os.name != "nt", reason="the hidden attribute is Windows'")
    def test_the_windows_hidden_attribute_hides(self, tmp_path, search_engine):
        import subprocess

        (tmp_path / "secret_dir").mkdir()
        (tmp_path / "secret_dir" / "a.txt").write_text("needle")
        (tmp_path / "plain.txt").write_text("needle")
        subprocess.run(["attrib", "+h", str(tmp_path / "secret_dir")], check=True)

        hits = {Path(m["file_path"]).name for m in textsearch.grep([tmp_path], "needle")["matches"]}

        assert hits == {"plain.txt"}

    def test_files_searched_counts_what_the_include_names(self, tmp_path, search_engine):
        """ripgrep reads the tree, and "searched 21 files" was the whole tree for
        an include that named one."""
        for index in range(20):
            (tmp_path / f"other_{index}.txt").write_text("x")
        (tmp_path / "chapter.md").write_text("needle")

        result = textsearch.grep([tmp_path], "needle", include_pattern="*.md")

        assert result["total_matches"] == 1 and result["total_files"] == 1

    def test_the_answer_names_ignore_files_wherever_they_were(self, tmp_path, search_engine):
        """Only a root-level ignore file counted, while nested ones apply too."""
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / ".gitignore").write_text("*.log\n")
        (tmp_path / "sub" / "a.log").write_text("needle")

        result = textsearch.grep([tmp_path], "needle")

        assert result["total_matches"] == 0
        assert ".gitignore" in " ".join(result["skipped"]["filters_active"])


class TestTheFixRoundAgainstItself:
    """12.11.2026: what the review of the review's fixes found."""

    def test_the_size_ceiling_holds_for_an_include_pattern(self, tmp_path, search_engine):
        """ripgrep applies --max-filesize to what it walks, not to a file it is
        given by name -- and an include pattern names its files."""
        (tmp_path / "big.log").write_text("needle\n" + "x" * 5000)
        (tmp_path / "small.log").write_text("needle\n")

        result = textsearch.grep([tmp_path], "needle", include_pattern="*.log",
                                 max_filesize_bytes=100)

        assert [Path(m["file_path"]).name for m in result["matches"]] == ["small.log"]
        assert any("too large" in note for note in result["skipped"]["notes"])

    def test_an_include_search_survives_a_missing_first_root(self, tmp_path, search_engine):
        """The shallowest root came first and was the cwd of every run: missing,
        it sent every include search to the walker."""
        real = tmp_path / "deep" / "real"
        real.mkdir(parents=True)
        (real / "a.md").write_text("needle")

        result = textsearch.grep([tmp_path / "gone", real], "needle", include_pattern="*.md")

        assert [Path(m["file_path"]).name for m in result["matches"]] == ["a.md"]
        assert result["engine"] == search_engine

    @pytest.mark.parametrize("query", [r"[_[:alnum:]]", r"[a-z--[b]]", r"[a-z&&[^aeiou]]"])
    def test_a_class_ripgrep_reads_as_a_set_follows_python(self, tmp_path, search_engine, query):
        """Nested classes and set operations are plain characters to Python:
        ripgrep read them narrower and missed lines without a word."""
        import re
        import warnings

        (tmp_path / "a.txt").write_text("a]\n:]\n&]\nb]\n")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)   # Python's "possible nested set"
            expected = [index + 1 for index, line in enumerate(["a]", ":]", "&]", "b]"])
                        if re.search(query, line, re.IGNORECASE)]
            result = textsearch.grep([tmp_path], query, is_regex=True)

        assert expected, "Python matches nothing here -- this proves nothing"
        assert [m["line_number"] for m in result["matches"]] == expected

    def test_a_ripgrep_that_changed_after_the_first_search_is_not_run(
            self, tmp_path, search_engine, monkeypatch):
        from plugins.file_ops import ripgrep

        (tmp_path / "a.txt").write_text("needle")
        monkeypatch.setattr(ripgrep, "still_trusted", lambda binary: False)

        result = textsearch.grep([tmp_path], "needle")
        files = textsearch.find_files([tmp_path], "*.txt")

        assert result["engine"] == "python" and files["engine"] == "python"
        assert result["total_matches"] == 1

    @pytest.mark.skipif(os.name != "nt", reason="the hidden attribute is Windows'")
    def test_a_flat_listing_hides_what_the_attribute_hides(self, tmp_path, search_engine):
        import subprocess

        (tmp_path / "hidden.txt").write_text("x")
        (tmp_path / "plain.txt").write_text("x")
        subprocess.run(["attrib", "+h", str(tmp_path / "hidden.txt")], check=True)

        listing = textsearch.list_directory(tmp_path)

        assert [Path(f).name for f in listing["files"]] == ["plain.txt"]
