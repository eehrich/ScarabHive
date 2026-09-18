"""Fetch web pages for an agent: readable text, links, or the raw file.

Two tools. ``page`` turns a URL into text the model can read, or into its
links. ``download`` saves whatever the URL serves into a sandboxed directory,
for the PDFs, images and archives the text path cannot represent.

Every URL -- the one asked for and every redirect hop -- passes the SSRF
guard first. The model controls the URL, so without that check a
prompt-injected page could make this server read cloud metadata or an
internal admin port and hand the body back.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import socket
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, TYPE_CHECKING

import httpx
from bs4 import BeautifulSoup

from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.plugins.cache import PluginCache
from agent_system.utils.path_sandbox import PathSandbox, PathSandboxDenied

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

# Hostnames of cloud metadata services, blocked by name on top of the IP-range
# check: they resolve to link-local 169.254.169.254, which is non-global
# anyway, but the explicit block survives odd resolver behaviour.
_BLOCKED_METADATA_HOSTS = {"metadata.google.internal", "metadata.goog", "metadata"}

MAX_HOPS = 10
RETRY_STATUSES = (429, 502, 503, 504)

# What ``page`` will read as text. Everything else is a file: download it.
_TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml")

# Desktop browsers only. A mobile user agent gets a mobile page, and the
# cache key does not know which one it stored.
_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
)
# No Accept-Encoding here: httpx advertises exactly the encodings it can
# decode. Forcing "br" on a host without brotli made every brotli response
# undecodable.
_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# A challenge page is recognised by its TITLE or by a marker of a known bot
# wall -- never by words in the body. The previous version matched "please
# wait", "forbidden", "captcha" or "cloudflare" anywhere in the HTML, so a
# page loading one script from cdnjs.cloudflare.com was reported blocked.
_CHALLENGE_TITLES = ("just a moment", "attention required", "access denied",
                     "are you a robot", "verify you are human", "one more step")
_CHALLENGE_MARKERS = ("cf-chl", "_cf_chl_opt", "challenge-platform", "captcha-delivery.com", "px-captcha")

# Removed before text extraction: code, styling and page furniture. NOT
# `aside`: in HTML5 that means "tangentially related", which is where
# documentation puts its note and warning callouts -- dropping it loses
# content silently, and the reply still reads as a complete page.
_NOISE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "nav", "footer")
# Stands in for a code block while the text is cleaned; the block comes back
# verbatim afterwards. No page produces this by itself.
_PRE_TOKEN = "␟"
# Get a line break after these, so a paragraph is a paragraph and not one
# word per inline tag.
_BLOCK_TAGS = ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "blockquote", "pre", "dd", "dt", "td", "th")
# Control characters (tab, LF, CR excepted), zero-width and bidi marks.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F\u200B-\u200D\uFEFF\u202A-\u202E]")


class WebScraperSSRFError(Exception):
    """The URL targets a blocked host: internal, metadata, or not http(s)."""


class _DownloadTooLarge(Exception):
    pass


class _TooManyRedirects(Exception):
    """The chain never arrived anywhere."""


def _number(params: dict[str, Any], key: str, default: float) -> float:
    """A numeric argument, however the model spelled it.

    The values reach this plugin from an LLM, so "8000", null and "lots" all
    turn up. Raising here would bypass the tool's own error contract and the
    status line with it, so an unusable value falls back to the default.
    """
    value = params.get(key, default)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.info("web_scraper: ignoring unusable %s=%r, using %s", key, value, default)
        return default


class WebScraperServer(SchemaBasedToolServer):
    """``page`` and ``download``, with per-session cookie jars and a cache."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        super().__init__(name, system_config, server_config)
        self.ssl_verify = getattr(system_config, "ssl_verify", True)
        self._proxies: list[str] = list(getattr(server_config, "proxies", None) or [])

        # Cookie jars keyed by (session, domain), so one user's login cookies
        # never ride along on another user's request to the same site.
        # Bounded FIFO, since the session dimension multiplies the jars.
        self._sessions: dict[tuple[str, str], httpx.Cookies] = {}
        self._max_cookie_jars = 2000

        self.cache = PluginCache(plugin_name="web_scraper",
                                 default_ttl=getattr(server_config, "cache_ttl", 1800))
        self.cache_enabled = getattr(server_config, "cache_enabled", True)

        # One fixed identity instead of the rotating browser pool. Some sites
        # require a descriptive agent with a contact address and refuse every
        # browser string: Wikimedia answers a Chrome user agent with 403 and
        # its own bot-policy text, and a descriptive one with normal traffic.
        # Measured 05.09.2026. Configuring it is the operator's call -- the
        # contact address has to be real.
        self.user_agent: str | None = getattr(server_config, "user_agent", None) or None

        # Downloads exist only where a directory was granted (fail closed):
        # without one the tool is not even rendered into the schema.
        directories = list(getattr(server_config, "allowed_directories", None) or [])
        self.download_sandbox = PathSandbox.from_config(directories, base=Path.cwd()) if directories else None
        self.max_download_mb = float(getattr(server_config, "max_download_mb", 100))

    def get_template_vars(self) -> dict[str, Any]:
        vars = super().get_template_vars()
        vars["download_enabled"] = self.download_sandbox is not None
        return vars

    def _user_agent(self) -> str:
        """The configured identity, or one from the browser pool."""
        return self.user_agent or random.choice(_USER_AGENTS)

    # ── SSRF guard ───────────────────────────────────────────────────────

    async def _assert_url_safe(self, url: str) -> None:
        """Raise WebScraperSSRFError unless ``url`` is http(s) to a public host.

        Resolves the hostname and rejects every address that is not globally
        routable: loopback, RFC1918, link-local (the 169.254.169.254 metadata
        IP included), reserved, multicast. Called for the initial URL AND for
        every redirect hop -- a public host can 302 to an internal target.

        The resolved IP is not pinned for the connection, so a DNS-rebinding
        attacker racing the resolver is out of scope; that needs a custom
        transport.
        """
        parsed = urllib.parse.urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise WebScraperSSRFError(f"Blocked URL scheme {scheme or '(none)'!r} (only http/https allowed)")
        host = parsed.hostname
        if not host:
            raise WebScraperSSRFError("URL has no host")
        if host.lower() in _BLOCKED_METADATA_HOSTS:
            raise WebScraperSSRFError(f"Blocked cloud-metadata host: {host}")

        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError as e:
            # urlparse defers this: `.port` is where "https://host:abc/" and
            # an out-of-range number finally raise.
            raise WebScraperSSRFError(f"Unusable port in URL: {e}")
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise WebScraperSSRFError(f"Cannot resolve host {host!r}: {e}")
        for info in infos:
            addr = info[4][0].split("%", 1)[0]  # drop an IPv6 scope id
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                raise WebScraperSSRFError(f"Unparseable address {addr!r} for host {host!r}")
            if not ip.is_global:
                raise WebScraperSSRFError(f"Blocked non-public address {ip} for host {host!r}")

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _client(self, url: str, user_agent: str, timeout: float,
                session_id: str | None = None, request_proxy: str | None = None) -> httpx.AsyncClient:
        """A client with this (session, domain)'s cookie jar and the proxy for
        the domain. Redirects are followed by hand so each hop is checked."""
        domain = urllib.parse.urlparse(url).netloc
        key = (session_id or "_shared", domain)
        jar = self._sessions.get(key)
        if jar is None:
            while len(self._sessions) >= self._max_cookie_jars:
                self._sessions.pop(next(iter(self._sessions)))
            jar = self._sessions[key] = httpx.Cookies()
        proxy = request_proxy
        if not proxy and self._proxies:
            proxy = self._proxies[hash(domain) % len(self._proxies)]
        kwargs: dict[str, Any] = {
            "follow_redirects": False,
            "verify": self.ssl_verify,
            "headers": {**_BROWSER_HEADERS, "User-Agent": user_agent},
            "cookies": jar,
            "timeout": timeout,
        }
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    async def _fetch_html_once(self, target_url: str, user_agent: str, timeout: float,
                               session_id: str | None = None,
                               request_proxy: str | None = None) -> tuple[str, int, str, str]:
        """One GET, redirects followed by hand.

        Returns (text, status_code, final_url, content_type); ``text`` is
        empty when the body is not a text type. Raises WebScraperSSRFError
        and httpx.HTTPError -- the caller decides about retries.
        """
        async with self._client(target_url, user_agent, timeout, session_id, request_proxy) as client:
            url = target_url
            for hop in range(MAX_HOPS + 1):
                await self._assert_url_safe(url)
                resp = await client.get(url)
                if resp.is_redirect and resp.headers.get("location"):
                    if hop == MAX_HOPS:
                        # Falling through here would hand the caller the last
                        # 3xx stub as if it were the page.
                        raise _TooManyRedirects(f"more than {MAX_HOPS} redirects from {target_url}")
                    url = urllib.parse.urljoin(url, resp.headers["location"])
                    continue
                break
        content_type = (resp.headers.get("content-type") or "").lower()
        # A missing content-type is treated as text: small servers omit it.
        is_text = not content_type or content_type.startswith(_TEXT_TYPES)
        return (resp.text or "") if is_text else "", resp.status_code, str(resp.url), content_type

    async def _fetch_with_retry(self, target_url: str, user_agent: str, timeout: float,
                                max_retries: int = 3, params: dict | None = None) -> tuple[str, int, str, str]:
        """Retry transport failures and 429/502/503/504 with backoff."""
        session_id = params.get("_session_id") if params else None
        for attempt in range(max_retries + 1):
            token = params.get("_cancellation_token") if params else None
            if token and token.is_cancelled:
                raise RuntimeError(f"Web scraper fetch cancelled for {target_url}")
            try:
                result = await self._fetch_html_once(target_url, user_agent, timeout, session_id=session_id)
            except WebScraperSSRFError:
                raise
            except httpx.HTTPError as e:
                if attempt == max_retries:
                    raise
                reason = type(e).__name__
            else:
                if result[1] not in RETRY_STATUSES or attempt == max_retries:
                    return result
                reason = f"HTTP {result[1]}"
            delay = 2 ** attempt + random.uniform(0, 1)
            logger.info("web_scraper: %s for %s, retry %d in %.1fs", reason, target_url[:80], attempt + 1, delay)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    # ── judging and reading a page ───────────────────────────────────────

    @staticmethod
    def _is_blocked_response(html: str, status_code: int, final_url: str) -> tuple[bool, str]:
        """A bot wall, by status code or by the page's own title/markers."""
        if status_code == 429:
            return True, "blocked: rate limited (429)"
        if status_code == 403:
            return True, "blocked: access forbidden (403)"
        if status_code == 503:
            return True, "blocked: service unavailable (503)"
        if not html:
            return False, ""
        lower = html.lower()
        m = re.search(r"<title[^>]*>(.*?)</title>", lower, re.S)
        title = " ".join(m.group(1).split()) if m else ""
        if any(t in title for t in _CHALLENGE_TITLES):
            return True, f"blocked: challenge page ({title[:40]})"
        if any(marker in lower for marker in _CHALLENGE_MARKERS):
            return True, "blocked: captcha challenge"
        return False, ""

    @staticmethod
    def _clean_text(text: str) -> str:
        """Drop control and zero-width characters, collapse whitespace per
        line, drop empty lines. Every script stays -- a Japanese page is a
        page."""
        text = _CONTROL_CHARS.sub("", text or "")
        lines = (" ".join(line.split()) for line in text.splitlines())
        return "\n".join(line for line in lines if line)

    @classmethod
    def _extract(cls, html: str, final_url: str) -> dict[str, Any]:
        """Everything ``page`` can answer about this HTML, in one parse."""
        soup = BeautifulSoup(html, "lxml")
        title = soup.title.get_text(strip=True) if soup.title else None
        links = []
        for a in soup.find_all("a", href=True):
            rel = a.get("rel") or []
            rel = rel.split() if isinstance(rel, str) else list(rel)
            links.append({"href": a["href"], "abs_url": urllib.parse.urljoin(final_url, a["href"]),
                          "text": a.get_text(strip=True) or None, "rel": [r.lower() for r in rel]})
        tables = cls._extract_tables(soup)
        lists = cls._extract_lists(soup)

        # Code blocks leave the pipeline before the whitespace pass and come
        # back verbatim afterwards. Collapsing runs of spaces per line is
        # right for prose and wrong for code: it strips the indentation, and
        # a Python snippet from a documentation page arrives as something
        # that no longer parses.
        blocks: list[str] = []
        for pre in soup.find_all("pre"):
            pre.replace_with(f"{_PRE_TOKEN}{len(blocks)}{_PRE_TOKEN}")
            blocks.append(pre.get_text())

        for tag in soup(_NOISE_TAGS):
            tag.decompose()
        for tag in soup.find_all(_BLOCK_TAGS):
            tag.append("\n")
        text = cls._clean_text(soup.get_text(" "))
        for i, block in enumerate(blocks):
            text = text.replace(f"{_PRE_TOKEN}{i}{_PRE_TOKEN}", "\n" + block.strip("\n") + "\n")
        return {"title": title, "text": text, "links": links, "tables": tables, "lists": lists}

    @staticmethod
    def _extract_tables(soup) -> list[dict[str, Any]]:
        tables = []
        for table in soup.find_all("table"):
            trs = table.find_all("tr")
            rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in trs]
            keep = [(tr, r) for tr, r in zip(trs, rows) if any(r)]
            if not keep:
                continue
            # A header is one that says so: a row inside <thead>, or a first
            # row built from <th>. Taking the first row on faith turned a
            # colspan title into the header and pushed the real one into the
            # data, where nothing lines up with it any more.
            first_tr, first_row = keep[0]
            is_header = first_tr.find_parent("thead") is not None or bool(first_tr.find("th"))
            caption = table.find("caption")
            tables.append({"caption": caption.get_text(" ", strip=True) if caption else None,
                           "headers": first_row if is_header else [],
                           "rows": [r for _, r in keep[1:]] if is_header else [r for _, r in keep]})
        return tables

    @staticmethod
    def _extract_lists(soup) -> list[dict[str, Any]]:
        lists = []
        for elem in soup.find_all(["ul", "ol", "dl"]):
            # A separator, because an item that contains a nested list would
            # otherwise arrive as one run-on word: "FruitsAppleBanana".
            if elem.name == "dl":
                items = [{"term": dt.get_text(" ", strip=True),
                          "definition": (dd.get_text(" ", strip=True) if (dd := dt.find_next_sibling("dd")) else "")}
                         for dt in elem.find_all("dt")]
            else:
                items = [li.get_text(" ", strip=True) for li in elem.find_all("li", recursive=False)]
            if items:
                lists.append({"type": elem.name, "items": items})
        return lists

    @staticmethod
    def _create_cache_key(url: str, session_id: str | None = None) -> str:
        """Per session: the cache holds the fetched body, and a page fetched
        with one user's cookies must not be served to another. Query
        parameters are sorted, the fragment dropped."""
        p = urllib.parse.urlparse(url)
        query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(p.query, keep_blank_values=True)))
        normalized = urllib.parse.urlunparse((p.scheme, p.netloc, p.path, p.params, query, ""))
        return json.dumps({"session": session_id or "_shared", "url": normalized},
                          sort_keys=True, separators=(",", ":"))

    # ── tools ────────────────────────────────────────────────────────────

    async def page(self, params: dict[str, Any]) -> dict[str, Any]:
        operation = params.get("operation", "content")
        if operation not in ("content", "links"):
            return {"error": f"Unknown operation: {operation}. Supported: 'content', 'links'"}
        url = params.get("url")
        if not url or not isinstance(url, str):
            return {"error": "Missing required parameter 'url' (string)"}
        status = params["_status"]

        try:
            await self._assert_url_safe(url)
        except WebScraperSSRFError as e:
            logger.warning("web_scraper blocked SSRF target %r: %s", url, e)
            await status.error(f"Blocked URL: {e}")
            return {"url": url, "error": f"Blocked URL (SSRF protection): {e}"}

        key = self._create_cache_key(url, params.get("_session_id"))
        page = None
        if self.cache_enabled and not params.get("ignore_cache", False):
            page = await self.cache.get(key)
        cached = page is not None

        if page is None:
            await status.progress(f"Fetching {url[:80]}")
            try:
                html, code, final_url, content_type = await self._fetch_with_retry(
                    url, self._user_agent(), _number(params, "timeout", 20), params=params)
            except WebScraperSSRFError as e:
                logger.warning("web_scraper blocked SSRF redirect for %r: %s", url, e)
                await status.error(f"Blocked redirect: {e}")
                return {"url": url, "error": f"Blocked URL (SSRF protection): {e}"}
            except (httpx.HTTPError, httpx.InvalidURL, _TooManyRedirects) as e:
                await status.error(f"Fetch failed: {type(e).__name__} -- {url[:60]}")
                return {"url": url, "error": f"Fetch failed: {type(e).__name__}: {e}"}

            blocked, why = self._is_blocked_response(html, code, final_url)
            if blocked:
                await status.error(f"{why} -- {url[:60]}")
                return {"url": url, "final_url": final_url, "status_code": code, "error": why}
            if not html:
                if content_type and not content_type.startswith(_TEXT_TYPES):
                    why = f"Not a text page ({content_type}); use {self.name}_download to save the file"
                else:
                    why = f"Empty response (HTTP {code})"
                await status.error(f"{why[:50]} -- {url[:60]}")
                return {"url": url, "final_url": final_url, "status_code": code,
                        "content_type": content_type, "error": why}

            page = {"url": url, "final_url": final_url, "status_code": code,
                    "content_type": content_type, **self._extract(html, final_url)}
            if self.cache_enabled:
                # 0 (absent, or a value the model made up) means: the
                # instance's configured lifetime.
                await self.cache.set(key, page, ttl=int(_number(params, "cache_ttl", 0)) or None)

        code = page.get("status_code")
        where = f"{'(cached) ' if cached else ''}HTTP {code} -- {url[:60]}"
        if operation == "links":
            links = page["links"]
            if not params.get("include_nofollow", False):
                links = [link for link in links if "nofollow" not in link["rel"]]
            if params.get("only_same_domain", False):
                domain = urllib.parse.urlparse(page["final_url"]).netloc
                links = [link for link in links if urllib.parse.urlparse(link["abs_url"]).netloc == domain]
            max_links = int(_number(params, "max_links", 0))
            if max_links > 0:
                links = links[:max_links]
            await status.end(f"{len(links)} link(s) {where}", meta={"link_count": len(links)})
            return {"url": url, "final_url": page["final_url"], "status_code": code, "links": links}

        text = page["text"]
        offset = max(0, int(_number(params, "offset", 0)))
        max_chars = int(_number(params, "max_chars", 8000))
        piece = text[offset:offset + max_chars] if max_chars > 0 else text[offset:]
        truncated = offset + len(piece) < len(text)
        result = {"url": url, "final_url": page["final_url"], "status_code": code,
                  "content_type": page.get("content_type"), "title": page.get("title"),
                  "text": piece, "total_chars": len(text), "truncated": truncated}
        if params.get("extract_tables") and page.get("tables"):
            result["tables"] = page["tables"]
        if params.get("extract_lists") and page.get("lists"):
            result["lists"] = page["lists"]
        got = f"{len(piece)} of {len(text)} chars" if truncated or offset else f"{len(text)} chars"
        await status.end(f"{got} {where}", meta={"total_chars": len(text), "truncated": truncated})
        return result

    async def download(self, params: dict[str, Any]) -> dict[str, Any]:
        """Save the URL's body to a file inside the download sandbox."""
        url, path = params.get("url"), params.get("path")
        if not url or not isinstance(url, str):
            return {"error": "Missing required parameter 'url' (string)"}
        if not path or not isinstance(path, str):
            return {"error": "Missing required parameter 'path' (string)"}
        if self.download_sandbox is None:
            return {"error": f"{self.name} has no allowed_directories configured, downloads are off"}
        status = params["_status"]

        try:
            target = self.download_sandbox.resolve(path, write=True)
        except PathSandboxDenied as e:
            return {"error": str(e)}
        if target.is_dir():
            return {"error": f"path names a directory: {path}. Give the file name to write."}
        if target.exists() and not params.get("overwrite", False):
            return {"error": f"File exists: {path}. Pass overwrite=true to replace it."}
        try:
            await self._assert_url_safe(url)
        except WebScraperSSRFError as e:
            await status.error(f"Blocked URL: {e}")
            return {"url": url, "error": f"Blocked URL (SSRF protection): {e}"}

        limit = int(self.max_download_mb * 1024 * 1024)
        part = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.part")
        digest = hashlib.sha256()
        written = 0
        await status.progress(f"Downloading {url[:80]}")
        try:
            async with self._client(url, self._user_agent(), _number(params, "timeout", 60),
                                    params.get("_session_id")) as client:
                current = url
                for hop in range(MAX_HOPS + 1):
                    await self._assert_url_safe(current)
                    async with client.stream("GET", current) as resp:
                        if resp.is_redirect and resp.headers.get("location"):
                            if hop == MAX_HOPS:
                                raise _TooManyRedirects(
                                    f"more than {MAX_HOPS} redirects from {url}")
                            current = urllib.parse.urljoin(current, resp.headers["location"])
                            continue
                        if resp.status_code >= 400:
                            await status.error(f"HTTP {resp.status_code} -- {url[:60]}")
                            return {"url": url, "final_url": str(resp.url), "status_code": resp.status_code,
                                    "error": f"HTTP {resp.status_code}"}
                        # The cap is enforced on the bytes as they arrive, never
                        # on Content-Length: the header can lie or be absent.
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with part.open("wb") as fh:
                            async for chunk in resp.aiter_bytes():
                                written += len(chunk)
                                if written > limit:
                                    raise _DownloadTooLarge()
                                fh.write(chunk)
                                digest.update(chunk)
                        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                        final_url, code = str(resp.url), resp.status_code
                        break
            os.replace(part, target)
        except WebScraperSSRFError as e:
            await status.error(f"Blocked redirect: {e}")
            return {"url": url, "error": f"Blocked URL (SSRF protection): {e}"}
        except (httpx.HTTPError, _TooManyRedirects) as e:
            await status.error(f"Download failed: {type(e).__name__} -- {url[:60]}")
            return {"url": url, "error": f"Download failed: {type(e).__name__}: {e}"}
        except _DownloadTooLarge:
            await status.error(f"larger than {self.max_download_mb:g} MB -- {url[:60]}")
            return {"url": url, "error": f"Download exceeds max_download_mb ({self.max_download_mb:g} MB)"}
        except httpx.InvalidURL as e:
            await status.error(f"Unusable URL -- {url[:60]}")
            return {"url": url, "error": f"Unusable URL: {e}"}
        except OSError as e:
            # mkdir onto an existing file, a name the filesystem refuses, a
            # full disk. The bytes are gone either way; say so as a result.
            await status.error(f"Cannot write {target.name}: {type(e).__name__}")
            return {"url": url, "error": f"Cannot write {path}: {type(e).__name__}: {e}"}
        finally:
            if part.exists():
                part.unlink()

        await status.end(f"{written} bytes ({content_type or 'unknown type'}) -- {target.name}",
                         meta={"bytes": written, "content_type": content_type})
        return {"url": url, "final_url": final_url, "status_code": code, "path": str(target),
                "bytes": written, "content_type": content_type, "sha256": digest.hexdigest()}
