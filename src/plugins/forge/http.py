"""One HTTP client per forge host, and the errors every backend raises.

Both platforms take the token as ``Authorization: Bearer`` (GitLab accepts a
personal access token that way too). That header, unlike GitLab's own
``PRIVATE-TOKEN``, is dropped by httpx when a redirect leaves the host -- and
GitHub answers a job log with a redirect to its blob storage.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx


class ForgeError(Exception):
    """A refusal or failure the model can act on; the text says how.

    Never carries the token or a header."""


class ForgeNotFound(ForgeError):
    """The platform answered 404: no such object, or the token may not see it."""


class ForgeUnavailable(ForgeError):
    """The platform cannot answer right now: not reachable, a server error or
    a rate limit. Worth waiting for, unlike a refused token."""


def _message(response: httpx.Response) -> str:
    """The platform's own words: GitLab's ``message``/``error`` (a string, a
    list or a dict of field errors), GitHub's ``message`` plus ``errors``."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:200]
    if not isinstance(body, dict):
        return str(body)[:200]
    parts: list[str] = []
    for key in ("message", "error", "error_description"):
        value = body.get(key)
        if isinstance(value, dict):
            parts += [f"{field}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for field, v in value.items()]
        elif isinstance(value, list):
            parts += [str(v) for v in value]
        elif value:
            parts.append(str(value))
    for error in body.get("errors") or []:
        parts.append(str(error.get("message") or error.get("code") or error) if isinstance(error, dict) else str(error))
    return "; ".join(parts)[:300] or f"HTTP {response.status_code}"


class Api:
    """A thin httpx wrapper: JSON in and out, errors as ForgeError."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0, verify: Any = True,
                 headers: Optional[dict] = None, transport: Optional[httpx.AsyncBaseTransport] = None,
                 token_env: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token_env = token_env
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, verify=verify, transport=transport, follow_redirects=True,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "ScarabHive-forge", **(headers or {})})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, *, params: Optional[dict] = None, json: Any = None,
                      text: bool = False) -> Any:
        """The decoded answer: JSON, or the body as text with ``text=True``."""
        response = await self._send(method, path, params=params, json=json)
        if text:
            return response.text
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise ForgeError(f"{method} {path} answered something that is not JSON -- does api_url point at the "
                             f"API itself, not a login page?") from None

    async def get_all(self, path: str, params: Optional[dict] = None, *, limit: int = 100,
                      key: Optional[str] = None) -> list:
        """Up to ``limit`` items, page by page. Numbered pages, not the ``Link``
        header: behind a proxy its URLs name the platform's external host, which
        need not be ``api_url``. ``key``: the list sits in that field of an object
        (GitHub's check-runs)."""
        per_page = max(1, min(limit, 100))
        items: list = []
        for page_no in range(1, limit // per_page + 2):
            response = await self._send("GET", path, params={**(params or {}), "per_page": per_page, "page": page_no})
            page = response.json() if response.content else []
            if key is not None and isinstance(page, dict):
                page = page.get(key)
            if not isinstance(page, list):
                raise ForgeError(f"GET {path} answered no list")
            items += page
            if len(page) < per_page or len(items) >= limit:
                break
        return items[:limit]

    async def _send(self, method: str, path: str, *, params: Optional[dict] = None, json: Any = None) -> httpx.Response:
        try:
            response = await self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise ForgeUnavailable(f"{self.base_url} not reachable: {type(exc).__name__}: {exc}"[:300]) from None
        status = response.status_code
        if status < 400:
            return response
        where = f"{method} {path.split('?')[0]}"
        message = _message(response)
        if status == 401:
            raise ForgeError(f"{where}: the token was refused (401) -- check {self.token_env or 'the token'}")
        if status == 404:
            raise ForgeNotFound(f"{where}: not found (404) -- or the token may not see it")
        # GitHub's secondary limit is a 403 with retry-after but a quota left.
        if status == 429 or (status == 403 and (response.headers.get("x-ratelimit-remaining") == "0"
                                                or response.headers.get("retry-after"))):
            raise ForgeUnavailable(f"{where}: rate limited ({status}), retry after "
                                   f"{response.headers.get('retry-after') or 'a while'}: {message}")
        if status >= 500:
            raise ForgeUnavailable(f"{where}: server error {status}: {message}")
        if status == 403:
            raise ForgeError(f"{where}: forbidden (403) -- the token's role or scope does not allow this: {message}")
        raise ForgeError(f"{where}: refused ({status}): {message}")
