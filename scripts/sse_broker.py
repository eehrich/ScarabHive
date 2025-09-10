"""Helper to run the SSE broker for local testing.

Provides a `main()` entrypoint so this script can be used directly during
development.
"""
from agent_system.mcp.sse_broker import run


def main() -> None:
    """Console entrypoint for the SSE broker runner."""
    run()


if __name__ == "__main__":
    main()
