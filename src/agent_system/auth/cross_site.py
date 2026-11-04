"""A page of another origin cannot act with the signed-in account's cookie.

The auth cookie is SameSite=Lax: a browser sends it along from another *site*'s page only for a top-level GET.
But another *origin* of the same site -- another port on this host, a sibling subdomain -- is same-site, and its
pages get the cookie sent with every request, a form POST or a fetch included. Such a page could undo, delete or
post as the user; it only cannot read the answers (CORS). This guard refuses a state-changing request that the
cookie authenticates unless the browser says it comes from this origin.

How a request is judged, as Go's ``net/http`` CrossOriginProtection does:

- GET, HEAD and OPTIONS pass: they change nothing.
- Without the ``access_token`` cookie, or with a Bearer value (then the cookie is not what authenticates, see
  ``dependencies._authenticate``), it passes.
- An ``Origin`` in ``trusted`` (the credentialed CORS allowlist) passes.
- ``Sec-Fetch-Site``, which current browsers send to HTTPS and localhost addresses: ``same-origin`` and ``none``
  (the user's own navigation) pass, ``same-site`` and ``cross-site`` do not.
- Without it -- plain HTTP to another address, or an older browser -- ``Origin`` decides: its host and port must
  be the request's ``Host``, or the ``X-Forwarded-Host`` a proxy that rewrites ``Host`` sets (a page cannot set
  that header on a form or a no-cors fetch). ``Origin: null`` (a sandboxed frame, a file) fails, also when the
  request names no host. A plain-HTTP proxy that rewrites ``Host`` and sets no ``X-Forwarded-Host`` gets every
  write refused: it has to pass one of the two.
- Neither header: not a browser, or a same-origin request of a browser that sends neither. It passes.
"""
from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from agent_system.auth.credentials import access_token_cookie, bearer_credentials, first_headers

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: what a browser sends when the user navigated themselves, or the page is of this origin
OWN = frozenset({"same-origin", "none"})


def install(app: Any, auth: Any) -> None:
    """Origins the credentialed CORS allowlist names may send it too: ``auth.cors_origins`` with
    ``auth.cors_credentials``, as ``configure_cors`` grants it -- a ``*`` among them drops the credentials."""
    credentialed = auth.cors_enabled and auth.cors_credentials and "*" not in auth.cors_origins
    trusted = auth.cors_origins if credentialed else ()
    app.add_middleware(CrossSiteGuard, trusted=trusted)


class CrossSiteGuard:
    def __init__(self, app: Any, trusted: Iterable[str] = ()) -> None:
        self.app = app
        self.trusted = frozenset(origin.rstrip("/").lower() for origin in trusted)  # a "*" matches no Origin

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("method") not in SAFE_METHODS and not self.allowed(scope):
            await JSONResponse({"detail": "Cross-origin request refused: it carries the sign-in cookie, but the "
                                          "browser says it does not come from this site's own pages (behind a proxy: "
                                          "pass Host, or set X-Forwarded-Host)."},
                               status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def allowed(self, scope: dict[str, Any]) -> bool:
        headers = first_headers(scope)  # the first of a repeated header, as Starlette reads
        if bearer_credentials(headers):
            return True
        if not access_token_cookie(headers):
            return True
        origin = headers.get(b"origin", "").strip().rstrip("/").lower()
        if origin in self.trusted:
            return True
        site = headers.get(b"sec-fetch-site")
        if site is not None:
            return site.strip().lower() in OWN
        if not origin:
            return True
        netloc = urlsplit(origin).netloc  # empty for "null"
        hosts = {headers.get(name, "").split(",")[0].strip().lower() for name in (b"host", b"x-forwarded-host")}
        return bool(netloc) and netloc in hosts
