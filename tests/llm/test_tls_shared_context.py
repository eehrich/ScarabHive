"""One TLS context per process, reused by every httpx client on the LLM path.

Measured 2026-09-04: ``httpx.AsyncClient(verify=True)`` cost 160-170 ms per
client (CA bundle load); the httpx LLM clients open a new AsyncClient per
request, so every LLM call paid it. With a shared ``ssl.SSLContext`` it is
0.1 ms.
"""
from __future__ import annotations

import re
import ssl
from pathlib import Path

import httpx
import pytest

from agent_system.llm import tls
from agent_system.llm.tls import httpx_verify
from llm_provider_dirs import llm_provider_dirs

REPO = Path(__file__).resolve().parents[2]


def test_clients_share_one_verifying_context(monkeypatch):
    loads = []
    original = ssl.SSLContext.load_verify_locations

    def counting(self, *args, **kwargs):
        loads.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", counting)
    tls._context.cache_clear()

    shared = httpx_verify()
    loads_for_the_build = len(loads)
    assert loads_for_the_build >= 1, "fixture: building the context loaded no CA bundle at all"

    clients = [httpx.AsyncClient(verify=httpx_verify()) for _ in range(3)]

    assert httpx_verify() is shared is httpx_verify(True) is httpx_verify(None)
    assert len(loads) == loads_for_the_build, (
        f"CA bundle loaded {len(loads) - loads_for_the_build} more times for 3 clients")
    assert all(isinstance(c, httpx.AsyncClient) for c in clients)


@pytest.mark.asyncio
async def test_the_streaming_transport_carries_the_shared_context(monkeypatch):
    """The agents' path: the httpx client streams through an explicit
    ``AsyncHTTPTransport``. httpx does NOT merge a transport with the
    client's verify -- the transport wins -- so the context has to reach the
    transport itself, or every streaming call builds its own (measured: one
    CA-bundle load per request)."""
    from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient

    seen = []

    class RecordingTransport:
        def __init__(self, **kwargs):
            seen.append(kwargs)
            raise RuntimeError("stop before any network")

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", RecordingTransport)
    client = HTTPXOpenAIClient(model="m", api_key="k", max_retries=0, verify=True)

    try:
        async for _ in client._make_request_streaming(messages=[{"role": "user", "content": "hi"}], tools=[]):
            pass
    except Exception:
        pass

    assert seen, "fixture: the streaming path never built its transport"
    assert seen[0].get("verify") is httpx_verify(True)


def test_verify_false_still_disables_verification():
    ctx = httpx_verify(False)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
    assert httpx_verify(False) is ctx
    assert ctx is not httpx_verify(True)


def test_the_verifying_context_also_trusts_the_os_store(monkeypatch):
    """Union of the two trust stores this repo used: certifi (httpx's default)
    plus the OS store (what the Responses client had). Only the verifying
    context loads it -- a no-verify context has nothing to trust."""
    calls = []
    original = ssl.SSLContext.load_default_certs

    def counting(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", counting)
    # A pinned trust base keeps the OS store out (the test below); a proxy's CA pins it in many containers.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    tls._context.cache_clear()

    httpx_verify(True)
    assert len(calls) == 1, "the verifying context did not load the OS trust store"
    httpx_verify(False)
    assert len(calls) == 1, "a no-verify context must not load a trust store"
    tls._context.cache_clear()


def test_a_pinned_trust_base_is_not_widened_by_the_os_store(monkeypatch, tmp_path):
    """SSL_CERT_FILE means "trust exactly this" -- adding the OS store on top
    would silently undo it. Measured on the dev box: httpx alone honours the
    pin (1 CA), plus load_default_certs it is 115."""
    import certifi

    one_cert = tmp_path / "one.pem"
    bundle = Path(certifi.where()).read_text(encoding="utf-8")
    one_cert.write_text(
        bundle.split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n",
        encoding="utf-8")

    monkeypatch.setenv("SSL_CERT_FILE", str(one_cert))
    tls._context.cache_clear()
    pinned = len(httpx_verify(True).get_ca_certs())

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    tls._context.cache_clear()
    wide = len(httpx_verify(True).get_ca_certs())
    tls._context.cache_clear()

    assert pinned == 1, f"the pinned trust base was widened to {pinned} CAs"
    assert wide > pinned, (
        f"fixture: without the pin the context has {wide} CAs -- that is not more than "
        f"the pinned {pinned}, so this test cannot tell the two apart")


@pytest.mark.asyncio
async def test_the_non_streaming_client_carries_the_shared_context(monkeypatch):
    """The other production path of the httpx client, taken when a model's
    capabilities disable streaming: ``_make_request_non_streaming`` builds
    ``httpx.AsyncClient(**client_kwargs)`` -- the guard test below cannot see
    inside a splat, so the kwargs are measured here. (``_make_request``
    itself streams by default and would record the streaming client.)"""
    from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient

    seen = []

    class RecordingClient:
        def __init__(self, **kwargs):
            seen.append(kwargs)
            raise RuntimeError("stop before any network")

    monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)
    client = HTTPXOpenAIClient(model="m", api_key="k", max_retries=0, verify=True)

    try:
        await client._make_request_non_streaming([{"role": "user", "content": "hi"}], [], None, None)
    except Exception:
        pass

    assert seen, "fixture: the non-streaming path never built its client"
    assert "transport" not in seen[0], "fixture: this recorded the streaming client, not the non-streaming one"
    assert seen[0].get("verify") is httpx_verify(True)


def test_a_context_passed_in_is_returned_untouched():
    own = ssl.create_default_context()
    assert httpx_verify(own) is own


def test_the_httpx_openai_client_hands_the_shared_context_to_httpx():
    """Production path: the client's normalised verify value IS the shared
    context, for both settings -- not a private copy built per client."""
    from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient

    verifying = HTTPXOpenAIClient(model="m", api_key="k", verify=True)
    default = HTTPXOpenAIClient(model="m", api_key="k")
    insecure = HTTPXOpenAIClient(model="m", api_key="k", verify=False)

    assert verifying._verify is httpx_verify(True)
    assert default._verify is httpx_verify(True)
    assert insecure._verify is httpx_verify(False)


def test_every_async_client_on_the_llm_path_passes_verify():
    """Anti-drift: a new ``httpx.AsyncClient(...)`` or ``httpx.AsyncHTTPTransport(...)``
    in agent_system or an LLM provider plugin without ``verify=`` silently
    brings the per-client context back (a transport passed to a client REPLACES the
    client's verify -- httpx does not merge them). Scans the balanced call
    span, so multi-line calls count. Only the module-qualified constructor is
    a construction site (``AnthropicAsyncClient(`` is a class definition; the
    module may be aliased ``_httpx`` or held as ``self._httpx``); a call that
    splats ``**kwargs`` builds its verify value elsewhere -- the two such
    calls (httpx_client's streaming transport and non-streaming client) are
    measured by the recorder tests above."""
    offenders = []
    sites = 0
    # A literal value is as bad as a missing one: verify=True builds a fresh
    # context per client again, and that is exactly the code this change
    # removed. Only a value that comes from somewhere else (httpx_verify, or a
    # field fed by it) passes.
    literal = re.compile(r"""verify\s*=\s*(True|False|["'])""")
    # The LLM path, not every plugin. The providers lost their own root on
    # 2026-09-20 and their manifests pick them out now. All of src/plugins
    # would pull in mcp_client, whose verify=False is a deliberate
    # per-server opt-out and has nothing to do with this context.
    # llm_common is named by hand: it is the providers' shared code, and not
    # a provider, so no manifest picks it -- the move to manifests dropped it
    # from this scan without a word. It builds no client today; the first one
    # it builds is what this line is for.
    for pkg_root in [REPO / "src" / "agent_system", REPO / "src" / "plugins" / "llm_common",
                     *llm_provider_dirs()]:
        for py in pkg_root.rglob("*.py"):
            if "tests" in py.parts or py.name == "tls.py":
                continue
            text = py.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r"httpx\.(AsyncClient|Client|AsyncHTTPTransport|HTTPTransport)\(", text):
                sites += 1
                depth, i = 1, match.end()
                while i < len(text) and depth:
                    depth += {"(": 1, ")": -1}.get(text[i], 0)
                    i += 1
                span = text[match.end():i]
                line = text.count("\n", 0, match.start()) + 1
                if "verify=" not in span and "**" not in span:
                    offenders.append(f"{py.relative_to(REPO)}:{line} (no verify=)")
                elif literal.search(span):
                    offenders.append(f"{py.relative_to(REPO)}:{line} (literal verify=, not the shared context)")
    # A scanner that finds no construction site at all is green for the wrong
    # reason -- rename the module alias and this whole test goes blind.
    # A count, not a bool: losing a whole root stayed invisible to `assert
    # sites` as long as one site anywhere survived. 17 measured 2026-09-20.
    assert sites >= 17, (
        f"only {sites} httpx construction sites found, 17 measured — the "
        f"scan pattern or the root list no longer matches the code")
    assert not offenders, "\n".join(offenders)
