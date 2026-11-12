"""Index-free file and text search: ripgrep, and the pruning walk behind it.

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

RIPGREP, AND THE WALKER BEHIND IT
=================================
ripgrep was built once, measured at 0.8 s against the walker's 0.8 s, and
removed: the speed was not where the cost was, and a second engine was a
second set of answers. That measurement aged. On 12.11.2026 the coder harness
tree held 45.920 files -- nine checkouts of this repository under
data/workspace -- and a grep that matched nothing took 24 s warm and 251 s
cold in the walker, against 0.8 s in ripgrep. The coder runs one grep before
every turn (tool_preload), so that was the wait on every message.

So ``grep`` and ``find_files`` run ripgrep when ``ripgrep.locate`` finds one,
and the walker when it does not. The second set of answers is what the old
attempt got wrong, and every way it got them wrong is closed here rather than
lived with:

* globs: an include pattern is never handed to ripgrep. A positive
  ``--glob`` there is a whitelist that overrides the ignore files and the
  hidden-file rule -- measured: ``*`` brought a gitignored log back. ripgrep
  lists, :func:`path_matches` decides; the excludes go to ripgrep (a negative
  glob can only drop) and are checked once more on every hit;
* ignore files: both read ``.gitignore``, ``.ignore`` and ``.rgignore`` in
  every directory, none above the search root, none global, with or without
  a ``.git`` (``--no-require-git --no-ignore-parent --no-ignore-global
  --no-ignore-exclude``), in ripgrep's precedence (:func:`_ignored`);
* hidden: a dot name, and on Windows the hidden attribute, which ripgrep
  honours as well;
* line ends: ``--crlf``, or ``$`` never matched a line of the 176 CRLF files
  in src/; a UTF-8 BOM is not part of line 1 on either engine;
* binary files: ripgrep reads them as text (``--text``) and a hit goes
  through :func:`is_text_file` like a walked file -- left to itself, ripgrep
  drops a file with a NUL byte without a word, and ``--binary`` turned the
  NUL into a line break, which moved every later line number;
* non-UTF-8 lines arrive as bytes in ripgrep's JSON and are decoded the way
  ``read_text(errors="ignore")`` decodes them;
* a regex ripgrep's dialect rejects (look-around, back references) is answered
  by the walker, not with "0 matches"; ``\\<``, ``\\>`` and ``[[:`` mean
  something else in each dialect and go to the walker too, and every line
  ripgrep reports is held against Python's reading of the query;
* an include pattern lists its files first (through the same filters as
  search_files), and ripgrep reads exactly those -- so ``total_files`` counts
  what the include names, not the whole tree;
* links: ripgrep follows them (``--follow``), and a hit behind a linked
  directory, or a linked file whose target lies outside the sandbox, is
  dropped -- the walker never enters the one and never yields the other;
* order: both sort their hits, so a search that collects fewer than its
  ceiling answers the same on either engine and after every restart.

What is left differs on purpose: ripgrep cannot say how many files it skipped
for size, so that note exists only on the walker; it counts the binary files
that held a match, where the walker counts every binary file it met; and a
lone CR (classic Mac line ends) is a line break to the walker, as read_file
counts lines, but not to ripgrep, which breaks lines at LF only. ``--follow``
has a price the walker does not pay: ripgrep walks a linked directory and
the hits from it are dropped afterwards, so a link to a large tree costs its
walk (none in the coder tree on 12.11.2026).

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

import base64
import functools
import json
import logging
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from agent_system.utils.path_sandbox import is_secret, might_be_private, private_places

from . import ripgrep

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

#: Ignore files read in every directory, in rising precedence (ripgrep's order).
IGNORE_FILES: Tuple[str, ...] = (".gitignore", ".ignore", ".rgignore")

#: Longest line a match reports. A minified bundle is one line of a megabyte,
#: and one hit on it used to be that megabyte in the model's context.
MAX_LINE_CHARS = 2000

#: Hits collected before sorting, at least. A search that finds fewer answers
#: the same on both engines; one that finds more is cut, says so, and the cut
#: may fall differently.
_COLLECT_AT_LEAST = 1000

#: Pass ``rg=USE_LOCATED`` (the default) to search with whatever
#: ``ripgrep.locate()`` finds; None forces the walker.
USE_LOCATED = "located"

#: Outside a character class: syntax both dialects accept and read differently
#: (a word boundary to ripgrep, a literal "<" to Python).
_DIALECT_TOKENS = ("\\<", "\\>")
#: Inside one: ripgrep's POSIX classes, nested classes and set operations,
#: all of them plain characters to Python ("[_[:alnum:]]" is "_", "[", ":"...).
_CLASS_TOKENS = ("[", "&&", "--", "~~")


def _reads_differently(query: str) -> bool:
    """Whether ripgrep would read the regex *query* otherwise than Python.

    ``accept`` drops a line ripgrep reports and Python would not; a line that
    ripgrep never reports it cannot bring back -- so these go to the walker.
    """
    if any(token in query for token in _DIALECT_TOKENS):
        return True
    index, inside = 0, False
    while index < len(query):
        char = query[index]
        if char == "\\":
            index += 2
            continue
        if not inside and char == "[":
            inside = True
            index += 1
            if query[index:index + 1] == "^":
                index += 1
            if query[index:index + 1] == "]":
                index += 1   # a "]" first in a class is literal
            continue
        if inside:
            if char == "]":
                inside = False
            elif any(query.startswith(token, index) for token in _CLASS_TOKENS):
                return True
        index += 1
    return False

#: Files an include pattern may name for ripgrep to read one by one; past
#: this ripgrep reads the tree and the include is held against each hit.
_EXPLICIT_FILES = 2000


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

    ``{ts,tsx}`` is an alternative and ``[!a]`` a negated class, as in git and
    ripgrep. Both used to be taken literally: ``*.{ts,tsx}`` -- the example
    opencode puts in its tool description -- matched no file at all.
    """
    alternatives = _expand_braces(pattern)
    body = "|".join(_glob_regex(alternative) for alternative in alternatives)
    return re.compile("^(?:" + body + ")$", re.IGNORECASE if ignore_case else 0)


def _expand_braces(pattern: str) -> List[str]:
    """``a{b,c}d`` -> ``[abd, acd]``; an unclosed or comma-less brace stays literal."""
    depth, start = 0, -1
    for index, char in enumerate(pattern):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                inner = pattern[start + 1:index]
                parts, level, last = [], 0, 0
                for position, inner_char in enumerate(inner):
                    if inner_char == "{":
                        level += 1
                    elif inner_char == "}":
                        level -= 1
                    elif inner_char == "," and level == 0:
                        parts.append(inner[last:position])
                        last = position + 1
                if not parts:
                    continue  # "{x}" is no alternative
                parts.append(inner[last:])
                head, tail = pattern[:start], pattern[index + 1:]
                return [expanded for part in parts
                        for expanded in _expand_braces(head + part + tail)]
    return [pattern]


def _glob_regex(pattern: str) -> str:
    """One brace-free glob as a regex body (see :func:`compile_glob`)."""
    out: List[str] = []
    index, length = 0, len(pattern)
    while index < length:
        if pattern.startswith("**/", index):
            # Zero or more directories. Not "(?:[^/]+/)*": two of those in a
            # row ("**/" put in front of "**/.git/**") backtracked so hard
            # that the excludes were 1.2 of the 1.6 s a listing took.
            out.append("(?:.*/)?")
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
            content = index + 1
            negated = pattern[content:content + 1] in ("!", "^")
            if negated:
                content += 1
            # The first character of a class may be "]" itself ("[]a]").
            close = pattern.find("]", content + 1)
            if close == -1:
                out.append(re.escape(pattern[index]))
                index += 1
            else:
                out.append(("[^" if negated else "[") + pattern[content:close] + "]")
                index = close + 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return "".join(out)


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
    if pattern.startswith("**/"):
        return False   # already matched at any depth
    return compile_glob("**/" + pattern, ignore_case).match(rel) is not None


def _is_excluded(rel: str, name: str, excludes: Sequence[str]) -> bool:
    """Whether a path, RELATIVE to its search root, matches an exclude glob.

    Relative only. Matching the absolute path as well made the root's own
    ancestors count: a repository checked out under ``/srv/build/`` lost every
    file to the ``**/build/**`` exclude the coder harness configures.
    """
    by_name, by_path = _exclusion_matchers(tuple(excludes))
    return bool((by_name is not None and by_name.match(name))
                or (by_path is not None and by_path.match(rel)))


@functools.lru_cache(maxsize=64)
def _exclusion_matchers(excludes: Tuple[str, ...]
                        ) -> Tuple[Optional["re.Pattern[str]"], Optional["re.Pattern[str]"]]:
    """The exclude globs as two regexes: one for the name, one for the path.

    The same rules as :func:`path_matches`, case-sensitive, but one match per
    entry instead of two per pattern: the per-pattern loop was a third of a
    walk over the coder tree, ten patterns for each of 45.000 entries.
    """
    names: List[str] = []
    paths: List[str] = []
    for pattern in excludes:
        # "**/x/**" also excludes the directory x itself — which is what lets
        # the walker prune it instead of opening it.
        for variant in ([pattern, pattern[:-3]] if pattern.endswith("/**") else [pattern]):
            for alternative in _expand_braces(variant):
                if "/" not in alternative:
                    names.append(_glob_regex(alternative))
                else:
                    paths.append(_glob_regex(alternative))
                    if not alternative.startswith("**/"):
                        paths.append(_glob_regex("**/" + alternative))
    by_name = re.compile("^(?:" + "|".join(names) + ")$") if names else None
    by_path = re.compile("^(?:" + "|".join(paths) + ")$") if paths else None
    return by_name, by_path


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
    return bool(text_verdict(path))  # unreadable — nothing to search


def text_verdict(path: Path) -> Optional[bool]:
    """``is_text_file``, with None for a file that could not be read at all.

    The index needs the difference: a binary file is left out until it changes,
    a locked one is tried again by the next pass.
    """
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(SNIFF_BYTES)
    except OSError:
        return None
    if not chunk:
        return False  # empty file carries no searchable text
    return looks_like_text(chunk)


def looks_like_text(chunk: bytes) -> bool:
    """The content rule of ``is_text_file``, on the first bytes of a file."""
    if b"\x00" in chunk:
        return False  # NUL byte: binary
    # Control characters outside tab/newline/CR/formfeed/escape. Text in any
    # encoding stays far below the threshold; binaries blow past it. ESC was
    # counted although this line exempted it: a coloured test log read as binary.
    control = sum(1 for byte in chunk if byte < 0x09 or (0x0e <= byte < 0x20 and byte != 0x1b))
    return control / len(chunk) <= 0.10


def gitignore_spec(root: Path) -> Optional[Any]:
    """The root's own ignore rules as a matcher, or None when unavailable.

    ``pathspec`` implements git's wildmatch semantics, which is why the rules
    are not re-implemented here — a hand-rolled approximation of ``.gitignore``
    is how a search quietly stops matching what the user sees in git.

    Declared in plugin.toml (it used to arrive only transitively, via mypy
    and comfy-cli). A venv without it is still a documented downgrade rather
    than an error: the walk then prunes by the configured patterns alone, and
    the answer says which filters were actually applied. On this repository the difference is not cosmetic —
    ``data/sessions/`` and ``data/message_debugger/`` are gitignored and hold
    130.935 of the 152.664 files.
    """
    lines: List[str] = []
    for name in IGNORE_FILES:
        lines.extend(_ignore_lines(root / name))
    return _rules(lines, root) if lines else None


def _ignore_lines(candidate: Path) -> List[str]:
    try:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        pass
    return []


@functools.lru_cache(maxsize=1)
def ignore_files_supported() -> bool:
    """Whether the walker can honour ignore files at all (see _rules)."""
    return _rules([], Path(".")) is not None


def _rules(lines: List[str], where: Path) -> Optional[Any]:
    """*lines* of ignore files as a matcher, or None when pathspec cannot build one."""
    try:
        import pathspec  # noqa: PLC0415 - optional, resolved per root
    except ImportError:
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
        where)
    return None


#: (directory as a prefix string, {ignore file name: its rules}) from the
#: walking root down.
IgnoreChain = Tuple[Tuple[str, Dict[str, Any]], ...]


def _prefix(directory: Path) -> str:
    """*directory* as the string every path below it starts with.

    Strings, not ``Path.relative_to``: measured on 12.11.2026, relative_to was
    13 of the 22 s a walk of the coder tree took once every directory's ignore
    file was asked -- pathlib builds a new object for every step.
    """
    text = str(directory)
    return text if text.endswith(os.sep) else text + os.sep


def _below(prefix: str, path: str) -> str:
    """*path* relative to the directory *prefix* names, with forward slashes."""
    rel = path[len(prefix):]
    return rel.replace(os.sep, "/") if os.sep != "/" else rel


def _ignored(chain: IgnoreChain, path: str, is_dir: bool) -> bool:
    """Whether the ignore files on the way down to *path* hide it.

    ripgrep's order: ``.rgignore`` before ``.ignore`` before ``.gitignore``,
    each asked from the deepest directory up, and the first rule that matches
    decides -- a ``!keep.log`` in a subdirectory lets through what ``*.log``
    at the top hides, as in git, while a ``.ignore`` above outranks a
    ``.gitignore`` below. Rules are read relative to the directory of the file
    that holds them.
    """
    for kind in reversed(IGNORE_FILES):
        for prefix, specs in reversed(chain):
            spec = specs.get(kind)
            if spec is None:
                continue
            rel = _below(prefix, path)
            verdict = spec.check_file(rel + "/" if is_dir else rel).include
            if verdict is not None:
                return verdict
    return False


def _chain_for(chain: IgnoreChain, directory: Path, names: Any) -> IgnoreChain:
    """*chain* plus *directory*'s own ignore files, when it has any."""
    specs: Dict[str, Any] = {}
    for name in IGNORE_FILES:
        if name in names:
            lines = _ignore_lines(directory / name)
            spec = _rules(lines, directory) if lines else None
            if spec is not None:
                specs[name] = spec
    return chain + ((_prefix(directory), specs),) if specs else chain


#: FILE_ATTRIBUTE_HIDDEN: Windows' own "hidden", which ripgrep honours too.
_HIDDEN_ATTRIBUTE = 0x2


def _hidden(name: str, entry: Any = None, path: Optional[Path] = None) -> bool:
    """A dot name, or on Windows the hidden attribute (of *entry* or *path*)."""
    if name.startswith("."):
        return True
    if os.name != "nt":
        return False
    try:
        info = entry.stat(follow_symlinks=False) if entry is not None else os.lstat(path)
    except (OSError, TypeError):
        return False
    return bool(getattr(info, "st_file_attributes", 0) & _HIDDEN_ATTRIBUTE)


def walk_files(
    root: Path,
    excludes: Sequence[str],
    *,
    include_hidden: bool = False,
    respect_ignore: bool = False,
    protected: Sequence[Path] = (),
    allowed: Sequence[Path] = (),
    on_directory: Optional[Callable[[Path], None]] = None,
    on_ignore_file: Optional[Callable[[Path], None]] = None,
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

    With *respect_ignore* the ignore files of EVERY directory apply, as git
    and ripgrep read them -- not only the root's. Only the root's used to, so
    a checkout below the root brought its build output and its own ignored
    data along.

    *on_directory* is called for a directory when it is queued, i.e. BEFORE it
    is scanned, and *on_ignore_file* for an ignore file before it is read: a
    caller that stats them there (the semantic index's freshness stamps) sees
    every later change as a moved mtime.
    """
    protected = set(protected)
    # (directory, the root its entries are judged against, the ignore files above it)
    stack: List[Tuple[Path, Path, IgnoreChain]] = [(root, root, ())]
    while stack:
        current, base, above = stack.pop()
        try:
            with os.scandir(current) as scan:
                entries = list(scan)
        except (OSError, PermissionError) as exc:
            logger.debug("FILE_OPS: cannot read %s: %s", current, exc)
            continue
        if respect_ignore:
            names = {e.name for e in entries}
            if on_ignore_file is not None:
                for name in IGNORE_FILES:
                    if name in names:
                        on_ignore_file(current / name)
            chain = _chain_for(above, current, names)
        else:
            chain = ()
        base_prefix = _prefix(base)
        for entry in entries:
            name = entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                # is_junction exists from Python 3.12; junctions only
                # exist on Windows.
                junction = getattr(entry, "is_junction", None)
                if is_dir and junction is not None and junction():
                    continue
                rel = _below(base_prefix, entry.path)
                if ((not include_hidden and _hidden(name, entry))
                        or _is_excluded(rel, name, excludes)
                        or (chain and _ignored(chain, entry.path, is_dir))):
                    path = Path(entry.path)
                    if is_dir:
                        # Pruned — but a configured root beneath it is still
                        # searched, as a root of its own: its contents are
                        # judged by its own ignore files, not by the ones above.
                        stack.extend(
                            (sub, sub, ()) for sub in _outermost_below(path, protected))
                    continue
                path = Path(entry.path)
                if might_be_private(name) and is_secret(path):
                    continue  # config/*.env, data/users.db, data/sessions: never read or listed
                if is_dir:
                    if on_directory is not None:
                        on_directory(path)
                    stack.append((path, base, chain))
                elif entry.is_file(follow_symlinks=False):
                    yield path
                elif entry.is_symlink() and entry.is_file():
                    target = path.resolve()
                    # The name check above saw the LINK's name: a
                    # harmless "notes.txt" may point at config/*.env.
                    if is_secret(target):
                        continue
                    if any(target == r or r in target.parents
                           for r in (root, *allowed)):
                        yield path
            except (OSError, ValueError):
                continue


def _outermost_below(directory: Path, protected: Sequence[Path]) -> List[Path]:
    """Protected roots at or inside *directory*, minus those inside another."""
    below = [p for p in protected if p == directory or directory in p.parents]
    return [p for p in below
            if not any(other in p.parents for other in below)]


def _begin(roots: Sequence[Path], excludes: Sequence[str], include_ignored: bool,
           include_hidden: bool) -> Tuple[List[Path], List[Path], Sequence[str], bool, "SearchReport"]:
    """What both searches start from: (deduplicated roots, configured roots, excludes, hidden, report)."""
    # The configured roots, kept BEFORE the collapse: a directory that the
    # configuration named as searchable must not be hidden by an ignore rule
    # inherited from an ancestor root.
    configured = [Path(r).resolve() for r in roots]
    deduplicated = deduplicate_roots(roots)
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
    return deduplicated, configured, excludes, include_hidden, report


def _honoured(include_ignored: bool, engine: str) -> bool:
    """Whether the search honoured ignore files: ripgrep always does, the
    walker when pathspec works. Not "the root has one": every folder's count
    now, and an answer that names no filter where one applied reads as absence."""
    return not include_ignored and (engine == "ripgrep" or ignore_files_supported())


def _collect_ceiling(max_results: int) -> int:
    """How many hits a search gathers before it sorts and cuts (see _COLLECT_AT_LEAST)."""
    return max(max_results * 10, _COLLECT_AT_LEAST)


def find_files(
    roots: Sequence[Path],
    pattern: str,
    *,
    max_results: int = 50,
    excludes: Sequence[str] = DEFAULT_EXCLUDES,
    include_ignored: bool = False,
    include_hidden: bool = False,
    rg: Optional[str] = USE_LOCATED,
) -> Dict[str, Any]:
    """Files whose path matches *pattern*, searched under the allowed roots."""
    roots, configured, excludes, include_hidden, report = _begin(
        roots, excludes, include_ignored, include_hidden)
    ceiling = _collect_ceiling(max_results)
    binary = ripgrep.locate() if rg == USE_LOCATED else rg
    if binary and not ripgrep.still_trusted(binary):
        binary = None
    found: Optional[List[str]] = None
    engine = "python"
    if binary:
        try:
            found = _find_with_rg(binary, roots, configured, pattern, ceiling,
                                  excludes, include_hidden, include_ignored)
            engine = "ripgrep"
        except _RipgrepFailed as exc:
            logger.info("FILE_OPS: ripgrep could not list files (%s) -- walking in Python", exc)
    if found is None:
        found = []
        for root in roots:
            if len(found) > ceiling:
                break
            if not root.exists():
                continue
            local_pattern = relativize_pattern(pattern, root)
            for path in walk_files(root, excludes, include_hidden=include_hidden,
                                   respect_ignore=not include_ignored,
                                   protected=sub_roots(root, configured),
                                   allowed=roots):
                rel = path.relative_to(root).as_posix()
                if path_matches(rel, path.name, local_pattern):
                    found.append(str(path))
                    if len(found) > ceiling:
                        break

    # One more than asked for decides truncation: collecting exactly
    # max_results cannot tell "there were more" from "that was all", and the
    # answer then tells the caller to narrow a pattern that was already complete.
    report.respected_ignore_files = _honoured(include_ignored, engine)
    report.truncated = len(found) > max_results
    return {
        "status": "success",
        "files": sorted(found)[:max_results],
        "total_found": min(len(found), max_results),
        "truncated": report.truncated,
        "skipped": report.as_dict(len(found)),
        "engine": engine,
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
    rg: Optional[str] = USE_LOCATED,
) -> Dict[str, Any]:
    """Lines matching *query*, searched under the allowed roots."""
    roots, configured, excludes, include_hidden, report = _begin(
        roots, excludes, include_ignored, include_hidden)
    if is_regex:
        try:
            compiled = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            return {"status": "error", "error": f"Invalid regex: {exc}",
                    "error_type": "RegexError"}
    else:
        compiled = None
    size_ceiling = max_filesize_bytes or _DEFAULT_MAX_FILE_BYTES
    ceiling = _collect_ceiling(max_results)
    binary = ripgrep.locate() if rg == USE_LOCATED else rg
    if binary and not ripgrep.still_trusted(binary):
        binary = None

    result: Optional[Tuple[List[Dict[str, Any]], int]] = None
    engine = "python"
    needle = query if case_sensitive else query.lower()

    def accept(line: str) -> bool:
        """Python's reading of the query, held against a line ripgrep reported."""
        if compiled is not None:
            return compiled.search(line) is not None
        return needle in (line if case_sensitive else line.lower())

    # A newline never matches one line, and ripgrep would read it as two
    # patterns; the rest of the list means one thing to ripgrep and another
    # to Python (\< is a word boundary there and a "<" here).
    if binary and "\n" not in query and not (
            is_regex and _reads_differently(query)):
        try:
            result = _grep_with_rg(binary, roots, configured, query, is_regex=is_regex,
                                   case_sensitive=case_sensitive,
                                   include_pattern=include_pattern,
                                   context_lines=context_lines, size_ceiling=size_ceiling,
                                   ceiling=ceiling, excludes=excludes,
                                   include_hidden=include_hidden,
                                   include_ignored=include_ignored, report=report,
                                   accept=accept)
            engine = "ripgrep"
        except _RipgrepFailed as exc:
            # A pattern of Python's dialect that ripgrep's rejects (look-around,
            # back references) is answered by the walker, not with "0 matches".
            logger.info("FILE_OPS: ripgrep could not run this search (%s) -- walking in Python", exc)
            report.binary_skipped = 0
    if result is None:
        result = _grep_with_walk(roots, configured, query, compiled, case_sensitive=case_sensitive,
                                 include_pattern=include_pattern, context_lines=context_lines,
                                 size_ceiling=size_ceiling, ceiling=ceiling, excludes=excludes,
                                 include_hidden=include_hidden, include_ignored=include_ignored,
                                 report=report)
    matches, files_searched = result
    matches.sort(key=lambda m: (m["file_path"], m["line_number"]))
    report.respected_ignore_files = _honoured(include_ignored, engine)

    report.truncated = len(matches) > max_results
    return {
        "status": "success",
        "matches": matches[:max_results],
        "total_matches": min(len(matches), max_results),
        "total_files": files_searched,
        "truncated": report.truncated,
        "skipped": report.as_dict(len(matches)),
        "engine": engine,
    }


def _clip(line: str) -> str:
    """*line* cut at MAX_LINE_CHARS, marked when it was."""
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " [line cut]"


def _match_entry(path: Path, lines: Dict[int, str], number: int, context: int) -> Dict[str, Any]:
    """One hit in the shape both engines answer with; *lines* maps line numbers to text."""
    return {
        "file_path": str(path),
        "line_number": number,
        "line_content": _clip(lines[number]),
        "context_before": [_clip(lines[n]) for n in range(max(1, number - context), number)
                           if n in lines],
        "context_after": [_clip(lines[n]) for n in range(number + 1, number + 1 + context)
                          if n in lines],
    }


#: Files read per batch by the walker's grep. Reading is what the walker waits
#: for on a cold disk, and the threads overlap that wait.
_READ_BATCH = 64
_READ_THREADS = 8


def _grep_with_walk(roots: List[Path], configured: List[Path], query: str,
                    compiled: Optional["re.Pattern[str]"], *, case_sensitive: bool,
                    include_pattern: Optional[str], context_lines: int, size_ceiling: int,
                    ceiling: int, excludes: Sequence[str], include_hidden: bool,
                    include_ignored: bool, report: SearchReport
                    ) -> Tuple[List[Dict[str, Any]], int]:
    """The grep without ripgrep: walk, read, match -- (hits, files searched)."""
    needle = query if case_sensitive else query.lower()
    # Whole-file test first, line by line only where it hits: most files of a
    # tree do not contain the query, and splitting each of them into lines was
    # most of the walker's time. MULTILINE keeps ^ and $ at line boundaries;
    # \A and \Z mean something else on a whole file, so those skip the test.
    whole: Optional["re.Pattern[str]"] = None
    if compiled is not None and "\\A" not in query and "\\Z" not in query:
        try:
            whole = re.compile(query, compiled.flags | re.MULTILINE)
        except re.error:
            whole = None

    def load(path: Path) -> Tuple[str, Optional[str]]:
        # Both guards run BEFORE the file counts as searched: a file that was
        # never opened must not appear in "searched N files", or the number
        # quietly claims coverage it does not have.
        try:
            if os.stat(path).st_size > size_ceiling:
                return "size", None
            # One open per file, not two (sniff, then read): opening is what a
            # walk over many files on Windows mostly waits for.
            data = path.read_bytes()
        except OSError:
            return "error", None
        if not data:
            return "miss", ""   # empty: searched, and nothing in it
        if not looks_like_text(data[:SNIFF_BYTES]):
            return "binary", None
        # What read_text gives: universal newlines, undecodable bytes dropped.
        text = data.decode("utf-8", "ignore").replace("\r\n", "\n").replace("\r", "\n")
        if text.startswith("\ufeff"):
            text = text[1:]   # a BOM is not part of line 1 -- not to ripgrep either
        if compiled is None:
            hit = needle in (text if case_sensitive else text.lower())
        elif whole is not None:
            hit = whole.search(text) is not None
        else:
            hit = True  # no whole-file test for this pattern: every line is asked
        return ("text" if hit else "miss"), text

    matches: List[Dict[str, Any]] = []
    files_searched = 0

    def search(path: Path, text: str) -> None:
        # Split on newline only, never splitlines(): that one also breaks on
        # form feed, the \x1c-\x1e separators and  , and the line numbers
        # then stop matching what read_file shows. read_text has already
        # folded CRLF and CR into a plain newline.
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        numbered: Optional[Dict[int, str]] = None
        for index, line in enumerate(lines):
            hit = (compiled.search(line) if compiled
                   else (needle in (line if case_sensitive else line.lower())))
            if not hit:
                continue
            if numbered is None:
                numbered = dict(enumerate(lines, start=1))
            matches.append(_match_entry(path, numbered, index + 1, context_lines))
            if len(matches) > ceiling:
                return

    with ThreadPoolExecutor(max_workers=_READ_THREADS) as pool:
        for root in roots:
            if len(matches) > ceiling:
                break
            if not root.exists():
                continue
            # Resolved once per root, not once per file: this sat in the hot
            # loop, and the loop runs over every file in the tree.
            local_include = (relativize_pattern(include_pattern, root)
                             if include_pattern else None)
            walk = walk_files(root, excludes, include_hidden=include_hidden,
                              respect_ignore=not include_ignored,
                              protected=sub_roots(root, configured), allowed=roots)
            exhausted = False
            while not exhausted and len(matches) <= ceiling:
                batch: List[Path] = []
                for path in walk:
                    if local_include and not path_matches(
                            path.relative_to(root).as_posix(), path.name, local_include):
                        continue
                    batch.append(path)
                    if len(batch) >= _READ_BATCH:
                        break
                else:
                    exhausted = True
                for path, (kind, text) in zip(batch, pool.map(load, batch)):
                    if kind == "size":
                        report.size_skipped += 1
                    elif kind == "binary":
                        report.binary_skipped += 1
                    elif kind in ("text", "miss"):
                        files_searched += 1
                        if kind == "text" and len(matches) <= ceiling:
                            search(path, text or "")
    return matches, files_searched


class _RipgrepFailed(Exception):
    """ripgrep did not answer this search; the walker does."""


class _RipgrepRun:
    """One ripgrep process: its stdout line by line, its stderr afterwards."""

    def __init__(self, binary: str, cwd: Path, args: List[str]):
        self._stderr = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen(
                [binary, *args], cwd=str(cwd), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=self._stderr,
                # A console window per search when the API runs without one.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._stderr.close()
            raise _RipgrepFailed(f"cannot start {binary}: {exc}") from exc
        self.stopped = False

    def lines(self) -> Iterator[bytes]:
        assert self._process.stdout is not None
        for raw in self._process.stdout:
            yield raw.rstrip(b"\r\n")

    def stop(self) -> None:
        self.stopped = True
        self._process.kill()

    def finish(self) -> None:
        """Wait for the process; a message on stderr raises _RipgrepFailed."""
        assert self._process.stdout is not None
        self._process.stdout.close()
        code = self._process.wait()
        self._stderr.seek(0)
        message = self._stderr.read().decode("utf-8", "replace").strip()
        self._stderr.close()
        # Exit 2 also follows an unreadable file in the tree (a Windows "nul"
        # in a checkout, say) -- with --no-messages those say nothing, and the
        # hits found before are good. A message is a pattern ripgrep rejected.
        if not self.stopped and code not in (0, 1) and message:
            raise _RipgrepFailed(" ".join(message.split())[:300])


def _hidden_from(root: Path, target: Path, excludes: Sequence[str], include_hidden: bool,
                 respect_ignore: bool) -> bool:
    """Whether *root*'s walk prunes a directory on the way down to *target*."""
    current: Path = root
    chain: IgnoreChain = ()
    for part in target.relative_to(root).parts:
        if respect_ignore:
            chain = _chain_for(chain, current,
                               {name for name in IGNORE_FILES if (current / name).is_file()})
        child = current / part
        rel = child.relative_to(root).as_posix()
        if ((not include_hidden and _hidden(part, path=child))
                or _is_excluded(rel, part, excludes)
                or (chain and _ignored(chain, str(child), True))):
            return True
        current = child
    return False


def _rg_scopes(root: Path, configured: Sequence[Path], excludes: Sequence[str],
               include_hidden: bool, respect_ignore: bool) -> List[Path]:
    """One ripgrep run each: *root*, and every configured root below it that
    *root*'s walk prunes -- the walker searches those as roots of their own."""
    hidden = [p for p in sub_roots(root, configured)
              if _hidden_from(root, p, excludes, include_hidden, respect_ignore)]
    scopes = [root]
    for scope in sorted(p for p in hidden if not any(o in p.parents for o in hidden)):
        scopes += _rg_scopes(scope, configured, excludes, include_hidden, respect_ignore)
    return scopes


def _glob_variants(pattern: str) -> List[str]:
    """*pattern* as ripgrep needs it to match at least what path_matches does:
    a glob with a separator is anchored there, and path_matches also tries it
    with ``**/`` in front. Only ever used negated."""
    if "/" in pattern and not pattern.startswith("**/"):
        return [pattern, "**/" + pattern]
    return [pattern]


def _rg_filters(scope: Path, excludes: Sequence[str], include_hidden: bool,
                include_ignored: bool) -> List[str]:
    """The filter flags of one run -- negative globs only (see the module head)."""
    args = ["--no-config", "--no-messages", "--follow", "--no-require-git",
            "--no-ignore-parent", "--no-ignore-global", "--no-ignore-exclude"]
    if include_ignored:
        args.append("--no-ignore")
    if include_hidden:
        args.append("--hidden")
    for pattern in excludes:
        for variant in _glob_variants(pattern):
            args += ["--glob", "!" + variant]
            if variant.endswith("/**"):
                # The directory itself, so ripgrep prunes it instead of
                # opening it and dropping every file inside.
                args += ["--glob", "!" + variant[:-3]]
    # Never read, not only never shown: the hits are checked again below, but
    # a session store is a hundred thousand files to open for nothing.
    _, _, folders = private_places()
    for folder in folders:
        try:
            rel = Path(folder).resolve().relative_to(scope).as_posix()
        except (OSError, ValueError):
            continue
        # Anchored: "!sessions" alone dropped every folder of that name at
        # any depth when the scope was the private folder's parent.
        args += ["--iglob", f"!/{rel}/**", "--iglob", f"!/{rel}"]
    return args


def _rg_name(data: Dict[str, Any]) -> str:
    """The path of a ripgrep JSON ``path`` object, as ripgrep printed it."""
    if "text" in data:
        return data["text"]
    return os.fsdecode(base64.b64decode(data.get("bytes", "")))


def _rg_rel(data: Dict[str, Any]) -> str:
    """The path of a ripgrep JSON ``path`` object, relative to its cwd, with forward slashes."""
    return _rg_relative(_rg_name(data))


def _rg_relative(raw: str) -> str:
    """A path ripgrep printed ("./a/b" or ".\\a\\b") as "a/b"."""
    rel = raw.replace("\\", "/") if os.sep == "\\" else raw
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _rg_text(data: Dict[str, Any]) -> str:
    """A line from ripgrep's JSON, decoded as read_text(errors='ignore') would."""
    if "text" in data:
        text = data["text"]
    else:
        text = base64.b64decode(data.get("bytes", "")).decode("utf-8", "ignore")
    return text.rstrip("\n").rstrip("\r")


def _file_link_admissible(full: str, roots: Sequence[Path]) -> bool:
    """A linked file is read only when its target lies in the sandbox and is no secret."""
    if not os.path.islink(full):
        return True
    target = Path(full).resolve()
    # The link's name told nothing: a harmless "notes.txt" may point at config/*.env.
    return not is_secret(target) and any(target == r or r in target.parents for r in roots)


class _HitFilter:
    """The walker's filters, held against a path ripgrep reported.

    ripgrep reaches further than the walker in two ways -- --follow enters
    linked directories, and the include pattern is never handed to it -- and
    each is taken back here. In strings, for the reason _prefix gives: a
    listing of the coder tree is 45.000 paths.
    """

    def __init__(self, root: Path, scope: Path, roots: Sequence[Path],
                 excludes: Sequence[str], pattern: Optional[str], file_links: bool = True):
        self._scope_prefix = _prefix(scope)
        # The include pattern is relative to the root, the paths to the scope.
        self._to_root = "" if scope == root else _below(_prefix(root), str(scope)) + "/"
        self._roots = roots
        self._excludes = excludes
        self._pattern = pattern
        # False: the caller asks _file_link_admissible itself, for the files
        # it reads -- one lstat per listed file was most of a listing's cost.
        self._file_links = file_links
        self._linked: Dict[str, bool] = {}

    def __call__(self, rel: str) -> Optional[Path]:
        """The absolute path when the walker would have yielded it, else None."""
        name = rel.rsplit("/", 1)[-1]
        if _is_excluded(rel, name, self._excludes):
            return None
        if self._pattern and not path_matches(self._to_root + rel, name, self._pattern):
            return None
        parts = rel.split("/")
        full = self._scope_prefix + (rel.replace("/", os.sep) if os.sep != "/" else rel)
        path = Path(full)
        # As cheap as the walker's test: a name that cannot be private is not
        # resolved. The private folders inside the scope are excluded by glob.
        if any(might_be_private(part) for part in parts) and is_secret(path):
            return None
        directory = self._scope_prefix
        for part in parts[:-1]:
            directory += part + os.sep
            if self._is_link(directory):
                return None  # behind a linked directory: the walker never enters one
        if self._file_links and not _file_link_admissible(full, self._roots):
            return None
        return path

    def _is_link(self, directory: str) -> bool:
        """Whether *directory* (a prefix string) is a link or a junction; cached."""
        known = self._linked.get(directory)
        if known is None:
            bare = directory.rstrip(os.sep)
            junction = getattr(os.path, "isjunction", None)
            known = os.path.islink(bare) or bool(junction and junction(bare))
            self._linked[directory] = known
        return known


def _find_with_rg(binary: str, roots: List[Path], configured: List[Path], pattern: str,
                  ceiling: Optional[int], excludes: Sequence[str], include_hidden: bool,
                  include_ignored: bool, file_links: bool = True) -> List[str]:
    """search_files through ``rg --files`` -- the paths, unsorted; None: no ceiling.

    *file_links* False leaves linked files unchecked (see _HitFilter): the
    caller checks the ones it reads.
    """
    found: List[str] = []
    seen: set = set()
    for root in roots:
        if not root.exists():
            continue
        local_pattern = relativize_pattern(pattern, root)
        for scope in _rg_scopes(root, configured, excludes, include_hidden, not include_ignored):
            if not scope.is_dir():
                # A configured root that is not there: Popen would fail on its
                # cwd, and the whole search fell back to the walker for it.
                continue
            admit = _HitFilter(root, scope, roots, excludes, local_pattern, file_links)
            # One path per line, not --null: the lines are what lets the
            # listing stop at the ceiling instead of being read to its end.
            args = (["--files"]
                    + _rg_filters(scope, excludes, include_hidden, include_ignored)
                    + ["."])
            run = _RipgrepRun(binary, scope, args)
            try:
                for raw in run.lines():
                    if not raw:
                        continue
                    path = admit(_rg_relative(os.fsdecode(raw)))
                    if path is None or path in seen:
                        continue
                    seen.add(path)
                    found.append(str(path))
                    if ceiling is not None and len(found) > ceiling:
                        run.stop()
                        break
            finally:
                run.finish()
            if ceiling is not None and len(found) > ceiling:
                return found
    return found


def _explicit_batches(files: List[str], budget: int = 24_000) -> Iterator[List[str]]:
    """*files* in batches whose command line stays below Windows' 32.767 characters."""
    batch: List[str] = []
    used = 0
    for name in files:
        if batch and used + len(name) + 3 > budget:
            yield batch
            batch, used = [], 0
        batch.append(name)
        used += len(name) + 3
    if batch:
        yield batch


def _consume(run: "_RipgrepRun", to_path: Callable[[Dict[str, Any]], Optional[Path]],
             accept: Callable[[str], bool], context_lines: int, ceiling: int,
             matches: List[Dict[str, Any]], seen: set, report: SearchReport) -> int:
    """Read one run's JSON into *matches*; returns the files it searched."""
    searched = 0
    current: Optional[Path] = None
    lines: Dict[int, str] = {}
    hits: List[int] = []
    try:
        for raw in run.lines():
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            kind, data = message.get("type"), message.get("data") or {}
            if kind == "begin":
                current = to_path(data.get("path") or {})
                lines, hits = {}, []
                if current is not None and current in seen:
                    current = None
                if current is not None and not is_text_file(current):
                    # ripgrep only knows NUL bytes; the walker also counts
                    # control characters (see is_text_file).
                    report.binary_skipped += 1
                    current = None
            elif kind in ("match", "context") and current is not None:
                number = data.get("line_number")
                if number is not None:
                    lines[number] = _rg_text(data.get("lines") or {})
                    if kind == "match":
                        hits.append(number)
            elif kind == "end":
                if current is not None:
                    seen.add(current)
                    for number in hits:
                        if accept(lines[number]):
                            matches.append(_match_entry(current, lines, number, context_lines))
                current = None
                if len(matches) > ceiling:
                    run.stop()
                    break
            elif kind == "summary":
                searched += int((data.get("stats") or {}).get("searches", 0))
    finally:
        run.finish()
    return searched


def _grep_with_rg(binary: str, roots: List[Path], configured: List[Path], query: str, *,
                  is_regex: bool, case_sensitive: bool, include_pattern: Optional[str],
                  context_lines: int, size_ceiling: int, ceiling: int,
                  excludes: Sequence[str], include_hidden: bool, include_ignored: bool,
                  report: SearchReport, accept: Callable[[str], bool]
                  ) -> Tuple[List[Dict[str, Any]], int]:
    """The grep through ripgrep's JSON output -- (hits, files searched)."""
    matches: List[Dict[str, Any]] = []
    seen: set = set()
    searched = 0
    search = (["--json", "--context", str(context_lines), "--max-filesize", str(size_ceiling),
               "--text", "--crlf",
               "--case-sensitive" if case_sensitive else "--ignore-case"]
              + ([] if is_regex else ["--fixed-strings"]))

    def floor() -> int:
        # A run stopped before its summary: the files it got to are a floor.
        return searched + len({m["file_path"] for m in matches})

    if include_pattern:
        # The include is never a ripgrep glob (see the module head). Its files
        # are listed through search_files' filters, and ripgrep reads exactly
        # those -- the file names it is given are not filtered again.
        listed = _find_with_rg(binary, roots, configured, include_pattern, None, excludes,
                               include_hidden, include_ignored, file_links=False)
        if len(listed) <= _EXPLICIT_FILES:
            # What ripgrep's walk would have applied itself: --max-filesize
            # does not hold for a file it is given by name.
            kept = []
            for name in listed:
                try:
                    if os.stat(name).st_size > size_ceiling:
                        report.size_skipped += 1
                        continue
                except OSError:
                    continue
                if _file_link_admissible(name, roots):
                    kept.append(name)
            listed = kept
            # Any root that exists will do as the cwd: the names are absolute,
            # and a missing first root failed Popen and sent the search to the walker.
            cwd = next((root for root in roots if root.is_dir()), None)
            if cwd is None:
                return matches, 0
            for batch in _explicit_batches(sorted(listed)):
                run = _RipgrepRun(binary, cwd, search + [
                    "--no-config", "--no-messages", "--regexp", query, "--", *batch])
                searched += _consume(run, lambda data: Path(_rg_name(data)), accept,
                                     context_lines, ceiling, matches, seen, report)
                if run.stopped:
                    return matches, floor()
            return matches, searched
        # Too many to name: ripgrep reads the tree and the include is held
        # against each hit, as below; the count is what the include names.
        searched = len(listed)
    count_runs = not include_pattern
    for root in roots:
        if not root.exists():
            continue
        local_include = relativize_pattern(include_pattern, root) if include_pattern else None
        for scope in _rg_scopes(root, configured, excludes, include_hidden, not include_ignored):
            if not scope.is_dir():
                continue   # see _find_with_rg
            admit = _HitFilter(root, scope, roots, excludes, local_include)
            run = _RipgrepRun(binary, scope, search
                              + _rg_filters(scope, excludes, include_hidden, include_ignored)
                              + ["--regexp", query, "--", "."])
            files = _consume(run, lambda data, admit=admit: admit(_rg_rel(data)), accept,
                             context_lines, ceiling, matches, seen, report)
            if count_runs:
                searched += files
            if run.stopped:
                return matches, floor() if count_runs else searched
    return matches, searched


def list_directory(
    directory: Path,
    *,
    recursive: bool = False,
    pattern: Optional[str] = None,
    max_results: int = 200,
    excludes: Sequence[str] = DEFAULT_EXCLUDES,
    include_hidden: bool = False,
    include_ignored: bool = False,
) -> Dict[str, Any]:
    """Files and directories under *directory*, capped, with what was skipped.

    Uncapped, a recursive listing of a repository is the whole repository in
    the model's context; it also used ``rglob``, which descends into every
    ignored and excluded tree first. Recursive listings therefore use the same
    pruning walk as the searches, and every listing stops at *max_results*
    entries and says so.
    """
    if include_ignored:
        excludes = ALWAYS_EXCLUDED   # see find_files
        include_hidden = True
    report = SearchReport(
        excluded_patterns=list(excludes) if recursive else [],
        hidden_files_skipped=not include_hidden,
    )
    files: List[str] = []
    directories: List[str] = []
    limit = max_results + 1   # see find_files: truncation must be a fact
    if pattern:
        # Same spellings as the searches accept: ./, backslashes, a repeated root.
        pattern = relativize_pattern(pattern, directory)

    def full() -> bool:
        return len(files) + len(directories) >= limit

    def wanted(path: Path) -> bool:
        if not pattern:
            return True
        rel = path.relative_to(directory).as_posix()
        return path_matches(rel, path.name, pattern)

    if recursive:
        report.respected_ignore_files = _honoured(include_ignored, "python")

        def on_directory(path: Path) -> None:
            if not full() and wanted(path):
                directories.append(str(path))

        for path in walk_files(directory, excludes, include_hidden=include_hidden,
                               respect_ignore=not include_ignored,
                               on_directory=on_directory):
            if full():
                break
            if wanted(path):
                files.append(str(path))
    else:
        with os.scandir(directory) as entries:
            for entry in entries:
                if full():
                    break
                if not include_hidden and _hidden(entry.name, entry):
                    continue
                path = Path(entry.path)
                if not wanted(path):
                    continue
                try:
                    if entry.is_dir():
                        directories.append(str(path))
                    elif entry.is_file():
                        files.append(str(path))
                except OSError:
                    continue

    total = len(files) + len(directories)
    report.truncated = total > max_results
    if report.truncated:
        # Drop the one extra entry that proved there were more.
        (files if files else directories).pop()
    return {
        "status": "success",
        "dir_path": str(directory),
        "files": sorted(files),
        "directories": sorted(directories),
        "total_files": len(files),
        "total_directories": len(directories),
        "truncated": report.truncated,
        "skipped": report.as_dict(total),
    }
