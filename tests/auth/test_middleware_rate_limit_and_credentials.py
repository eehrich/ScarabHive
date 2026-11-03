"""The rate limiter forgets addresses that went quiet, and every layer reads the credentials one way.

Also: the middleware package still answers ``from agent_system.auth.middleware import X`` for every name.
"""
from __future__ import annotations

import pytest

from agent_system.auth.credentials import access_token_cookie, bearer_credentials, first_headers
from agent_system.auth.middleware import RateLimitMiddleware


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _call(limiter, ip):
    sent = []

    async def send(message):
        sent.append(message)

    await limiter({"type": "http", "client": (ip, 1)}, None, send)
    return sent[0]["status"]


@pytest.mark.asyncio
async def test_the_sweep_drops_addresses_quiet_for_a_minute(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("agent_system.auth.middleware.rate_limit.time.time", lambda: now[0])
    limiter = RateLimitMiddleware(_ok, requests_per_minute=5)
    for n in range(100):
        assert await _call(limiter, f"10.0.0.{n}") == 200
    assert len(limiter.request_counts) == 100

    now[0] += limiter._cleanup_interval + 1
    assert await _call(limiter, "10.0.1.1") == 200

    assert set(limiter.request_counts) == {"10.0.1.1"}


@pytest.mark.asyncio
async def test_the_sweep_keeps_an_address_still_inside_its_minute(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("agent_system.auth.middleware.rate_limit.time.time", lambda: now[0])
    limiter = RateLimitMiddleware(_ok, requests_per_minute=2)
    limiter._last_cleanup = now[0] - limiter._cleanup_interval + 10
    for _ in range(2):
        assert await _call(limiter, "10.0.0.9") == 200

    now[0] += 30  # the sweep runs now; the address sent its two 30 s ago
    assert await _call(limiter, "10.0.0.8") == 200
    assert await _call(limiter, "10.0.0.9") == 429


def _scope(*headers):
    return {"headers": [(name.encode(), value.encode("latin-1")) for name, value in headers]}


def test_the_first_of_a_repeated_header_counts():
    headers = first_headers(_scope(("authorization", "Bearer one"), ("authorization", "Bearer two")))
    assert bearer_credentials(headers) == "one"


@pytest.mark.parametrize("value, credentials", [
    ("Bearer abc", "abc"), ("bearer abc ", "abc"), ("BEARER  abc", "abc"),
    ("Bearer ", None), ("Basic abc", None), ("", None),
])
def test_the_bearer_value_is_read_as_httpbearer_reads_it(value, credentials):
    assert bearer_credentials(first_headers(_scope(("authorization", value)))) == credentials


def test_of_two_access_token_cookies_the_last_counts():
    headers = first_headers(_scope(("cookie", "access_token=first; access_token=second")))
    assert access_token_cookie(headers) == "second"
    assert access_token_cookie(first_headers(_scope(("cookie", "other=1")))) is None


def test_the_package_answers_every_name_the_module_did():
    from agent_system.auth import middleware

    for name in ("AUDIT_CATEGORIES", "AUDIT_LOG_PATH", "AUDIT_LOGGER_NAME", "AUDIT_STATUS_CLASSES",
                 "IDENTITY_SCOPE_KEY", "ROLE_HIERARCHY", "EndpointSecurityMiddleware", "RateLimitMiddleware",
                 "SecurityAuditMiddleware", "SecurityHeadersMiddleware", "configure_cors",
                 "configure_security_middleware", "get_security_audit_middleware", "log_field",
                 "security_audit_log", "security_audit_logger"):
        assert hasattr(middleware, name), name
