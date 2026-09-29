"""OKF core — pure, IO-light library for the Open Knowledge Format (v0.1).

Format, not platform: an OKF *bundle* is a directory tree of markdown files; a
*concept* is one ``.md`` file with a YAML frontmatter block. The only hard rule
is that every concept carries a non-empty ``type``. See docs/okf_support_design.md
and the spec at github.com/GoogleCloudPlatform/knowledge-catalog/okf.

This module owns the format ONCE — parsing/serialization, conformance checking,
link resolution and graph traversal. The tools (server.py) and the consumer
hook both call in here, so the semantics can never drift between them (same
lesson as server_matches_patterns / server_resolution in the agent package).

Design rules honored here:
- **Round-trip fidelity (spec MUST):** frontmatter is parsed and re-serialized
  with ``ruamel.yaml`` so unknown keys, key order, comments and quoting survive
  a read-modify-write. Bundles are git-versioned; edits must diff cleanly.
- **No regex on the body:** the markdown body is treated as opaque text; only
  the frontmatter fence and markdown links are parsed structurally.
- **Consumer tolerance (spec MUST):** broken links, unknown ``type`` values and
  unknown keys never raise — they are tolerated (and, for validation, reported).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# Reserved filenames (spec §6). These are NOT concept documents.
INDEX_FILENAME = "index.md"
LOG_FILENAME = "log.md"
RESERVED_FILENAMES = frozenset({INDEX_FILENAME, LOG_FILENAME})

# Reserved frontmatter fields (spec §1). Only ``type`` is required; the rest are
# optional. Producers MAY add arbitrary extra keys — those are preserved, never
# rejected.
RESERVED_FIELDS = ("type", "title", "description", "resource", "tags", "timestamp")

# Lifecycle vocabulary (spec §5.4). Absent ``status`` means ``stable``;
# ``deprecated`` is how OKF retires a concept — kept for links and history
# instead of deleted.
LIFECYCLE_VALUES = frozenset({"draft", "stable", "deprecated"})


def _lifecycle_of(value: Any) -> Optional[str]:
    """Normalize a raw ``status`` value, or ``None`` if it is not a lifecycle.

    Empty/blank counts as absent (``stable``) — a producer that cleared the
    field did not thereby say something unknown. Everything else that is not
    one of the three values (including numbers, booleans, dates and lists —
    YAML turns ``status: true`` and ``status: 2026-01-01`` into non-strings)
    returns ``None`` so callers can tell "occupied by something foreign"
    apart from "absent".
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return "stable"
    return normalized if normalized in LIFECYCLE_VALUES else None

OKF_VERSION = "0.1"

# Frontmatter fence: a leading ``---`` line, the YAML block, then a closing
# ``---`` (or ``...``) line. Matched only at the very start of the document.
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<yaml>.*?)\r?\n(?:---|\.\.\.)[ \t]*\r?\n?(?P<body>.*)\Z",
    re.DOTALL,
)

# Markdown inline link: ``[text](target)``. We only need the target. Image
# links (``![alt](src)``) are excluded — they are assets, not concept edges.
_LINK_RE = re.compile(r"(?<!\!)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")

# log.md date heading (spec §4): ``## YYYY-MM-DD``.
_DATE_HEADING_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


def _yaml() -> YAML:
    """A round-trip YAML 1.2 handler. Fresh per call — ruamel's YAML objects are
    cheap and not documented as thread-safe to share."""
    y = YAML()  # round-trip mode by default: preserves order, comments, quoting
    y.preserve_quotes = True
    y.width = 4096  # don't line-wrap long scalars (keeps diffs clean)
    return y


# ---------------------------------------------------------------------------
# Frontmatter round-trip
# ---------------------------------------------------------------------------

def split_frontmatter(text: str) -> Tuple[Optional[str], str]:
    """Split raw document text into (raw_yaml_or_None, body).

    Returns ``(None, text)`` when there is no leading frontmatter fence — a
    document without frontmatter is not a conformant concept but must still be
    readable (consumer tolerance). Never raises.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    return m.group("yaml"), m.group("body")


def parse_frontmatter(text: str) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    """Parse a concept document.

    Returns ``(frontmatter, body, error)``:
    - ``frontmatter``: an ordered dict (ruamel ``CommentedMap``) or None if there
      is no frontmatter fence.
    - ``body``: the markdown body (opaque text).
    - ``error``: a human-readable message if the fence was present but the YAML
      failed to parse (``frontmatter`` is None in that case), else None.

    Never raises — malformed input degrades to an error string so callers can
    report it instead of crashing (spec: consumers tolerate; validation reports).
    """
    raw_yaml, body = split_frontmatter(text)
    if raw_yaml is None:
        return None, body, None
    try:
        data = _yaml().load(io.StringIO(raw_yaml))
    except YAMLError as e:
        return None, body, f"unparseable YAML frontmatter: {e}"
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, body, "frontmatter is not a mapping"
    # Drop the single conventional blank-line separator between the closing
    # fence and the body content (handles both LF and CRLF endings).
    # dump_frontmatter re-adds it, so read → modify → write stays idempotent.
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return data, body, None


def merge_frontmatter(base: Optional[Dict[str, Any]],
                      updates: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge ``updates`` into ``base`` IN PLACE and return base.

    Used on concept overwrite: ``base`` is the existing frontmatter (a ruamel
    ``CommentedMap`` when it came from :func:`parse_frontmatter`), so mutating
    it in place preserves comments, key order and quoting — the round-trip
    fidelity the module promises. Nested mappings merge recursively so updating
    one sub-key does not drop its siblings; non-mapping values are replaced.
    Setting a key to ``None`` in ``updates`` removes it (lets a caller clear a
    field). ``base`` None -> a shallow copy of ``updates``.
    """
    if base is None:
        return dict(updates)
    for k, v in updates.items():
        if v is None:
            base.pop(k, None)
        elif (isinstance(v, dict) and k in base and isinstance(base[k], dict)):
            merge_frontmatter(base[k], v)
        else:
            base[k] = v
    return base


def dump_frontmatter(frontmatter: Dict[str, Any], body: str) -> str:
    """Serialize frontmatter + body back into a concept document.

    Round-trip-safe: a ``CommentedMap`` from :func:`parse_frontmatter` keeps its
    key order, comments and quoting. The body is written VERBATIM (a body that
    legitimately begins with blank lines keeps them) — :func:`parse_frontmatter`
    already removed the single separator, so the pair round-trips idempotently.
    """
    buf = io.StringIO()
    _yaml().dump(frontmatter, buf)
    yaml_text = buf.getvalue().rstrip("\n")
    body = body or ""
    return f"---\n{yaml_text}\n---\n\n{body}" if body else f"---\n{yaml_text}\n---\n"


# ---------------------------------------------------------------------------
# Concept + link extraction
# ---------------------------------------------------------------------------

@dataclass
class Concept:
    """One OKF concept document, addressed by its bundle-relative POSIX path
    (e.g. ``/tables/orders.md``)."""
    path: str  # bundle-relative, leading-slash POSIX path
    frontmatter: Dict[str, Any]
    body: str
    # Whether the file even HAD a frontmatter fence, and any YAML parse error —
    # carried so validation can report the true failure (unparseable / missing
    # frontmatter) instead of a misleading "type-required".
    has_frontmatter: bool = True
    parse_error: Optional[str] = None

    @property
    def type(self) -> Optional[str]:
        t = self.frontmatter.get("type")
        return t if isinstance(t, str) and t.strip() else None

    @property
    def title(self) -> Optional[str]:
        t = self.frontmatter.get("title")
        return t if isinstance(t, str) else None

    @property
    def description(self) -> Optional[str]:
        d = self.frontmatter.get("description")
        return d if isinstance(d, str) else None

    @property
    def lifecycle_status(self) -> str:
        """Spec §5.4: ``draft`` | ``stable`` | ``deprecated``; absent means
        ``stable``.

        Deliberately NOT named ``status`` — that word means the MCP
        envelope's ok/error everywhere else in this server, and a concept
        whose lifecycle says "deprecated" is not an errored concept.

        This is how OKF retires knowledge: ``deprecated`` keeps a concept
        "for links and history" instead of deleting it, so the graph keeps
        its edges. A consumer that treats every concept as current will
        happily quote retired knowledge — hence this is surfaced in
        ``list``, not hidden behind a full read of every file.

        Always one of the three: a foreign value (a producer that put its
        own state in ``status``) reads as ``stable`` here, because the tool
        descriptions promise this vocabulary to the model and a value it
        cannot interpret is worse than the documented default. The RAW
        value is not lost — ``validate_bundle`` warns about it, which is
        where a wrong ``status`` gets fixed. Case and surrounding space are
        forgiven: ``Deprecated`` must not slip past the consumer that only
        checks for the exact word (the context-injection hook does).

        On unparseable frontmatter this reports ``stable`` too. That is a
        default, not a finding — such a file already announces itself with
        ``type is None`` and a validate error.
        """
        return _lifecycle_of(self.frontmatter.get("status")) or "stable"

    def links(self) -> List[str]:
        """Bundle-relative targets of every outbound markdown link in the body,
        resolved against this concept's path. External URLs (``http:``, etc.)
        and pure anchors (``#section``) are excluded — only intra-bundle edges."""
        out: List[str] = []
        for m in _LINK_RE.finditer(self.body):
            target = _resolve_link(self.path, m.group("target"))
            if target is not None:
                out.append(target)
        return out


def _is_external(target: str) -> bool:
    return bool(re.match(r"[a-zA-Z][a-zA-Z0-9+.\-]*:", target)) or target.startswith("//")


def _resolve_link(from_path: str, target: str) -> Optional[str]:
    """Resolve a markdown link target to a bundle-relative ``/...`` path, or None
    if it is external / a bare anchor / not a bundle path.

    - ``/tables/x.md`` (bundle-relative, recommended) -> normalized as-is.
    - ``./x.md`` or ``../y.md`` (relative) -> resolved against ``from_path``'s dir.
    - ``http://...``, ``mailto:...`` -> None (external).
    - ``#anchor`` -> None (same-doc anchor).
    A trailing ``#anchor`` on a bundle path is stripped.
    """
    target = target.strip()
    if not target or target.startswith("#"):
        return None
    if _is_external(target):
        return None
    # Drop a trailing anchor: /tables/x.md#schema -> /tables/x.md
    target = target.split("#", 1)[0]
    if not target:
        return None
    if target.startswith("/"):
        base = PurePosixPath(target)
    else:
        parent = PurePosixPath(from_path).parent
        base = parent / target
    # Normalize .. / . segments against the bundle root.
    parts: List[str] = []
    for seg in base.parts:
        if seg in ("", "/"):
            continue
        if seg == ".":
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/" + "/".join(parts)


# ---------------------------------------------------------------------------
# Bundle model — an in-memory view of a loaded bundle
# ---------------------------------------------------------------------------

@dataclass
class Bundle:
    """A loaded OKF bundle: concepts keyed by bundle-relative path, plus the set
    of reserved files present. Built by the server from disk; the graph and
    validation logic operate on this in-memory view so they stay IO-free and
    unit-testable."""
    concepts: Dict[str, Concept] = field(default_factory=dict)
    reserved: Dict[str, str] = field(default_factory=dict)  # path -> raw text
    version: Optional[str] = None  # from root index.md okf_version, if declared

    def neighbors(self, path: str) -> List[str]:
        """Outbound concept links from ``path`` that resolve to a concept present
        in the bundle. Broken links are silently dropped (spec: tolerate)."""
        c = self.concepts.get(path)
        if not c:
            return []
        seen: List[str] = []
        for target in c.links():
            if target in self.concepts and target not in seen:
                seen.append(target)
        return seen

    def broken_links(self, path: str) -> List[str]:
        """Outbound links from ``path`` that do NOT resolve to a present concept
        (reported by validation, tolerated at runtime)."""
        c = self.concepts.get(path)
        if not c:
            return []
        return [t for t in c.links()
                if t not in self.concepts and t not in self.reserved]

    def subgraph(self, seeds: List[str], depth: int = 1) -> List[str]:
        """Breadth-first concept paths reachable from ``seeds`` within ``depth``
        hops (following outbound links), seeds included, in stable BFS order.

        This is the ``Wiki``-shaped retrieval: given a few relevant concepts,
        pull the concepts they reference so the LLM sees the relationships a
        flat vector search would miss."""
        order: List[str] = []
        frontier = [s for s in seeds if s in self.concepts]
        visited = set()
        for s in frontier:
            if s not in visited:
                visited.add(s)
                order.append(s)
        current = list(frontier)
        for _ in range(max(0, depth)):
            nxt: List[str] = []
            for p in current:
                for n in self.neighbors(p):
                    if n not in visited:
                        visited.add(n)
                        order.append(n)
                        nxt.append(n)
            current = nxt
            if not current:
                break
        return order


# ---------------------------------------------------------------------------
# Conformance validation (spec §8 producer MUSTs)
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """One conformance issue. ``severity`` is 'error' for a spec MUST violation,
    'warning' for a SHOULD / likely-mistake (e.g. broken link)."""
    path: str
    rule: str
    severity: str
    message: str


@dataclass
class ConformanceReport:
    findings: List[Finding] = field(default_factory=list)

    @property
    def conformant(self) -> bool:
        """A bundle is conformant when it has no ERROR findings. Warnings
        (broken links, missing optional index.md) do not break conformance —
        consumers must tolerate those."""
        return not any(f.severity == "error" for f in self.findings)

    def add(self, path: str, rule: str, severity: str, message: str) -> None:
        self.findings.append(Finding(path, rule, severity, message))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conformant": self.conformant,
            "errors": sum(1 for f in self.findings if f.severity == "error"),
            "warnings": sum(1 for f in self.findings if f.severity == "warning"),
            "findings": [
                {"path": f.path, "rule": f.rule, "severity": f.severity,
                 "message": f.message}
                for f in self.findings
            ],
        }


def is_reserved(path: str) -> bool:
    """Whether a bundle-relative path is a reserved file (index.md / log.md)."""
    return PurePosixPath(path).name in RESERVED_FILENAMES


def validate_concept_text(path: str, text: str) -> List[Finding]:
    """Validate a single concept document's text against the producer MUSTs:
    parseable frontmatter with a non-empty ``type``. Returns findings (empty =
    conformant). Used both by bundle validation and by the write path (reject a
    write that would violate the format)."""
    findings: List[Finding] = []
    fm, _body, err = parse_frontmatter(text)
    if err is not None:
        findings.append(Finding(path, "frontmatter-parseable", "error", err))
        return findings
    if fm is None:
        findings.append(Finding(
            path, "frontmatter-required", "error",
            "concept has no YAML frontmatter block"))
        return findings
    t = fm.get("type")
    if not isinstance(t, str) or not t.strip():
        findings.append(Finding(
            path, "type-required", "error",
            "frontmatter is missing a non-empty 'type' field"))
    return findings


def validate_bundle(bundle: Bundle) -> ConformanceReport:
    """Validate an in-memory :class:`Bundle` against the spec's producer MUSTs
    and report broken links as warnings. Never raises."""
    report = ConformanceReport()
    for path, concept in bundle.concepts.items():
        # Report the TRUE producer-MUST failure, most specific first, so the
        # fixer isn't misdirected (a YAML syntax error must not read as
        # "missing type"). Mirrors validate_concept_text on the write path.
        if concept.parse_error is not None:
            report.add(path, "frontmatter-parseable", "error", concept.parse_error)
        elif not concept.has_frontmatter:
            report.add(path, "frontmatter-required", "error",
                       "concept has no YAML frontmatter block")
        elif not concept.type:
            report.add(path, "type-required", "error",
                       "frontmatter is missing a non-empty 'type' field")
        # ``status`` belongs to the format (spec §5.4), not to the producer:
        # a foreign value there is not a free extra key, it silently answers
        # "is this knowledge current?" with something no consumer understands.
        # Measured 2026-08-20: a bundle had put its publish verdict there
        # ("publishable", "in Arbeit"), so every reader got an unknown
        # lifecycle. A warning, not an error — the spec permits extra keys,
        # and this one is merely occupied, not malformed.
        # Against the RAW value, not the normalized one: the property maps
        # anything foreign to "stable", so comparing it would make this check
        # blind to exactly the case it exists for (``status: true`` and
        # friends — YAML types the value, not the producer).
        raw_status = concept.frontmatter.get("status")
        if raw_status is not None and _lifecycle_of(raw_status) is None:
            report.add(
                path, "status-vocabulary", "warning",
                f"'status' is the lifecycle field "
                f"({' | '.join(sorted(LIFECYCLE_VALUES))}) but reads "
                f"{raw_status!r} — put producer state in its own key",
            )
        for broken in bundle.broken_links(path):
            report.add(path, "link-resolves", "warning",
                       f"link target not found in bundle: {broken}")
    return report


# ---------------------------------------------------------------------------
# index.md / log.md conventions (spec §3, §4)
# ---------------------------------------------------------------------------

def render_index(entries: List[Tuple[str, Optional[str]]],
                 heading: str = "Contents") -> str:
    """Render an ``index.md`` body from ``(link_target, description)`` pairs.

    Spec §3 body form::

        # Heading
        * [Title](/path.md) - short description

    ``description`` SHOULD be the linked concept's frontmatter description
    (spec). A missing description just omits the ``- ...`` suffix.
    """
    lines = [f"# {heading}", ""]
    for target, desc in entries:
        name = PurePosixPath(target).stem
        if name == "index":
            # A link to a child directory's index.md reads by the directory
            # name ("buecher"), not as a wall of identical "index" links.
            name = PurePosixPath(target).parent.name or "index"
        if desc:
            lines.append(f"* [{name}]({target}) - {desc}")
        else:
            lines.append(f"* [{name}]({target})")
    return "\n".join(lines) + "\n"


def append_log_entry(existing: Optional[str], date_iso: str, action: str,
                     description: str, time_str: Optional[str] = None) -> str:
    """Return ``log.md`` text with a new entry prepended under its date (spec §4:
    ISO ``YYYY-MM-DD`` headings, newest first, ``**Action**: desc``).

    A new date heading is inserted at the top (after the ``# ...Log`` title);
    an existing same-date heading gets the entry appended beneath it. ``date_iso``
    and ``time_str`` are passed in by the caller (this module never reads the
    clock — determinism and the project's UTC-timestamp discipline stay with the
    caller).

    ``time_str`` (``HH:MM:SS``) is prefixed to the entry. It stays OUT of the
    heading on purpose: the spec's date grouping is what other OKF tooling reads,
    and a run that writes a dozen entries in one day needs them distinguishable
    *within* the day, not a dozen headings.
    """
    entry_line = (f"* {time_str} **{action}**: {description}" if time_str
                  else f"* **{action}**: {description}")
    title = "# Update Log"
    if not existing or not existing.strip():
        return f"{title}\n\n## {date_iso}\n{entry_line}\n"

    lines = existing.splitlines()
    # Find the title line (first '# ' heading), default to inserting at top.
    title_idx = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), -1)
    date_heading = f"## {date_iso}"
    date_idx = next((i for i, ln in enumerate(lines) if ln.strip() == date_heading), -1)

    if date_idx != -1:
        # Insert the entry right after the existing date heading (newest first
        # within the day).
        lines.insert(date_idx + 1, entry_line)
        return "\n".join(lines).rstrip("\n") + "\n"

    # New date section: slot it before the first OLDER section, not blindly at
    # the top. Blind-at-the-top is right for the common case (today's entry) and
    # wrong for a backfilled one, which would then sit above newer dates and
    # break the very ordering this function promises. ISO dates compare as
    # strings, so no parsing is needed.
    insert_at = title_idx + 1 if title_idx != -1 else 0
    while insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1
    def _heading_date(line: str) -> Optional[str]:
        """The ISO date of a ``## YYYY-MM-DD`` heading, else None.

        Only date-shaped headings take part in the ordering — a hand-written
        ``## Notes`` section must not be compared against a date and shuffled.
        """
        stripped = line.strip()
        if not stripped.startswith("## "):
            return None
        candidate = stripped[3:].strip()
        return candidate if _DATE_HEADING_RE.match(candidate) else None

    dated = [(i, d) for i, ln in enumerate(lines) if (d := _heading_date(ln))]
    older = next((i for i, d in dated if d < date_iso), None)
    if older is not None:
        insert_at = older
    elif dated:
        # Older than every existing section -> at the end.
        insert_at = len(lines)
    block = [f"## {date_iso}", entry_line, ""]
    if insert_at > 0 and lines[insert_at - 1].strip():
        # Appending after a section that does not end in a blank line (the
        # oldest-date case) — keep sections visually separated.
        block = [""] + block
    new_lines = lines[:insert_at] + block + lines[insert_at:]
    return "\n".join(new_lines).rstrip("\n") + "\n"
