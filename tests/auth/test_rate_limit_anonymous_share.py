"""auth.anonymous_access.rate_limit_multiplier: a request without credentials gets its share of the limit (O8).

The model accepted the value and the limiter never read it. It applies while auth and anonymous access are
on; otherwise the limit is the same for everyone, as before.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI

from agent_system.auth.middleware import RateLimitMiddleware, configure_security_middleware
from agent_system.config.models import AuthConfig
from agent_system.config.models.auth import AnonymousAccessConfig


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _call(limiter, *headers, ip="10.0.0.1"):
    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "client": (ip, 1),
             "headers": [(name.encode(), value.encode()) for name, value in headers]}
    await limiter(scope, None, send)
    return sent[0]["status"]


async def _allowed(limiter, *headers, ip="10.0.0.1"):
    """How many of 20 requests in a row pass."""
    return [await _call(limiter, *headers, ip=ip) for _ in range(20)].count(200)


@pytest.mark.asyncio
async def test_without_credentials_a_caller_gets_its_share():
    limiter = RateLimitMiddleware(_ok, requests_per_minute=10, anonymous_multiplier=0.5)

    assert await _allowed(limiter) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("credentials", [
    ("authorization", "Bearer a.jwt.value"),
    ("cookie", "access_token=a.jwt.value"),
    ("x-api-key", "sk-something"),
])
async def test_a_token_a_cookie_or_a_key_gets_the_full_limit(credentials):
    limiter = RateLimitMiddleware(_ok, requests_per_minute=10, anonymous_multiplier=0.5)

    assert await _allowed(limiter, credentials) == 10


@pytest.mark.asyncio
async def test_signed_in_requests_do_not_use_up_the_share():
    """On one count, the signed-in requests of an address pushed its login page and /health into a 429 at 5."""
    limiter = RateLimitMiddleware(_ok, requests_per_minute=10, anonymous_multiplier=0.5)
    for _ in range(9):
        assert await _call(limiter, ("authorization", "Bearer t")) == 200

    assert await _allowed(limiter) == 5
    assert await _call(limiter, ("authorization", "Bearer t")) == 200, "the anonymous requests used up the limit"
    assert await _call(limiter, ("authorization", "Bearer t")) == 429


@pytest.mark.asyncio
async def test_without_a_share_an_address_has_one_count():
    limiter = RateLimitMiddleware(_ok, requests_per_minute=10)
    for _ in range(9):
        assert await _call(limiter, ("authorization", "Bearer made-up")) == 200

    assert await _allowed(limiter) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("multiplier", [0.0, 0.01, -1.0])
async def test_the_share_is_at_least_one_request(multiplier):
    limiter = RateLimitMiddleware(_ok, requests_per_minute=10, anonymous_multiplier=multiplier)

    assert await _allowed(limiter) == 1


def _limiter_of(auth: AuthConfig) -> RateLimitMiddleware:
    app = FastAPI()
    configure_security_middleware(app, auth_config=auth, requests_per_minute=10,
                                  security_headers_enabled=False, audit_enabled=False)
    (entry,) = [m for m in app.user_middleware if m.cls is RateLimitMiddleware]
    return RateLimitMiddleware(_ok, **entry.kwargs)


@pytest.mark.parametrize("auth,share", [
    (AuthConfig(enabled=True, secret_key="k" * 32,
                anonymous_access=AnonymousAccessConfig(enabled=True, rate_limit_multiplier=0.3)), 3),
    (AuthConfig(enabled=True, secret_key="k" * 32,
                anonymous_access=AnonymousAccessConfig(enabled=False, rate_limit_multiplier=0.3)), 10),
    (AuthConfig(enabled=False, anonymous_access=AnonymousAccessConfig(enabled=True, rate_limit_multiplier=0.3)), 10),
])
def test_the_setup_hands_the_multiplier_on_while_anonymous_access_is_on(auth, share):
    assert _limiter_of(auth).anonymous_requests_per_minute == share
