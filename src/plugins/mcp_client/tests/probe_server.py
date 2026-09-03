"""A real MCP server for the client tests to talk to.

Run as a subprocess over stdio by the tests. Using a genuine server rather
than a mock is the whole point: the previous client passed its unit tests for
years while being unable to speak to any current server, because the tests
asserted against hand-written JSON-RPC dicts instead of a real handshake.
"""
import base64 as _b64

from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("probe")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@mcp.tool()
def boom() -> str:
    """Always fails, so error handling can be exercised."""
    raise ValueError("intentional failure")


@mcp.tool()
async def sleep(seconds: float) -> str:
    """Takes its time, so head-of-line blocking becomes visible."""
    import asyncio

    await asyncio.sleep(seconds)
    return "slept"


# 1x1 red PNG, the smallest real image a tool can hand back. Base64 keeps
# the source ASCII-clean -- an escaped bytes literal got mangled once.
_TINY_PNG = _b64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNg"
    "+M/AAAACAQEAqCJhkAAAAABJRU5ErkJggg=="
)


@mcp.tool()
def picture() -> Image:
    """Return a tiny image, so image content blocks can be exercised."""
    return Image(data=_TINY_PNG, format="png")


@mcp.tool()
def captioned_picture() -> list:
    """Text AND image in one result -- both halves must survive."""
    return ["a red pixel", Image(data=_TINY_PNG, format="png")]


if __name__ == "__main__":
    mcp.run(transport="stdio")
