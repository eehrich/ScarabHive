"""One TLS context per process instead of one per HTTP client.

``httpx.AsyncClient(verify=True)`` builds a fresh ``ssl.SSLContext`` and loads
the CA bundle every time -- measured 2026-09-04 at 160-170 ms per client on
the dev box. The httpx LLM clients open a NEW AsyncClient per request, so
every single LLM call paid that, and a plain ``agent-cli run`` built eleven
contexts before its one LLM call. Passing a ready ``ssl.SSLContext`` as
``verify=`` costs 0.1 ms.

Sharing is safe as long as every client runs the same HTTP/2 setting: the
context itself is not written after setup, but httpcore sets the ALPN
protocols on it at every connect. All clients here speak HTTP/1.1 (the one
explicit transport passes ``http2=False``), so every writer sets the same
value. If a client ever enables h2, key the cache by ``(verify, http2)``.

Trust store: the verifying context trusts the OS store AND certifi -- the
union of what the two conventions in this repo trusted before (httpx's
default is certifi only; the Responses client used ``ssl.create_default_context``,
i.e. the OS store), so a CA that only one of them knew keeps working.

That union stops where an operator has NARROWED the trust base: with
SSL_CERT_FILE or SSL_CERT_DIR set, httpx trusts exactly what is in there and
nothing else, and adding the OS store would silently undo it. Measured on the
dev box (2026-09-04, SSL_CERT_FILE pointing at a one-certificate bundle):
httpx alone 1 CA, plus ``load_default_certs`` 115. Widening a deliberately
narrow trust base is not a performance decision to make on the side.

``verify=False`` keeps its meaning (no certificate check) -- it just stops
building a throwaway context for it too.
"""
from __future__ import annotations

import functools
import os
import ssl

import httpx


def _trust_is_pinned() -> bool:
    """Has the environment pinned the trust base (SSL_CERT_FILE/SSL_CERT_DIR)?"""
    return bool(os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"))


@functools.lru_cache(maxsize=None)
def _context(verify: bool | str) -> ssl.SSLContext:
    # Exactly the context httpx builds per client (certifi for True,
    # CERT_NONE without hostname check for False, a CA file or directory
    # for a str) ...
    ctx = httpx.create_ssl_context(verify=verify)
    if verify is True and not _trust_is_pinned():
        # ... plus the OS trust store on top, see the module docstring.
        ctx.load_default_certs()
    return ctx


def httpx_verify(verify: bool | str | ssl.SSLContext | None = True) -> ssl.SSLContext:
    """What to hand to ``httpx.AsyncClient(verify=...)``: a shared context.

    ``None`` means "httpx default", i.e. verify. A context passed in is
    returned as is (a caller that built its own keeps it).
    """
    if isinstance(verify, ssl.SSLContext):
        return verify
    if verify is None:
        verify = True
    return _context(verify)
