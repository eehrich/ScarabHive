from __future__ import annotations

import uuid
# typing imports omitted - keep this module tiny


def short_id(length: int = 10) -> str:
    """Return a short unique base36 id of the requested length (default 10).

    The length argument used to be ignored for every value <= 12 -- all
    callers got 10 chars regardless (json_store sized its collision loop
    against an 8-char space that never existed; sub_agent_manager asked for
    6 and got 10). Default stays at 10 chars, the shape every default caller
    has always received.
    """
    if length <= 12:
        return short_id_base36(length)
    # Long ids: hex slice keeps the full requested length
    return uuid.uuid4().hex[:length]


def short_id_base36(length: int = 10) -> str:
    """Return a base36-encoded short id (default 10 chars).

    This produces a shorter string for the same entropy when needed.
    """
    n = uuid.uuid4().int
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    out: list[str] = []
    while n and len(out) < length:
        n, r = divmod(n, 36)
        out.append(alphabet[r])
    return ''.join(reversed(out)) or '0'
