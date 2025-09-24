from __future__ import annotations

import uuid
# typing imports omitted - keep this module tiny


def short_id(length: int = 12) -> str:
    """Return a short unique id.

    Defaults to 12 hex characters (48 bits). This is compact and still reasonably collision-resistant
    for typical local development and debugging use-cases.
    """
    # Default to base36 representation for shorter readable ids
    # Map the requested hex-length to a base36 length approximately
    # If caller provided a length <= 12, use base36 with proportionally smaller length
    # Convert requested length (hex chars) to approximate base36 chars
    # 1 hex char ~ 4 bits, 1 base36 char ~ log2(36)=~5.17 bits -> base36 shorter
    # We'll default to 10 base36 chars for reasonable compactness unless overridden
    if length <= 12:
        return short_id_base36(10)
    # Fallback: return hex slice
    h = uuid.uuid4().hex
    return h[:length]


def short_id_base36(length: int = 10) -> str:
    """Return a base36-encoded short id (default 10 chars).

    This produces a shorter string for the same entropy when needed.
    """
    n = uuid.uuid4().int
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while n and len(out) < length:
        n, r = divmod(n, 36)
        out.append(alphabet[r])
    return ''.join(reversed(out)) or '0'
