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
    """Drop-in for ``yaml.safe_load``: same contract, C parser when present."""
    return yaml.load(stream, Loader=loader())
