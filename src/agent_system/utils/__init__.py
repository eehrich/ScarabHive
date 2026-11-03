"""Utility modules for the agent system.

Importing the package stays light: ``agent_system.utils.id`` and friends must
not drag the vector store's backend wiring along. The names re-exported here
are resolved on first use (PEP 562).
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

#: Re-exported name -> the submodule that defines it.
_LAZY = {
    "atomic_write_text": "agent_system.utils.io",
    "VectorStore": "agent_system.utils.vector_store",
    "VectorStoreError": "agent_system.utils.vector_store",
    "get_vector_backend": "agent_system.utils.vector_store",
    "get_available_onnx_providers": "agent_system.utils.vector_store",
    "create_vector_store": "agent_system.utils.vector_store",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})
