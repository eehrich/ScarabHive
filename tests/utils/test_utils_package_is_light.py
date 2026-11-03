"""Importing a small helper of agent_system.utils must not load the vector store.

The package used to import vector_store eagerly, so ``import agent_system.utils.id`` pulled the
vector backend wiring (and filelock) into every process that only wanted an id.
"""
from __future__ import annotations

import subprocess
import sys


def test_a_helper_does_not_load_the_vector_store():
    probe = ("import sys, agent_system.utils.id; "
             "print(any(m.startswith('agent_system.utils.vector_store') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_the_package_names_are_still_there():
    import agent_system.utils as utils
    from agent_system.utils.io import atomic_write_text
    from agent_system.utils.vector_store import VectorStore, create_vector_store

    assert utils.atomic_write_text is atomic_write_text
    assert utils.VectorStore is VectorStore and utils.create_vector_store is create_vector_store
    assert "VectorStoreError" in dir(utils)
