"""Small text helpers shared by the terminal, the chat and the run views."""
from __future__ import annotations


def shorten(text: str, limit: int, *, ellipsis: str = "…") -> str:
    """*text* as it is when it fits in *limit* characters, else cut so that it
    and *ellipsis* together are *limit* long."""
    return text if len(text) <= limit else text[: limit - len(ellipsis)] + ellipsis
