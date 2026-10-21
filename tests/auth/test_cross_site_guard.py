"""The sign-in cookie acts only from this origin's own pages (auth/cross_site.py): another port on the same host
is the same site, so SameSite=Lax hands its pages the cookie for every request."""
import pytest

from agent_system.auth.cross_site import CrossSiteGuard

HOST = "hive.local:8000"
COOKIE = ("cookie", "theme=dark; access_token=jwt")


async def call(method="POST", headers=(), trusted=(), host=HOST):
    reached, sent = [], []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/api/undo", "method": method, "query_string": b"",
             "headers": ([(b"host", host.encode())] if host else []) + [(k.encode(), v.encode()) for k, v in headers]}
    await CrossSiteGuard(inner, trusted)(scope, receive, send)
    status = next((m.get("status") for m in sent if m["type"] == "http.response.start"), None)
    return bool(reached), status


@pytest.mark.parametrize("method,headers,passes", [
    ("POST", [COOKIE, ("sec-fetch-site", "same-origin")], True),
    ("POST", [COOKIE, ("sec-fetch-site", "none")], True),                       # the user's own navigation
    ("POST", [COOKIE, ("sec-fetch-site", "same-site"), ("origin", "http://hive.local:3000")], False),  # other port
    ("DELETE", [COOKIE, ("sec-fetch-site", "cross-site")], False),
    ("PATCH", [COOKIE, ("sec-fetch-site", "same-site")], False),
    ("PUT", [COOKIE, ("sec-fetch-site", "same-site")], False),
    ("GET", [COOKIE, ("sec-fetch-site", "cross-site")], True),                   # changes nothing
    ("HEAD", [COOKIE, ("sec-fetch-site", "cross-site")], True),
    ("OPTIONS", [COOKIE, ("sec-fetch-site", "cross-site")], True),               # a CORS preflight
    ("POST", [("sec-fetch-site", "cross-site")], True),                          # no cookie: nobody to act as
    ("POST", [("cookie", "theme=dark"), ("sec-fetch-site", "cross-site")], True),
    ("POST", [COOKIE, ("authorization", "Bearer abc"), ("sec-fetch-site", "cross-site")], True),  # not the cookie
    ("POST", [COOKIE, ("authorization", "Bearer  "), ("sec-fetch-site", "cross-site")], False),   # empty: it is
    ("POST", [COOKIE, ("authorization", "Basic YTpi"), ("sec-fetch-site", "cross-site")], False),
    ("POST", [COOKIE, ("origin", f"http://{HOST}")], True),                      # an older browser, same origin
    ("POST", [COOKIE, ("origin", "http://hive.local:3000")], False),             # an older browser, other port
    ("POST", [COOKIE, ("origin", "null")], False),                               # a sandboxed frame
    ("POST", [COOKIE, ("origin", "http://hive.example"), ("x-forwarded-host", "hive.example")], True),  # a proxy
    ("POST", [COOKIE, ("origin", "http://hive.local:3000"), ("x-forwarded-host", "hive.example")], False),
    ("POST", [COOKIE, ("origin", "http://hive.example"), ("x-forwarded-host", "hive.example, inner:8000")], True),
    ("POST", [COOKIE], True),                                                    # not a browser
])
async def test_a_cookie_request_must_come_from_this_origin(method, headers, passes):
    reached, status = await call(method, headers)
    assert reached is passes and (status is None if passes else status == 403)


async def test_the_credentialed_cors_allowlist_may_send_it():
    headers = [COOKIE, ("sec-fetch-site", "same-site"), ("origin", "http://hive.local:3000")]
    assert (await call(headers=headers, trusted=["http://hive.local:3000/"]))[0] is True
    assert (await call(headers=headers, trusted=["*"]))[0] is False  # a wildcard never gets credentials


async def test_a_null_origin_fails_also_without_a_host():
    assert (await call(headers=[COOKIE, ("origin", "null")], host=""))[0] is False


def test_build_app_installs_it_with_the_credentialed_allowlist_only():
    from fastapi import FastAPI

    from agent_system.auth.cross_site import install
    from agent_system.config.models.auth import AuthConfig

    def trusted(**auth):
        app = FastAPI()
        install(app, AuthConfig(**auth))
        [middleware] = app.user_middleware
        assert middleware.cls is CrossSiteGuard
        return middleware.kwargs["trusted"]

    assert list(trusted(cors_origins=["http://a:1"], cors_credentials=True)) == ["http://a:1"]
    assert list(trusted(cors_origins=["http://a:1"], cors_credentials=False)) == []
    assert list(trusted(cors_origins=["http://a:1"], cors_credentials=True, cors_enabled=False)) == []
    assert list(trusted(cors_origins=["*", "http://a:1"], cors_credentials=True)) == []  # CORS drops them too


def test_build_app_installs_the_guard():
    import inspect

    from agent_system import app as app_module

    assert "install_cross_site_guard(app, config.auth)" in inspect.getsource(app_module.build_app)
