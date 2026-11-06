"""A real MCP server for the client tests to talk to.

Run as a subprocess over stdio by the tests. Using a genuine server rather
than a mock is the whole point: the previous client passed its unit tests for
years while being unable to speak to any current server, because the tests
asserted against hand-written JSON-RPC dicts instead of a real handshake.
"""
import base64 as _b64

from mcp.server.mcpserver import Context, Image, MCPServer
from pydantic import BaseModel

mcp = MCPServer("probe", instructions="Use add for sums; the notes resource says more.")


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
def die() -> str:
    """Ends the server process mid-call, the way a native crash does (no answer, no cleanup)."""
    import os

    os._exit(3)


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


@mcp.tool()
def resource_only():
    """Only an embedded resource, no text block: the result goes out as the
    server's structure, URL field included."""
    from mcp.types import EmbeddedResource, TextResourceContents

    return [EmbeddedResource(type="resource", resource=TextResourceContents(
        uri="file:///probe/notes.txt", mime_type="text/plain", text="a note"))]


@mcp.resource("probe://notes", mime_type="text/plain")
def notes() -> str:
    """Notes about the probe."""
    return "the probe adds numbers"


@mcp.resource("probe://item/{item_id}")
def item(item_id: str) -> str:
    """One item, by its id."""
    return f"item {item_id}"


@mcp.prompt()
def greet(name: str) -> str:
    """A greeting for someone."""
    return f"Say hello to {name}."


@mcp.tool()
async def grow(ctx: Context) -> str:
    """Adds the tool 'grown' and tells the client its tools changed."""
    mcp.add_tool(lambda: "grown up", name="grown", description="Appears at run time.")
    await ctx.session.send_tool_list_changed()
    return "grew"


class _Booking(BaseModel):
    seats: int
    window: bool = False


@mcp.tool()
async def book(ctx: Context) -> str:
    """Asks the person how many seats (elicitation) and says what came back."""
    answer = await ctx.elicit("How many seats?", _Booking)
    if answer.action != "accept":
        return f"not booked: {answer.action}"
    return f"booked {answer.data.seats} seats, window={answer.data.window}"


class _Yes(BaseModel):
    ok: bool


@mcp.tool()
async def pester(times: int, ctx: Context) -> str:
    """Asks the person `times` times in a row; says what each answer was."""
    return ",".join([(await ctx.elicit(f"Question {i}?", _Yes)).action for i in range(times)])


@mcp.tool()
async def ask_twice_at_once(ctx: Context) -> str:
    """Asks the person two things at the same moment; says what each answer was."""
    import asyncio

    answers = await asyncio.gather(ctx.elicit("First?", _Yes), ctx.elicit("Second?", _Yes))
    return ",".join(sorted(a.action for a in answers))



_LATER: list = []


@mcp.tool()
async def ask_later(ctx: Context) -> str:
    """Starts a question to the person and answers the call before it is answered."""
    import asyncio

    _LATER.append(asyncio.create_task(ctx.elicit("Still there?", _Yes)))
    await asyncio.sleep(0.5)
    return "answered before the question"


if __name__ == "__main__":
    mcp.run(transport="stdio")
