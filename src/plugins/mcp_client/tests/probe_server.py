"""A real MCP server for the client tests to talk to.

Run as a subprocess over stdio by the tests. Using a genuine server rather
than a mock is the whole point: the previous client passed its unit tests for
years while being unable to speak to any current server, because the tests
asserted against hand-written JSON-RPC dicts instead of a real handshake.
"""
from mcp.server.fastmcp import FastMCP

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


if __name__ == "__main__":
    mcp.run(transport="stdio")
