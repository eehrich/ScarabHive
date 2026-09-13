"""Index-free file and text search: one pruning walk, no second backend.

WHY THERE IS NO INDEX HERE
==========================
The previous implementation built an in-memory filename+word index and every
search AWAITED it. Measured on this repo with the coder harness config (four
allowed roots, the last of them the repository itself): one
``search_files("**/agent_continuation/**")`` had not returned after 150
seconds. The same glob without the index takes under a second. The index was
not slow because it was badly written — it was reading 152.664 files / 127 GB
before answering a question about six paths.

Two properties of the old design made it worse than its size suggests:

* exclusions were applied to the RESULT of the walk, so an excluded directory
  was still descended into. Pruning has to happen before the descent or it
  saves nothing.
* overlapping roots were walked once each. ``.`` contains ``src/``, so the same
  tree was indexed several times per instance.

Both are fixed here: roots are deduplicated, and the walker prunes directories
on the way down. The index itself stays in ``search.py``, used by semantic
search alone — that one genuinely needs a corpus.

WHY NOT RIPGREP
===============
It was built and measured, and removed. On this repository the walker answers a
glob and a grep in about 0.8 s, so the speed a second backend buys is not where
the cost is. What it does buy is a second set of answers: globs matched relative
to the process directory instead of the search root, binary files reported as
matches, non-UTF-8 lines arriving empty, and a regex dialect that rejects what
Python accepts — which surfaced as "success, 0 matches". One implementation
answers the same everywhere.

The functions here are synchronous and walk the disk. Callers in an event loop
must run them in a worker thread: with ``include_ignored`` a walk over this
repository takes around 15 s, and a blocked loop stalls every other tool call
of the server for that long.

WHAT THIS REPORTS THAT OTHERS DO NOT
====================================
Every comparable agent tool reports truncation, and none of them reports why a
file was never looked at. That is the difference between "there is no such
code" and "the code is there, behind an ignore rule" — and the model cannot
tell them apart from an empty result. So every answer here carries ``skipped``:
which filters were active, and whether anything was cut off. When a search
finds nothing while filters were on, that is stated in the answer instead of
being left to look like absence.
"""

from __future__ import annotations

import functools
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Never worth descending into for a code search. Pruned by the walker.
DEFAULT_EXCLUDES: Tuple[str, ...] = (
    "**/.git/**",
    "**/__pycache__/**",
    "**/node_modules/**",
    "**/.venv/**",
    "**/*.pyc",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
)

#: Pruned even under include_ignored.
ALWAYS_EXCLUDED: Tuple[str, ...] = ("**/.git/**",)

#: Largest file grep opens when the caller sets no ceiling of its own; keeps
#: the search from paging in a database.
_DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024


def deduplicate_roots(roots: Sequence[Path]) -> List[Path]:
    """Drop every root that another root already contains.

    ``[., src, src/plugins/coder/skills, data/workspace]`` becomes ``[.]``.
    Without this the same tree is searched once per entry, which is where a
    fourfold walk of the repository came from.
    """
    resolved = []
    for root in roots:
        try:
            resolved.append(Path(root).resolve())
        except OSError:
            continue
    kept: List[Path] = []
    # Deterministic: ties used to be broken by set iteration order, which
    # depends on the per-process string hash seed. Measured — five processes,
    # five orders — and with a truncated result that means the same query
    # answers differently after a restart.
    for root in sorted(set(resolved), key=lambda p: (len(p.parts), str(p))):
        if any(root == other or other in root.parents for other in kept):
            continue
        kept.append(root)
    return kept


def sub_roots(root: Path, configured: Sequence[Path]) -> List[Path]:
    """The configured roots that lie strictly BELOW *root*.

    A filter of *root* may not hide them — see :func:`walk_files`. The walking
    root itself is deliberately left out: protecting it makes its own ignore
    rules cancel themselves — measured: the repository root walked 131.616
    files in 14.8 s instead of 2.561 in 0.4 s, and .gitignore was effectively
    off.
    """
    return [entry for entry in configured
            if entry != root and root in entry.parents]


@dataclass
class SearchReport:
    """What was filtered away, so an empty answer can be read correctly."""

    respected_ignore_files: bool = False
    excluded_patterns: List[str] = field(default_factory=list)
    hidden_files_skipped: bool = False
    truncated: bool = False
    size_skipped: int = 0
    binary_skipped: int = 0

    def as_dict(self, found: int) -> Dict[str, Any]:
        notes: List[str] = []
        if self.truncated:
            notes.append(
                "Result truncated at the requested maximum — narrow the "
                "pattern or raise max_results to see the rest.")
        filters = []
        if self.respected_ignore_files:
            filters.append(".gitignore and .ignore files")
        if self.excluded_patterns:
            filters.append(f"{len(self.excluded_patterns)} configured exclude patterns")
        if self.hidden_files_skipped:
            filters.append("hidden files")
        if filters and found == 0:
            # The one case where silence is misread as absence.
            notes.append(
                "Nothing matched WHILE these filters were active: "
                + ", ".join(filters)
                + ". Pass include_ignored=true to search them as well before "
                  "concluding the code does not exist — it walks everything, "
                  "which takes minutes on a large tree, so keep the pattern "
                  "(or include_pattern) narrow.")
        elif filters:
            notes.append("Filters active: " + ", ".join(filters)
                         + " (include_ignored=true searches them too).")
        if self.size_skipped:
            notes.append(
                f"{self.size_skipped} file(s) were too large to search and "
                f"were skipped — raise max_file_size_for_indexing_kb to "
                f"include them.")
        if self.binary_skipped:
            notes.append(
                f"{self.binary_skipped} binary file(s) were skipped (decided "
                f"by content, not by extension).")
        return {
            "filters_active": filters,
            "notes": notes,
        }


def relativize_pattern(pattern: str, root: Path) -> str:
    """Strip a leading path that merely repeats the root's own tail.

    A model writes ``data/workspace/*.txt`` while the allowed root already IS
    ``.../data/workspace``. Without this the search looks for
    ``data/workspace/data/workspace/*.txt`` and finds nothing — a pinned
    regression in ``test_plugin_file_ops_search.py``.

    Backslashes and a leading ``./`` are normalised first. Both are how a
    path is written on Windows or in a shell, and as a glob both matched
    nothing: ``./`` was taken literally, and a backslash pattern has no ``/``
    and fell back to matching the file name.
    """
    pattern = pattern.replace("\\", "/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    parts = Path(pattern).parts
    base_parts = root.parts
    if not parts or not base_parts:
        return pattern
    matching = 0
    for i in range(len(base_parts)):
        suffix = base_parts[-(i + 1):]
        if len(parts) > i and parts[:i + 1] == suffix:
            matching = i + 1
    if matching:
        rest = parts[matching:]
        # Forward slashes, never the platform separator: the result is matched
        # as a GLOB, and on Windows ``str(Path(...))`` produced "sub\\*.txt",
        # where the backslash is an escape rather than a separator. Measured:
        # "data/workspace/godot/*" found 0 files instead of 5.
        return "/".join(rest) if rest else "*"
    return pattern


@functools.lru_cache(maxsize=512)
def compile_glob(pattern: str, ignore_case: bool = True) -> "re.Pattern[str]":
    """Translate a glob into a regex that understands PATH SEPARATORS.

    ``fnmatch`` does not: its ``*`` matches slashes too, so ``a/**/b.py``
    silently requires an intermediate directory and ``a/*.py`` happily matches
    ``a/deep/b.py``. Measured on this repository, same query, same directory:
    ``src/agent_system/hooks/**/*.py`` found 0 matches while
    ``src/agent_system/hooks/*.py`` found 61 in 6 files. The idiomatic pattern
    — the one an agent writes — was the one that failed, and it failed
    silently.

    The rules follow git:

        ``**/``   zero OR MORE directories (this is the one fnmatch gets wrong)
        ``**``    anything, separators included
        ``*``     anything within one path segment
        ``?``     exactly one character, never a separator

    Case-insensitive by default, as the filename index before it was: an
    agent writes ``readme.md`` for ``README.md``, and a miss there reads as
    "the file does not exist". Exclude globs pass ``ignore_case=False``: there
    a loose match HIDES files, and ``**/build/**`` must not take a Unity or
    Unreal ``Build/`` source tree with it.
    """
    out: List[str] = []
    index, length = 0, len(pattern)
    while index < length:
        if pattern.startswith("**/", index):
            out.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        elif pattern[index] == "[":
            close = pattern.find("]", index)
            if close == -1:
                out.append(re.escape(pattern[index]))
                index += 1
            else:
                out.append(pattern[index:close + 1])
                index = close + 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE if ignore_case else 0)


def path_matches(rel: str, name: str, pattern: str,
                 ignore_case: bool = True) -> bool:
    """Whether a file matches the pattern.

    A pattern without a separator matches the FILE NAME at any depth — that is
    what makes ``*.py`` useful. A pattern with a separator is matched against
    the path relative to the search root, and once more with an implicit
    ``**/`` in front, so ``hooks/*.py`` still works from a root above it.
    """
    if "/" not in pattern:
        return compile_glob(pattern, ignore_case).match(name) is not None
    if compile_glob(pattern, ignore_case).match(rel) is not None:
        return True
    return compile_glob("**/" + pattern, ignore_case).match(rel) is not None


def _is_excluded(rel: str, name: str, excludes: Sequence[str]) -> bool:
    """Whether a path, RELATIVE to its search root, matches an exclude glob.

    Relative only. Matching the absolute path as well made the root's own
    ancestors count: a repository checked out under ``/srv/build/`` lost every
    file to the ``**/build/**`` exclude the coder harness configures.
    """
    for pattern in excludes:
        if path_matches(rel, name, pattern, ignore_case=False):
            return True
        # "**/x/**" also excludes the directory x itself — which is what lets
        # the walker prune it instead of opening it.
        if pattern.endswith("/**") and path_matches(
                rel, name, pattern[:-3], ignore_case=False):
            return True
    return False


#: Bytes read to decide text-vs-binary. Same sniff the indexer uses.
SNIFF_BYTES = 4096


def is_text_file(path: Path) -> bool:
    """Whether the file carries searchable text — decided by CONTENT.

    Shared with the indexer on purpose: two readings of "is this text" drift,
    and the drift is invisible. The rule has history — an allow-list of "text"
    extensions once hid .asm, .s and .inc files, and the only symptom was a
    search reporting "0 matches in 0 files", which reads like absence rather
    than like a file nobody looked at.

    No list can be complete, so there is none: a NUL byte in the first few KB
    is the classic binary marker, and everything else is searchable.
    """
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(SNIFF_BYTES)
    except OSError:
        return False  # unreadable — nothing to search
    if not chunk:
        return False  # empty file carries no searchable text
    if b"\x00" in chunk:
        return False  # NUL byte: binary
    # Control characters outside tab/newline/CR/formfeed/escape. Text in any
    # encoding stays far below the threshold; binaries blow past it.
    control = sum(1 for byte in chunk if byte < 0x09 or 0x0e <= byte < 0x20)
    return control / len(chunk) <= 0.10


def gitignore_spec(root: Path) -> Optional[Any]:
    """The root's own ignore rules as a matcher, or None when unavailable.

    ``pathspec`` implements git's wildmatch semantics, which is why the rules
    are not re-implemented here — a hand-rolled approximation of ``.gitignore``
    is how a search quietly stops matching what the user sees in git.

    It is present only as a TRANSITIVE dependency, so its absence is a
    documented downgrade rather than an error: the walk then prunes by the
    configured patterns alone, and the answer says which filters were actually
    applied. On this repository the difference is not cosmetic —
    ``data/sessions/`` and ``data/message_debugger/`` are gitignored and hold
    130.935 of the 152.664 files.
    """
    try:
        import pathspec  # noqa: PLC0415 - optional, resolved per root
    except ImportError:
        return None
    lines: List[str] = []
    for name in (".gitignore", ".ignore"):
        candidate = root / name
        try:
            if candidate.is_file():
                lines.extend(candidate.read_text(
                    encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            continue
    if not lines:
        return None
    # The factory name changed across pathspec versions, and the mismatch is
    # not a warning but an exception: 1.x REFUSES "gitwildmatch" and wants
    # "gitignore", while 0.x only knows the old name. Measured with the
    # installed pathspec 1.0.1 — the old name raised, the exception was
    # swallowed at debug level, and ignore support was silently off. Every
    # claim about .gitignore pruning was false while it lasted; only
    # ``respected_ignore_files`` told the truth.
    for factory in ("gitignore", "gitwildmatch"):
        try:
            return pathspec.PathSpec.from_lines(factory, lines)
        except Exception:  # a malformed ignore file must not kill the search
            continue
    logger.warning(
        "FILE_OPS: could not build ignore rules under %s — ignore files are "
        "NOT being honoured (searches stay correct, but slower and wider)",
        root)
    return None


def walk_files(
    root: Path,
    excludes: Sequence[str],
    *,
    include_hidden: bool = False,
    ignore_spec: Any = None,
    protected: Sequence[Path] = (),
    allowed: Sequence[Path] = (),
) -> Iterator[Path]:
    """Yield files under *root*, PRUNING excluded directories on the way down.

    The pruning is the point: the old walker pushed every directory onto its
    stack and filtered the files afterwards, so an excluded tree still cost a
    full traversal. A directory that is ignored is never entered, which is the
    only thing that makes a walk over a large tree affordable at all.

    ``protected`` are configured roots below *root* (:func:`sub_roots`). No
    filter of *root* may hide them — not its .gitignore, not the hidden-name
    rule, not an exclude glob — because the configuration named them as
    searchable. The coder harness proved it: its roots collapse to the
    repository, whose .gitignore lists ``data/workspace/``, the agent's own
    sandbox; measured before the guard, ``dodge.html`` in the workspace was
    unfindable. So when a filter would hide a protected root, it is walked as
    a root of its own: its contents are judged relative to IT, and the
    ancestor's ignore file does not apply inside it. A protected root that no
    filter hides is walked like any directory, under *root*'s rules — treating
    every one as its own base switched .gitignore off below ``src/`` (135
    ignored files, logs and a database among them).

    Symlinked FILES are yielded when their target lies inside *root* or one of
    the *allowed* roots — the target is what gets read, so it has to lie within
    the sandbox. Symlinked directories and Windows junctions are never
    entered: a junction reports as a plain directory, and one pointing at its
    parent produced the same file 64 times.
    """
    protected = set(protected)
    # (directory, the root its entries are judged against)
    stack = [(root, root)]
    while stack:
        current, base = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    name = entry.name
                    path = Path(entry.path)
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        # is_junction exists from Python 3.12; junctions only
                        # exist on Windows.
                        junction = getattr(entry, "is_junction", None)
                        if is_dir and junction is not None and junction():
                            continue
                        rel = path.relative_to(base).as_posix()
                        if ((not include_hidden and name.startswith("."))
                                or _is_excluded(rel, name, excludes)
                                or (ignore_spec is not None and base == root
                                    and ignore_spec.match_file(
                                        rel + "/" if is_dir else rel))):
                            if is_dir:
                                # Pruned — but a configured root beneath it
                                # is still searched, as a root of its own.
                                stack.extend(
                                    (sub, sub) for sub in _outermost_below(path, protected))
                            continue
                        if is_dir:
                            stack.append((path, base))
                        elif entry.is_file(follow_symlinks=False):
                            yield path
                        elif entry.is_symlink() and entry.is_file():
                            target = path.resolve()
                            if any(target == r or r in target.parents
                                   for r in (root, *allowed)):
                                yield path
                    except (OSError, ValueError):
                        continue
        except (OSError, PermissionError) as exc:
            logger.debug("FILE_OPS: cannot read %s: %s", current, exc)
            continue


def _outermost_below(directory: Path, protected: Sequence[Path]) -> List[Path]:
    """Protected roots at or inside *directory*, minus those inside another."""
    below = [p for p in protected if p == directory or directory in p.parents]
    return [p for p in below
            if not any(other in p.parents for other in below)]


def find_files(
    roots: Sequence[Path],
    pattern: str,
    *,
    max_results: int = 50,
    excludes: Sequence[str] = DEFAULT_EXCLUDES,
    include_ignored: bool = False,
    include_hidden: bool = False,
) -> Dict[str, Any]:
    """Files whose path matches *pattern*, searched under the allowed roots."""
    # The configured roots, kept BEFORE the collapse: a directory that the
    # configuration named as searchable must not be hidden by an ignore rule
    # inherited from an ancestor root.
    configured = [Path(r).resolve() for r in roots]
    roots = deduplicate_roots(roots)
    if include_ignored:
        # "Search everything" has to mean everything. Lifting only the
        # .gitignore rules while the configured excludes kept filtering made
        # the answer say "pass include_ignored=true" to a caller who already
        # had — measured on a file under node_modules/. Hidden files count as
        # filtered too: the note offers include_ignored for them as well.
        #
        # .git stays pruned: compressed objects and packfiles are never what
        # a search is looking for, and walking them is pure cost.
        excludes = ALWAYS_EXCLUDED
        include_hidden = True
    report = SearchReport(
        excluded_patterns=list(excludes),
        hidden_files_skipped=not include_hidden,
    )
    found: List[str] = []
    # One more than asked for: collecting exactly max_results cannot tell
    # "there were more" from "that was all", and the answer then tells the
    # caller to narrow a pattern that was already complete.
    limit = max_results + 1

    for root in roots:
        if len(found) >= limit:
            break
        if not root.exists():
            continue
        local_pattern = relativize_pattern(pattern, root)
        spec = None if include_ignored else gitignore_spec(root)
        # Accumulated, not assigned: with several roots the last one used to
        # decide, so the report denied a filter that had been applied.
        report.respected_ignore_files |= spec is not None
        for path in walk_files(root, excludes, include_hidden=include_hidden,
                               ignore_spec=spec,
                               protected=sub_roots(root, configured),
                               allowed=roots):
            rel = path.relative_to(root).as_posix()
            if path_matches(rel, path.name, local_pattern):
                found.append(str(path))
                if len(found) >= limit:
                    break

    report.truncated = len(found) > max_results
    return {
        "status": "success",
        "files": sorted(found)[:max_results],
        "total_found": min(len(found), max_results),
        "truncated": report.truncated,
        "skipped": report.as_dict(len(found)),
    }


def grep(
    roots: Sequence[Path],
    query: str,
    *,
    is_regex: bool = False,
    case_sensitive: bool = False,
    include_pattern: Optional[str] = None,
    context_lines: int = 2,
    max_results: int = 100,
    excludes: Sequence[str] = DEFAULT_EXCLUDES,
    include_ignored: bool = False,
    include_hidden: bool = False,
    max_filesize_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Lines matching *query*, searched under the allowed roots."""
    configured = [Path(r).resolve() for r in roots]
    roots = deduplicate_roots(roots)
    if include_ignored:
        excludes = ALWAYS_EXCLUDED   # see find_files: everything means everything
        include_hidden = True
    report = SearchReport(
        excluded_patterns=list(excludes),
        hidden_files_skipped=not include_hidden,
    )
    matches: List[Dict[str, Any]] = []
    files_searched: set[str] = set()
    limit = max_results + 1   # see find_files: truncation must be a fact

    if is_regex:
        try:
            compiled = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            return {"status": "error", "error": f"Invalid regex: {exc}",
                    "error_type": "RegexError"}
    else:
        compiled = None
    needle = query if case_sensitive else query.lower()
    ceiling = max_filesize_bytes or _DEFAULT_MAX_FILE_BYTES

    for root in roots:
        if len(matches) >= limit:
            break
        if not root.exists():
            continue
        spec = None if include_ignored else gitignore_spec(root)
        report.respected_ignore_files |= spec is not None
        # Resolved once per root, not once per file: this sat in the hot
        # loop, and the loop runs over every file in the tree.
        local_include = (relativize_pattern(include_pattern, root)
                         if include_pattern else None)
        for path in walk_files(root, excludes, include_hidden=include_hidden,
                               ignore_spec=spec,
                               protected=sub_roots(root, configured),
                               allowed=roots):
            if len(matches) >= limit:
                break
            if local_include:
                rel = path.relative_to(root).as_posix()
                if not path_matches(rel, path.name, local_include):
                    continue
            # Both guards run BEFORE the file counts as searched: a file
            # that was never opened must not appear in "searched N files",
            # or the number quietly claims coverage it does not have.
            try:
                if path.stat().st_size > ceiling:
                    report.size_skipped += 1
                    continue
            except OSError:
                continue
            if not is_text_file(path):
                report.binary_skipped += 1
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            files_searched.add(str(path))
            # Split on newline only, never splitlines(): that one also breaks
            # on form feed, the \x1c-\x1e separators and \u2028, and the line
            # numbers then stop matching what read_file shows. read_text has
            # already folded CRLF and CR into a plain newline.
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            for index, line in enumerate(lines):
                hit = (compiled.search(line) if compiled
                       else (needle in (line if case_sensitive else line.lower())))
                if not hit:
                    continue
                matches.append({
                    "file_path": str(path),
                    "line_number": index + 1,
                    "line_content": line,
                    "context_before": lines[max(0, index - context_lines):index],
                    "context_after": lines[index + 1:index + 1 + context_lines],
                })
                if len(matches) >= limit:
                    break

    report.truncated = len(matches) > max_results
    return {
        "status": "success",
        "matches": matches[:max_results],
        "total_matches": min(len(matches), max_results),
        "total_files": len(files_searched),
        "truncated": report.truncated,
        "skipped": report.as_dict(len(matches)),
    }
