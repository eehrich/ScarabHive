"""(method, path) of every HTTP route an app serves, on every fastapi version the tests run on.

Walking ``app.routes`` misses every route of an included router since fastapi 0.13x: the
router stays ONE entry there (``_IncludedRouter``) instead of being copied route by route.
The OpenAPI schema lists them on every version. Path parameters appear without their
converter (``{name}``, not ``{name:path}``); a route with ``include_in_schema=False`` is
not listed.
"""
from __future__ import annotations

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def http_routes(app) -> set[tuple[str, str]]:
    return {(method.upper(), path)
            for path, operations in app.openapi()["paths"].items()
            for method in operations if method in HTTP_METHODS}
