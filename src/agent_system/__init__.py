try:
    from importlib.metadata import version
    __version__ = version("agent-system-mcp")
except Exception:
    __version__ = "unknown"

__all__ = ["__version__"]
