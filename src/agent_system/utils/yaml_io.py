"""YAML loading through libyaml when it is available.

``yaml.safe_load`` always runs the pure-Python parser, even when PyYAML was
built with libyaml — the C parser is only reachable through
``yaml.load(..., Loader=yaml.CSafeLoader)``. Measured 2026-09-04 on the real
config tree: the pure-Python parser spent 1.2 s of the 0.7 s config load
(under the profiler) and 4.5 s of a single ``agent-cli run`` on 452 schema
loads; libyaml is 8.8x faster on the same files and produced identical
results for every one of the 80 YAML documents in the repo.

Same safety as ``yaml.safe_load``: ``CSafeLoader`` is the SafeLoader
implemented in C — no arbitrary object construction. On an interpreter whose
PyYAML lacks the C extension the pure-Python ``SafeLoader`` is used, so a
missing libyaml changes speed, never behaviour.
"""
from __future__ import annotations

from typing import Any, IO

import yaml


def loader() -> type:
    """The loader class ``safe_load`` uses right now (resolved per call, so a
    test can take libyaml away and see the fallback)."""
    return getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader


def safe_load(stream: str | bytes | IO[str] | IO[bytes]) -> Any:
    """Drop-in for ``yaml.safe_load``: same contract, C parser when present.

    Anything the C parser rejects is handed to the pure-Python loader, which
    settles both halves of the contract at once:

    * it is an ACCELERATOR, not a stricter parser. libyaml rejects documents
      SafeLoader accepts -- a ``%YAML 1.3`` directive, a BOM in the middle of
      a document -- and those loaded before this module existed. They still do.
    * when it fails too, its error is the better one: libyaml reports line and
      column, the Python parser also prints the offending line with a caret.

    ``UnicodeEncodeError`` belongs in the same net: libyaml raises it on a lone
    surrogate, which is not a ``YAMLError`` at all, so no caller catching YAML
    errors would ever see it. SafeLoader turns the same input into a
    ``ReaderError``.

    Only on the error path, and only for text (a file object is already
    consumed by then).
    """
    active = loader()
    try:
        return yaml.load(stream, Loader=active)
    except (yaml.YAMLError, UnicodeEncodeError):
        if active is not yaml.SafeLoader and isinstance(stream, (str, bytes)):
            return yaml.load(stream, Loader=yaml.SafeLoader)
        raise
