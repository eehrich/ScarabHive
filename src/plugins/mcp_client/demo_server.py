"""A small MCP server whose tools ask the person something (elicitation), for trying it out.

Declared in config/mcp_servers.yaml as ``feature_demo`` and used by the ``feature_demo_agent``.
It runs over stdio as ``python src/plugins/mcp_client/demo_server.py`` and needs only the ``mcp``
package. Nothing it is told leaves the process: the tools only say back what they heard.
"""
import datetime
import uuid
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, Field

mcp = MCPServer("feature_demo", instructions="Demo tools that ask the person in the chat before they answer.")


class _Booking(BaseModel):
    day: datetime.date = Field(description="Which day (YYYY-MM-DD)")
    guests: int = Field(ge=1, le=12, description="How many people")
    seating: Literal["window", "terrace", "inside"] = Field(description="Where to sit")
    note: str = Field(default="", max_length=200, description="Anything the kitchen should know")


@mcp.tool()
async def book_table(restaurant: str, ctx: Context) -> str:
    """Books a table (pretend) -- asks the person for day, guests, seating and a note."""
    answer = await ctx.elicit(f"Booking a table at {restaurant}.", _Booking)
    if answer.action != "accept":
        return f"Nothing booked: the person answered {answer.action}."
    b = answer.data
    return (f"Booked (pretend) at {restaurant}: {b.day.isoformat()}, {b.guests} guest(s), {b.seating}"
            + (f", note: {b.note}" if b.note else "") + ".")


class _Pick(BaseModel):
    toppings: list[Literal["cheese", "mushrooms", "olives", "basil", "chili"]] = Field(
        min_length=1, max_length=3, description="One to three toppings")


@mcp.tool()
async def order_pizza(ctx: Context) -> str:
    """Orders a pizza (pretend) -- asks the person to pick one to three toppings."""
    answer = await ctx.elicit("Pick the toppings for your pizza.", _Pick)
    if answer.action != "accept":
        return f"No pizza: the person answered {answer.action}."
    return f"Pizza ordered (pretend) with {', '.join(answer.data.toppings)}."


@mcp.tool()
async def link_account(ctx: Context) -> str:
    """Pretends to connect an account -- sends the person to a web address (URL-mode elicitation)."""
    answer = await ctx.elicit_url("Open the page to connect your demo account.",
                                  "https://example.com/connect-demo", str(uuid.uuid4()))
    return f"The person answered {answer.action}." + (" (pretend) connected." if answer.action == "accept" else "")


if __name__ == "__main__":
    mcp.run(transport="stdio")
