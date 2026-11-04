"""What a request carries to sign in with, read off the raw ASGI scope.

For the layers that run before the route dependencies and must read the
credentials exactly as those do (``fastapi.security.HTTPBearer`` and
``request.cookies``, both on Starlette): the endpoint security and the audit
(auth/middleware/), and the cross-site guard (auth/cross_site.py). Read two
ways, the layers named different users for one request -- ``bearer <key>``,
a second Authorization header next to a cookie, two ``access_token`` cookies.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from starlette.requests import cookie_parser

#: The cookie a browser session signs in with.
ACCESS_TOKEN_COOKIE = "access_token"


def first_headers(scope: Dict[str, Any]) -> Dict[bytes, str]:
    """The request's headers by their (lower-case) name, the first of a repeated one -- as Starlette's
    ``Headers.get`` reads them -- decoded latin-1, as Starlette decodes them."""
    headers: Dict[bytes, str] = {}
    for name, value in scope.get("headers", []):
        headers.setdefault(name, value.decode("latin-1"))
    return headers


def bearer_credentials(headers: Dict[bytes, str]) -> Optional[str]:
    """The value of ``Authorization: Bearer <value>`` -- the scheme in any case, as HTTPBearer takes it --
    or None (no such header, another scheme, an empty value). A JWT or an API key (``security.bearer_api_key``)."""
    scheme, _, credentials = headers.get(b"authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credentials.strip() or None


def access_token_cookie(headers: Dict[bytes, str]) -> Optional[str]:
    """The ``access_token`` cookie, or None. Parsed by the parser behind ``request.cookies``, so of two such
    cookies every layer takes the same one (the last)."""
    return cookie_parser(headers.get(b"cookie", "")).get(ACCESS_TOKEN_COOKIE) or None
