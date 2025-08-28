"""Backlog tool library package.

Lightweight parsing and safe-write utilities used by the `backlog` CLI.
This is intentionally small and conservative: it treats the backlog as structured
markdown but preserves unknown content when rewriting.
"""

__all__ = ["parser", "__version__"]

__version__ = "0.1.0"
