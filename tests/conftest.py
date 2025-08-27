# Ensure repository root is on sys.path so tests can import top-level packages like `plugins`.
# Pytest may add the tests directory to sys.path which can shadow the project root.
import sys
import os

root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if root not in sys.path:
    sys.path.insert(0, root)
