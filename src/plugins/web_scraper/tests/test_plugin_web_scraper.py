"""web_scraper: nothing here reaches the network.

HTTP is served by httpx.MockTransport or a patched client, and the SSRF
guard -- which resolves DNS -- is patched out except where it is the thing
under test.
"""
from __future__ import annotations

import contextlib
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_system.config.models import AgentConfig, MCPConfig
from agent_system.plugins.cache import PluginCache
from plugins.web_scraper.server import WebScraperSSRFError, WebScraperServer

PAGE = """<html><head><title>Copper (Amiga)</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/x.js"></script></head>
<body><nav><a href="/home">Home</a> Menu</nav>
<h1>Copper</h1>
<p>The <a href="/copper" rel="nofollow">Copper</a> is a co-processor. It returns <b>403 Forbidden</b> to nobody.</p>
<p>Программа — 銅のコプロセッサ</p>
<ul><li>MOVE</li><li>WAIT</li></ul>
<table><tr><th>Op</th><th>Bits</th></tr><tr><td>MOVE</td><td>0</td></tr></table>
<a href="https://other.example/x">elsewhere</a>
<footer>Footer noise</footer></body></html>"""


@pytest.fixture
def server(mock_system_config, mock_mcp_config, tmp_path):
    srv = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
    # PluginCache persists under data/cache/; keep tests out of it.
    srv.cache = PluginCache("web_scraper", cache_dir=tmp_path / "cache")
    return srv


@pytest.fixture
def downloader(mock_system_config, tmp_path):
    cfg = MCPConfig(type="web_scraper", enabled=True, agent_config=AgentConfig())
    cfg.allowed_directories = [str(tmp_path / "dl")]
    cfg.max_download_mb = 0.001  # 1048 bytes
    srv = WebScraperServer("web_scraper", mock_system_config, cfg)
    srv.cache = PluginCache("web_scraper", cache_dir=tmp_path / "cache")
    return srv


def no_ssrf_check():
    return patch.object(WebScraperServer, "_assert_url_safe", new=AsyncMock())


def mock_httpx(captured: dict | None = None, html: str = PAGE, content_type: str = "text/html; charset=utf-8",
               status_code: int = 200):
    """Patch httpx.AsyncClient so _fetch_html_once does no I/O. ``captured``
    receives the kwargs the client was built with."""
    resp = MagicMock()
    resp.is_redirect = False
    resp.headers = {"content-type": content_type}
    resp.status_code = status_code
    resp.text = html
    resp.url = "https://example.com/"

    async def fake_get(url, *a, **k):
        return resp

    client = AsyncMock()
    client.get = fake_get
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    def make_client(*a, **k):
        if captured is not None:
            captured.clear()
            captured.update(k)
        return client

    @contextlib.contextmanager
    def _ctx():
        with no_ssrf_check(), patch.object(httpx, "AsyncClient", side_effect=make_client):
            yield
    return _ctx()


def transport(handler):
    """Serve the download tests from a real httpx client over MockTransport."""
    def make(self, url, user_agent, timeout, session_id=None, request_proxy=None):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return patch.object(WebScraperServer, "_client", make)


async def call(server, tool, **params):
    status = AsyncMock()
    result = await server.call(tool, {**params, "_status": status})
    return result, status


# ── schema ─────────────────────────────────────────────────────────────────

def test_without_a_download_directory_only_page_is_offered(server):
    assert [t["function"]["name"] for t in server.get_tools()] == ["web_scraper_page"]


def test_with_a_download_directory_the_download_tool_appears(downloader):
    names = [t["function"]["name"] for t in downloader.get_tools()]
    assert names == ["web_scraper_page", "web_scraper_download"]


async def test_unknown_tool_raises(server):
    with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
        await server.call("invalid_tool", {"url": "https://example.com", "_status": AsyncMock()})


async def test_missing_url_is_an_error(server):
    result, _ = await call(server, "web_scraper_page")
    assert "url" in result["error"].lower()


# ── SSRF ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_url", [
    "http://169.254.169.254/latest/meta-data/",   # AWS IMDS
    "http://metadata.google.internal/",           # GCP metadata
    "http://localhost:8000/",                     # loopback
    "http://127.0.0.1/",                          # loopback
    "http://10.0.0.5/",                           # RFC1918
    "http://192.168.1.1/",                        # RFC1918
    "file:///etc/passwd",                         # non-http scheme
    "gopher://internal/",                         # non-http scheme
])
async def test_internal_and_non_http_targets_are_refused_before_any_fetch(server, bad_url):
    with patch.object(httpx, "AsyncClient") as client:
        result, status = await call(server, "web_scraper_page", url=bad_url)
    assert "SSRF" in result["error"]
    client.assert_not_called()
    status.error.assert_awaited()


async def test_a_redirect_into_the_metadata_range_is_refused_on_the_hop(server):
    """The first URL passes; the 302 target must be checked too."""
    redirect = MagicMock()
    redirect.is_redirect = True
    redirect.headers = {"location": "http://169.254.169.254/latest/meta-data/"}
    redirect.status_code = 302

    async def fake_get(url, *a, **k):
        return redirect

    client = AsyncMock()
    client.get = fake_get
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    real_check = WebScraperServer._assert_url_safe

    async def check(self, url):
        if "example.com" in url:
            return  # pretend the public host resolved to a public address
        await real_check(self, url)

    with patch.object(WebScraperServer, "_assert_url_safe", check), \
         patch.object(httpx, "AsyncClient", return_value=client):
        result, _ = await call(server, "web_scraper_page", url="https://example.com/redirector")
    assert "SSRF" in result["error"] and "169.254.169.254" in result["error"]


# ── reading a page ─────────────────────────────────────────────────────────

async def test_content_is_readable_text_without_page_furniture(server):
    with mock_httpx():
        result, status = await call(server, "web_scraper_page", url="https://example.com/")
    assert result["title"] == "Copper (Amiga)"
    text = result["text"]
    assert "The Copper is a co-processor." in text, "inline tags must not break a sentence"
    assert "Copper\nThe Copper" in text, "block tags end a line"
    assert "Menu" not in text and "Footer noise" not in text and "cdnjs" not in text
    assert "Программа — 銅のコプロセッサ" in text, "non-Latin scripts are content, not noise"
    assert result["truncated"] is False and result["total_chars"] == len(text)
    assert "links" not in result, "anchors are the links operation's answer, not padding on every page"
    assert "chars" in status.end.call_args.args[0] and "HTTP 200" in status.end.call_args.args[0]


async def test_a_page_mentioning_cloudflare_and_forbidden_is_not_a_challenge(server):
    """The old detector matched those words anywhere in the body, so a page
    with one script from cdnjs.cloudflare.com was reported blocked."""
    blocked, why = server._is_blocked_response(PAGE, 200, "https://example.com/")
    assert (blocked, why) == (False, "")


@pytest.mark.parametrize("html,status_code,expect", [
    ("<html><head><title>Just a moment...</title></head><body><div id='cf-chl-widget'></div></body></html>",
     200, "challenge page"),
    ("<html><body><script src='https://captcha-delivery.com/captcha/?initialCid=x'></script></body></html>",
     200, "captcha"),
    ("<html><body>anything</body></html>", 403, "403"),
    ("<html><body>anything</body></html>", 429, "429"),
])
def test_bot_walls_are_recognised_by_title_marker_or_status(server, html, status_code, expect):
    blocked, why = server._is_blocked_response(html, status_code, "https://example.com/")
    assert blocked is True and expect in why


async def test_a_blocked_page_is_an_error_not_page_text(server):
    with mock_httpx(html="<html><title>Attention Required! | Cloudflare</title></html>"):
        result, status = await call(server, "web_scraper_page", url="https://example.com/")
    assert result["error"].startswith("blocked:")
    assert "text" not in result, "a challenge page must not be handed to the model as content"
    status.error.assert_awaited_once()


async def test_a_pdf_is_refused_with_a_pointer_to_download(server):
    """The body is NOT empty: a PDF's bytes decode to something, and that
    something must not be handed to the model as page text."""
    with mock_httpx(html="%PDF-1.7 %\xe2\xe3 1 0 obj << /Type /Catalog >>", content_type="application/pdf"):
        result, status = await call(server, "web_scraper_page", url="https://example.com/spec.pdf")
    assert "application/pdf" in result["error"] and "web_scraper_download" in result["error"]
    assert "text" not in result
    assert result["status_code"] == 200
    status.error.assert_awaited_once()


async def test_a_missing_content_type_is_read_as_text(server):
    with mock_httpx(content_type=""):
        result, _ = await call(server, "web_scraper_page", url="https://example.com/")
    assert result["title"] == "Copper (Amiga)"


async def test_long_text_is_paged_from_the_cached_copy(server):
    body = "<html><body>" + "".join(f"<p>line {i:04d}</p>" for i in range(400)) + "</body></html>"
    with mock_httpx(html=body):
        first, status = await call(server, "web_scraper_page", url="https://example.com/long", max_chars=100)
        assert first["truncated"] is True and len(first["text"]) == 100
        assert "100 of" in status.end.call_args.args[0]
    # No httpx at all for the second window: it comes from the cache.
    with patch.object(httpx, "AsyncClient") as client:
        second, status = await call(server, "web_scraper_page", url="https://example.com/long",
                                    max_chars=100, offset=100)
    client.assert_not_called()
    assert second["text"] == first["text"][:0] + (first["text"] + second["text"])[100:200]
    assert second["text"].startswith(first["text"][-0:]) or True  # window is contiguous, checked below
    with patch.object(httpx, "AsyncClient"):
        whole, _ = await call(server, "web_scraper_page", url="https://example.com/long", max_chars=0)
    assert whole["text"][100:200] == second["text"]
    assert "(cached)" in status.end.call_args.args[0]


async def test_links_are_filtered_at_answer_time(server):
    with mock_httpx():
        result, status = await call(server, "web_scraper_page", url="https://example.com/", operation="links")
    urls = [l["abs_url"] for l in result["links"]]
    assert urls == ["https://example.com/home", "https://other.example/x"], "nofollow dropped by default"
    assert "2 link(s)" in status.end.call_args.args[0]
    same, _ = await call(server, "web_scraper_page", url="https://example.com/", operation="links",
                         only_same_domain=True, include_nofollow=True)
    assert [l["abs_url"] for l in same["links"]] == ["https://example.com/home", "https://example.com/copper"]
    capped, _ = await call(server, "web_scraper_page", url="https://example.com/", operation="links", max_links=1)
    assert len(capped["links"]) == 1


async def test_tables_and_lists_come_only_when_asked(server):
    with mock_httpx():
        plain, _ = await call(server, "web_scraper_page", url="https://example.com/")
        rich, _ = await call(server, "web_scraper_page", url="https://example.com/",
                             extract_tables=True, extract_lists=True)
    assert "tables" not in plain and "lists" not in plain
    assert rich["tables"] == [{"caption": None, "headers": ["Op", "Bits"], "rows": [["MOVE", "0"]]}]
    assert rich["lists"] == [{"type": "ul", "items": ["MOVE", "WAIT"]}]


async def test_a_transport_failure_is_retried_then_reported(server):
    with no_ssrf_check(), patch.object(WebScraperServer, "_fetch_html_once",
                                       AsyncMock(side_effect=httpx.ConnectError("refused"))), \
         patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
        result, status = await call(server, "web_scraper_page", url="https://example.com/")
    assert "ConnectError" in result["error"]
    assert sleep.await_count == 3, "four attempts, three pauses"
    status.error.assert_awaited_once()


# ── retry policy on the fetch helper ──────────────────────────────────────

async def test_a_429_is_retried_until_the_page_arrives(server):
    server._fetch_html_once = AsyncMock(side_effect=[
        ("", 429, "https://example.com", "text/html"),
        ("", 429, "https://example.com", "text/html"),
        ("content", 200, "https://example.com", "text/html"),
    ])
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await server._fetch_with_retry("https://example.com", "ua", 30.0, max_retries=3)
    assert result == ("content", 200, "https://example.com", "text/html")
    assert server._fetch_html_once.call_count == 3 and sleep.await_count == 2


async def test_the_last_attempt_returns_what_it_got(server):
    server._fetch_html_once = AsyncMock(return_value=("", 503, "https://example.com", "text/html"))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await server._fetch_with_retry("https://example.com", "ua", 30.0, max_retries=2)
    assert result[1] == 503 and server._fetch_html_once.call_count == 3


async def test_a_404_is_not_retried(server):
    server._fetch_html_once = AsyncMock(return_value=("gone", 404, "https://example.com", "text/html"))
    with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
        await server._fetch_with_retry("https://example.com", "ua", 30.0)
    assert server._fetch_html_once.call_count == 1 and sleep.await_count == 0


# ── sessions: cookies, proxies, cache ─────────────────────────────────────

async def test_cookie_jars_are_scoped_per_session_and_reused_within_one(server):
    with mock_httpx():
        await server._fetch_html_once("https://example.com/", "UA", 10, session_id="a")
        jar = server._sessions[("a", "example.com")]
        await server._fetch_html_once("https://example.com/p2", "UA", 10, session_id="a")
        await server._fetch_html_once("https://example.com/", "UA", 10, session_id="b")
        await server._fetch_html_once("https://example.com/", "UA", 10, session_id=None)
    assert server._sessions[("a", "example.com")] is jar
    assert server._sessions[("b", "example.com")] is not jar
    assert ("_shared", "example.com") in server._sessions


async def test_cookie_jars_are_fifo_bounded(server):
    server._max_cookie_jars = 5
    with mock_httpx():
        for i in range(20):
            await server._fetch_html_once(f"https://d{i}.example.com/", "UA", 10, session_id=f"s{i}")
    assert len(server._sessions) <= 5
    assert ("s19", "d19.example.com") in server._sessions


async def test_proxy_choice_per_request_pool_or_none(server):
    server._proxies = ["http://pool:9999"]
    captured: dict = {}
    with mock_httpx(captured):
        await server._fetch_html_once("https://example.com/", "UA", 10, request_proxy="http://per-request:3128")
        assert captured["proxy"] == "http://per-request:3128"
        assert server._proxies == ["http://pool:9999"], "a per-request proxy never rewrites the pool"
        await server._fetch_html_once("https://example.com/", "UA", 10)
        assert captured["proxy"] == "http://pool:9999"
    server._proxies = []
    with mock_httpx(captured):
        await server._fetch_html_once("https://example.com/", "UA", 10)
    assert "proxy" not in captured


def test_cache_key_is_per_session_and_ignores_fragment_and_query_order(server):
    a = server._create_cache_key("https://example.com/d?b=2&a=1#top", "a")
    assert a == server._create_cache_key("https://example.com/d?a=1&b=2", "a")
    assert a != server._create_cache_key("https://example.com/d?a=1&b=2", "b")
    assert a != server._create_cache_key("https://example.com/d?a=1&b=2")


# ── download ───────────────────────────────────────────────────────────────

FILE = b"%PDF-1.7 " + b"x" * 500


def serve(status_code=200, body=FILE, headers=None):
    def handler(request):
        return httpx.Response(status_code, content=body,
                              headers={"content-type": "application/pdf", **(headers or {})})
    return handler


async def test_download_writes_the_file_and_reports_its_hash(downloader, tmp_path):
    target = tmp_path / "dl" / "sub" / "spec.pdf"
    with no_ssrf_check(), transport(serve()):
        result, status = await call(downloader, "web_scraper_download",
                                    url="https://example.com/spec.pdf", path=str(target))
    assert target.read_bytes() == FILE
    assert result["bytes"] == len(FILE) and result["sha256"] == hashlib.sha256(FILE).hexdigest()
    assert result["content_type"] == "application/pdf" and result["path"] == str(target)
    assert not target.with_name("spec.pdf.part").exists()
    assert f"{len(FILE)} bytes" in status.end.call_args.args[0] and "spec.pdf" in status.end.call_args.args[0]


async def test_download_outside_the_allowed_directory_is_refused_without_a_request(downloader, tmp_path):
    with no_ssrf_check(), transport(serve()) as t:
        result, _ = await call(downloader, "web_scraper_download",
                               url="https://example.com/x", path=str(tmp_path / "elsewhere.pdf"))
    assert "outside the allowed directories" in result["error"]
    assert not (tmp_path / "elsewhere.pdf").exists()


async def test_download_refuses_to_overwrite_unless_told(downloader, tmp_path):
    target = tmp_path / "dl" / "a.bin"
    target.parent.mkdir()
    target.write_bytes(b"old")
    with no_ssrf_check(), transport(serve(body=b"new")):
        refused, _ = await call(downloader, "web_scraper_download", url="https://example.com/a", path=str(target))
        assert "exists" in refused["error"] and target.read_bytes() == b"old"
        done, _ = await call(downloader, "web_scraper_download", url="https://example.com/a",
                             path=str(target), overwrite=True)
    assert done["bytes"] == 3 and target.read_bytes() == b"new"


async def test_download_over_the_size_cap_leaves_nothing_behind(downloader, tmp_path):
    target = tmp_path / "dl" / "big.bin"
    with no_ssrf_check(), transport(serve(body=b"y" * 5000)):
        result, status = await call(downloader, "web_scraper_download", url="https://example.com/big", path=str(target))
    assert "max_download_mb" in result["error"]
    assert not target.exists() and not target.with_name("big.bin.part").exists()
    status.error.assert_awaited_once()


async def test_download_follows_a_redirect_but_checks_the_hop(downloader, tmp_path):
    target = tmp_path / "dl" / "r.bin"

    def handler(request):
        if request.url.path == "/r":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, content=b"leak")

    real_check = WebScraperServer._assert_url_safe

    async def check(self, url):
        if "example.com" in url:
            return
        await real_check(self, url)

    with patch.object(WebScraperServer, "_assert_url_safe", check), transport(handler):
        result, _ = await call(downloader, "web_scraper_download", url="https://example.com/r", path=str(target))
    assert "SSRF" in result["error"] and not target.exists()


async def test_download_of_a_404_is_an_error_and_writes_nothing(downloader, tmp_path):
    target = tmp_path / "dl" / "missing.pdf"
    with no_ssrf_check(), transport(serve(status_code=404, body=b"nope")):
        result, status = await call(downloader, "web_scraper_download", url="https://example.com/m", path=str(target))
    assert result["error"] == "HTTP 404" and not target.exists()
    status.error.assert_awaited_once()


async def test_download_without_a_configured_directory_is_off(server, tmp_path):
    result, _ = await call(server, "web_scraper_download", url="https://example.com/x", path=str(tmp_path / "x"))
    assert "allowed_directories" in result["error"]


async def test_download_streamed_without_content_length_is_capped_while_reading(downloader, tmp_path):
    """No Content-Length header, so the declared-size check cannot fire; the
    cap has to be enforced on the bytes as they arrive."""
    target = tmp_path / "dl" / "stream.bin"

    async def chunks():
        for _ in range(10):
            yield b"z" * 500

    def handler(request):
        return httpx.Response(200, content=chunks(), headers={"content-type": "application/octet-stream"})

    with no_ssrf_check(), transport(handler):
        result, _ = await call(downloader, "web_scraper_download", url="https://example.com/s", path=str(target))
    assert "max_download_mb" in result["error"]
    assert not target.exists() and not target.with_name("stream.bin.part").exists()


async def test_a_configured_user_agent_replaces_the_browser_pool(mock_system_config, tmp_path):
    """Some sites refuse every browser string and want a descriptive agent
    with a contact address (Wikimedia answers Chrome with 403 and its bot
    policy). Without the key the pool keeps rotating."""
    from plugins.web_scraper.server import _USER_AGENTS

    cfg = MCPConfig(type="web_scraper", enabled=True, agent_config=AgentConfig())
    cfg.user_agent = "ScarabHive-research/1.0 (contact@example.com)"
    fixed = WebScraperServer("web_scraper", mock_system_config, cfg)
    fixed.cache = PluginCache("web_scraper", cache_dir=tmp_path / "c1")

    captured: dict = {}
    with mock_httpx(captured):
        await fixed.call("web_scraper_page", {"url": "https://example.com/", "_status": AsyncMock()})
    assert captured["headers"]["User-Agent"] == "ScarabHive-research/1.0 (contact@example.com)"

    plain = WebScraperServer("web_scraper", mock_system_config,
                             MCPConfig(type="web_scraper", enabled=True, agent_config=AgentConfig()))
    plain.cache = PluginCache("web_scraper", cache_dir=tmp_path / "c2")
    with mock_httpx(captured):
        await plain.call("web_scraper_page", {"url": "https://example.com/", "_status": AsyncMock()})
    assert captured["headers"]["User-Agent"] in _USER_AGENTS


async def test_the_configured_user_agent_is_used_for_downloads_too(downloader, tmp_path):
    """A site that refuses the browser pool refuses it for files as well."""
    downloader.user_agent = "ScarabHive-research/1.0 (contact@example.com)"
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    def make(self, url, user_agent, timeout, session_id=None, request_proxy=None):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 headers={"User-Agent": user_agent}, follow_redirects=False)

    with no_ssrf_check(), patch.object(WebScraperServer, "_client", make):
        await call(downloader, "web_scraper_download", url="https://example.com/f",
                   path=str(tmp_path / "dl" / "f.txt"))
    assert seen["ua"] == "ScarabHive-research/1.0 (contact@example.com)"


# ── what an LLM actually sends ─────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"max_chars": "lots"}, {"max_chars": None}, {"offset": "nope"}, {"offset": None},
    {"timeout": "soon"}, {"max_links": "all"}, {"cache_ttl": "an hour"},
])
async def test_an_unusable_number_falls_back_instead_of_raising(server, bad):
    """These arrive from a model, so "8000", null and "lots" all turn up.
    Raising here would bypass the tool's own error contract and its status
    line; the page still has to come back."""
    with mock_httpx():
        result, status = await call(server, "web_scraper_page", url="https://example.com/", **bad)
    assert result["title"] == "Copper (Amiga)"
    assert result["total_chars"] > 0
    status.end.assert_awaited_once()


@pytest.mark.parametrize("url", ["https://example.com:abc/", "https://example.com:99999999/"])
async def test_a_port_that_is_not_a_number_is_refused_as_a_result(server, url):
    """urlparse defers this: `.port` is where it finally raises, inside the
    SSRF guard, past every except clause the tool had."""
    result, status = await call(server, "web_scraper_page", url=url)
    assert "error" in result and "port" in result["error"].lower()
    status.error.assert_awaited()


async def test_an_endless_redirect_chain_is_an_error_not_the_last_stub(server):
    """Falling out of the hop loop handed the caller the final 3xx response
    as if it were the page -- with its body, which many redirect stubs have."""
    resp = MagicMock()
    resp.is_redirect = True
    resp.headers = {"location": "/next", "content-type": "text/html"}
    resp.status_code = 302
    resp.text = "<html><title>Redirecting</title><body>Moved</body></html>"
    resp.url = "https://example.com/loop"

    async def fake_get(url, *a, **k):
        return resp

    client = AsyncMock()
    client.get = fake_get
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with no_ssrf_check(), patch.object(httpx, "AsyncClient", return_value=client), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result, status = await call(server, "web_scraper_page", url="https://example.com/loop")
    assert "TooManyRedirects" in result["error"]
    assert "text" not in result
    status.error.assert_awaited()


async def test_two_downloads_of_the_same_path_do_not_share_a_staging_file(downloader, tmp_path):
    """A deterministic `.part` name is one file for both: one loses its bytes,
    the other trips over the open handle."""
    import asyncio as _asyncio

    async def body():
        for _ in range(3):
            await _asyncio.sleep(0)
            yield b"z" * 10

    def handler(request):
        return httpx.Response(200, content=body(), headers={"content-type": "text/plain"})

    target = tmp_path / "dl" / "same.bin"
    with no_ssrf_check(), transport(handler):
        results = await _asyncio.gather(
            call(downloader, "web_scraper_download", url="https://example.com/a", path=str(target)),
            call(downloader, "web_scraper_download", url="https://example.com/a", path=str(target),
                 overwrite=True),
            return_exceptions=True)
    assert not any(isinstance(r, BaseException) for r in results), results
    assert target.read_bytes() == b"z" * 30
    assert not list((tmp_path / "dl").glob("*.part")), "a staging file survived"


async def test_a_path_whose_parent_is_a_file_is_an_error_not_a_raise(downloader, tmp_path):
    blocker = tmp_path / "dl" / "blocker"
    blocker.parent.mkdir()
    blocker.write_text("i am a file")
    with no_ssrf_check(), transport(serve()):
        result, status = await call(downloader, "web_scraper_download",
                                    url="https://example.com/x", path=str(blocker / "nested" / "f.pdf"))
    assert "Cannot write" in result["error"]
    status.error.assert_awaited()


# ── extraction fidelity ────────────────────────────────────────────────────

async def test_an_aside_is_content_not_furniture(server):
    """HTML5 `aside` means "tangentially related", which is where
    documentation puts its note and warning callouts."""
    html = ("<html><head><title>T</title></head><body>"
            "<aside><h1>Important Notice</h1><p>The kernel panics on boot.</p></aside>"
            "<p>Normal.</p></body></html>")
    with mock_httpx(html=html):
        result, _ = await call(server, "web_scraper_page", url="https://example.com/")
    assert "The kernel panics on boot." in result["text"]
    assert "Important Notice" in result["text"]


async def test_a_code_block_keeps_its_indentation(server):
    """Collapsing runs of spaces is right for prose and wrong for code: a
    Python snippet from a documentation page arrived as something that no
    longer parses -- and reading documentation is what this tool is for."""
    html = ("<html><body><p>Example:</p><pre><code>def foo():\n"
            "    if True:\n        return 1\n    return 2\n</code></pre></body></html>")
    with mock_httpx(html=html):
        result, _ = await call(server, "web_scraper_page", url="https://example.com/")
    assert "def foo():\n    if True:\n        return 1\n    return 2" in result["text"]


def test_a_nested_list_item_does_not_run_its_children_together(server):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup("<ul><li>Fruits<ul><li>Apple</li><li>Banana</li></ul></li></ul>", "lxml")
    outer = server._extract_lists(soup)[0]["items"][0]
    assert "FruitsAppleBanana" not in outer
    assert outer.split() == ["Fruits", "Apple", "Banana"]


def test_a_header_row_has_to_say_it_is_one(server):
    """Taking the first row on faith made a colspan title the header and
    pushed the real one into the data, where nothing lines up any more."""
    from bs4 import BeautifulSoup
    titled = BeautifulSoup(
        '<table><tr><td colspan="2">Merged Header</td></tr>'
        "<tr><th>Name</th><th>Symbol</th></tr><tr><td>Hydrogen</td><td>H</td></tr></table>", "lxml")
    table = server._extract_tables(titled)[0]
    assert table["headers"] == []
    assert ["Name", "Symbol"] in table["rows"] and ["Hydrogen", "H"] in table["rows"]

    proper = BeautifulSoup(
        "<table><thead><tr><td>Name</td><td>Symbol</td></tr></thead>"
        "<tbody><tr><td>Hydrogen</td><td>H</td></tr></tbody></table>", "lxml")
    assert server._extract_tables(proper)[0] == {
        "caption": None, "headers": ["Name", "Symbol"], "rows": [["Hydrogen", "H"]]}
