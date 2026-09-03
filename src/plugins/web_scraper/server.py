from __future__ import annotations

from typing import Any, TYPE_CHECKING
import re
import ipaddress
import socket
import urllib.parse
import asyncio
import random
import logging

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.plugins.cache import PluginCache

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)

# Hostnames that resolve to cloud metadata services. Blocked by name as a
# belt-and-suspenders measure on top of the IP-range check (they resolve to
# link-local 169.254.169.254, which is already non-global, but an explicit
# name block is clearer and survives odd resolver behavior).
_BLOCKED_METADATA_HOSTS = {
    "metadata.google.internal", "metadata.goog", "metadata",
}


class WebScraperSSRFError(Exception):
    """Raised when a URL targets a blocked host (internal / metadata / non-http).

    SSRF guard: the scraper fetches LLM-controlled URLs and returns the body to
    the model, so without this an LLM (e.g. via prompt-injection in a scraped
    page) could read cloud metadata (169.254.169.254 -> IAM credentials),
    localhost admin ports, or internal RFC1918 services and exfiltrate them.
    """


class WebScraperServer(SchemaBasedMCPServer):
    """Web scraper with caching, retry logic, and anti-bot evasion."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # SSL verification from system config
        self.ssl_verify = getattr(system_config, 'ssl_verify', True)
        
        # Initialize User-Agent pool for anti-bot evasion
        self._user_agents = [
            # Chrome (most common)
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            
            # Firefox
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:132.0) Gecko/20100101 Firefox/132.0",
            "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
            
            # Safari
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1.1 Safari/605.1.15",
            
            # Edge
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
            
            # Mobile browsers
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
        ]
        
        # Browser-like headers that real browsers send
        self._browser_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        
        # Session management for cookie persistence, keyed by (session_id,
        # domain) for per-user isolation. Bounded by FIFO eviction since the
        # session dimension multiplies the number of jars.
        self._sessions = {}  # (session_id, domain) -> httpx.Cookies
        self._max_cookie_jars = 2000

        # Proxy configuration from mcp_config
        self._proxies = getattr(mcp_config, 'proxies', [])
        
        # Initialize cache system
        cache_ttl = getattr(mcp_config, 'cache_ttl', 1800)  # 30 minutes default
        self.cache = PluginCache(plugin_name="web_scraper", default_ttl=cache_ttl)
        self.cache_enabled = getattr(mcp_config, 'cache_enabled', True)

    def _get_random_user_agent(self) -> str:
        """Get a random User-Agent from the pool"""
        return random.choice(self._user_agents)

    def _get_browser_headers(self, user_agent: str) -> dict[str, str]:
        """Get browser-like headers with the specified User-Agent"""
        headers = self._browser_headers.copy()
        headers["User-Agent"] = user_agent
        return headers

    def set_proxies(self, proxies: list[str]):
        """Set proxy URLs for requests. Format: ['http://proxy1:port', 'https://proxy2:port']"""
        self._proxies = proxies

    def add_proxy(self, proxy_url: str):
        """Add a single proxy URL"""
        if proxy_url not in self._proxies:
            self._proxies.append(proxy_url)
    
    def _create_cache_key(self, url: str, operation: str, max_chars: int,
                         extract_tables: bool, extract_forms: bool,
                         extract_lists: bool, include_html: bool,
                         session_id: str | None = None) -> str:
        """Create a cache key from request parameters.

        Scoped by session_id: the cache stores the FETCHED page body, so a
        shared (session-less) key would serve one user's authenticated/
        personalized content to another from cache - the same cross-user leak
        the per-session cookie jars close on the fetch path. Trade-off: public
        pages are no longer shared across sessions (re-fetched per session),
        which is acceptable for a scraper where correctness beats cache reuse.
        """
        # Normalize URL (remove fragment, sort query params)
        try:
            from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
            parsed = urlparse(url)
            # Sort query parameters for consistent caching
            if parsed.query:
                query_params = parse_qs(parsed.query, keep_blank_values=True)
                sorted_query = urlencode(sorted(query_params.items()), doseq=True)
                normalized_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, 
                                          parsed.params, sorted_query, ""))  # Remove fragment
            else:
                normalized_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, 
                                          parsed.params, "", ""))  # Remove fragment and query
        except Exception:
            normalized_url = url
        
        # Create cache key from normalized URL and options
        cache_data = {
            "session": session_id or "_shared",
            "url": normalized_url,
            "operation": operation,
            "max_chars": max_chars,
            "extract_tables": extract_tables,
            "extract_forms": extract_forms,
            "extract_lists": extract_lists,
            "include_html": include_html
        }
        
        import json
        return json.dumps(cache_data, sort_keys=True, separators=(',', ':'))
    def _clean_text(self, text: str) -> str:
        """Clean up extracted text by removing excessive whitespace, normalizing newlines, and filtering invalid Unicode."""
        if not text:
            return ""

        # First, ensure we have valid UTF-8 by re-encoding with error handling
        try:
            # Handle potential encoding issues by cleaning bytes first
            text_bytes = text.encode('utf-8', errors='ignore')
            text = text_bytes.decode('utf-8', errors='ignore')
        except Exception:
            # Fallback: just remove non-printable characters
            text = ''.join(char for char in text if char.isprintable() or char.isspace())

        # Remove control characters except for common whitespace
        import re
        # Keep only printable ASCII, basic Latin, and common Unicode ranges
        # Remove control chars (0x00-0x1F) except tab(0x09), LF(0x0A), CR(0x0D)
        text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', text)
        
        # Remove zero-width characters and other problematic Unicode
        text = re.sub(r'[\u200B-\u200D\uFEFF]', '', text)  # zero-width spaces
        text = re.sub(r'[\u202A-\u202E]', '', text)        # directional formatting
        
        # Remove any remaining non-printable characters outside basic ranges
        # Keep: Basic Latin (0000-007F), Latin-1 Supplement (0080-00FF), 
        #       Latin Extended-A (0100-017F), Latin Extended-B (0180-024F),
        #       Common punctuation and symbols
        text = re.sub(r'[^\u0020-\u007E\u00A0-\u024F\u2000-\u206F\u20A0-\u20CF\u2100-\u214F\s]', '', text)

        # Replace multiple consecutive newlines with maximum 2 newlines
        text = re.sub(r'\n{3,}', '\n\n', text)

        # Replace multiple consecutive spaces/tabs with single space (but preserve newlines)
        lines = text.split('\n')
        cleaned_lines = []
        for line in lines:
            # Clean each line individually - remove excessive spaces/tabs
            cleaned_line = re.sub(r'[ \t]+', ' ', line.strip())
            cleaned_lines.append(cleaned_line)

        # Join lines back and remove empty lines between content
        text = '\n'.join(cleaned_lines)

        # Remove lines that are just whitespace
        lines = [line for line in text.split('\n') if line.strip()]

        # Join with single newlines and add a final cleanup
        text = '\n'.join(lines)

        # Final cleanup: ensure no more than 2 consecutive newlines
        text = re.sub(r'\n{2,}', '\n\n', text)

        return text.strip()

    def _is_blocked_response(self, html: str, status_code: int, final_url: str) -> tuple[bool, str]:
        """Detect if response is a Cloudflare challenge, 403 block, or other anti-bot response."""
        if not html:
            return False, ""
        
        html_lower = html.lower()
        
        # Common Cloudflare patterns
        cloudflare_patterns = [
            "just a moment",
            "please wait while your request is being verified",
            "cloudflare",
            "checking your browser",
            "enable javascript and cookies",
            "ray id:",
            "cf-ray:",
            "please enable cookies",
            "verify you are human",
            "ddos protection by cloudflare",
            "attention required! | cloudflare"
        ]
        
        # Other anti-bot patterns
        antibot_patterns = [
            "please verify that you are a human",
            "access denied",
            "blocked by security policy",
            "captcha",
            "robot or human",
            "security check",
            "suspicious activity",
            "too many requests",
            "rate limit",
            "forbidden",
            "403 forbidden",
            "access forbidden",
            "bot detected",
            "automated request",
            "request blocked",
            "anti-bot protection",
            "please wait",
            "checking browser",
            "verify human",
            "prove you are human",
            "complete the captcha",
            "security verification",
            "request denied",
            "blocked request"
        ]
        
        all_patterns = cloudflare_patterns + antibot_patterns
        
        # Check for patterns in content
        detected_pattern = None
        for pattern in all_patterns:
            if pattern in html_lower:
                detected_pattern = pattern
                break
        
        # Check status codes that typically indicate blocking
        blocked_status_codes = [403, 429, 503]
        
        if status_code in blocked_status_codes or detected_pattern:
            if status_code == 429:
                return True, "blocked: rate limited (429)"
            elif status_code == 403:
                return True, "blocked: access forbidden (403)"
            elif status_code == 503:
                return True, "blocked: service unavailable (503)"
            elif detected_pattern:
                if any(cf in detected_pattern for cf in ["cloudflare", "just a moment", "ray id"]):
                    return True, "blocked: cloudflare challenge detected"
                elif "captcha" in detected_pattern:
                    return True, "blocked: captcha required"
                elif any(rl in detected_pattern for rl in ["rate limit", "too many requests"]):
                    return True, "blocked: rate limited"
                else:
                    return True, f"blocked: anti-bot protection ({detected_pattern})"
        
        return False, ""

    async def _assert_url_safe(self, url: str) -> None:
        """Raise WebScraperSSRFError if `url` targets a non-public/internal host.

        Validates the scheme (http/https only), then resolves the hostname and
        rejects any address that is not globally routable (loopback, private,
        link-local incl. the 169.254.169.254 metadata IP, reserved, multicast,
        etc.). Must be called for the initial URL AND every redirect hop, since
        a public host can 302 to an internal target.

        Note: this does not pin the resolved IP for the actual connection, so a
        determined DNS-rebinding attacker could still race the resolver. That is
        a far more sophisticated attack than the direct/redirect-to-internal
        vector this closes; IP-pinning would require a custom httpx transport.
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

        port = parsed.port or (443 if scheme == "https" else 80)
        try:
            # Async resolver (does not block the event loop)
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, proto=socket.IPPROTO_TCP
            )
        except socket.gaierror as e:
            raise WebScraperSSRFError(f"Cannot resolve host {host!r}: {e}")

        for info in infos:
            addr = info[4][0]
            # Strip IPv6 scope id (e.g. 'fe80::1%eth0')
            addr = addr.split("%", 1)[0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                raise WebScraperSSRFError(f"Unparseable address {addr!r} for host {host!r}")
            # is_global is True only for publicly routable addresses; everything
            # else (private/loopback/link-local/reserved/multicast/unspecified)
            # is a potential SSRF target and is rejected.
            if not ip.is_global:
                raise WebScraperSSRFError(
                    f"Blocked non-public address {ip} for host {host!r}"
                )

    async def _fetch_with_retry(self, target_url: str, user_agent: str, timeout: float, max_retries: int = 3, params: dict = None) -> tuple[str, int, str, str]:
        """Fetch URL with retry logic for rate limiting and temporary failures."""
        last_exception = None

        # Per-request context (threaded into the fetch instead of mutating
        # shared singleton state): the caller's session_id scopes the cookie
        # jar, and a per-request proxy overrides the configured rotation.
        session_id = params.get("_session_id") if params else None
        request_proxy = params.get("proxy") if params else None

        for attempt in range(max_retries + 1):
            # Check for cancellation before each retry attempt
            cancellation_token = params.get("_cancellation_token") if params else None
            if cancellation_token and cancellation_token.is_cancelled:
                raise RuntimeError(f"Web scraper fetch cancelled for {target_url}")
            # Add random delay before each request (except first) to avoid rate limiting
            if attempt > 0:
                delay = random.uniform(1.0, 3.0)  # 1-3 second random delay
                await asyncio.sleep(delay)

            try:
                html, status_code, final_url, content_type = await self._fetch_html_once(
                    target_url, user_agent, timeout,
                    session_id=session_id, request_proxy=request_proxy,
                )
                
                # If we got a rate limit response, wait and retry
                if status_code == 429 and attempt < max_retries:
                    # Exponential backoff with jitter
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    await asyncio.sleep(delay)
                    continue
                
                # If we got a temporary error, retry
                if status_code in [502, 503, 504] and attempt < max_retries:
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    await asyncio.sleep(delay)
                    continue
                
                return html, status_code, final_url, content_type

            except WebScraperSSRFError:
                # Security rejection - do NOT retry, propagate to the caller.
                raise
            except Exception as e:
                last_exception = e
                if attempt < max_retries:
                    # Exponential backoff with jitter for exceptions too
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    await asyncio.sleep(delay)
                    continue
                
        # All retries failed, return last result or raise last exception
        if last_exception:
            raise last_exception
        return "", 0, target_url, ""

    async def _fetch_html_once(self, target_url: str, user_agent: str, timeout: float,
                               session_id: str | None = None,
                               request_proxy: str | None = None) -> tuple[str, int, str, str]:
        """Fetch HTML once without retry logic."""
        html: str = ""
        status_code: int = 0
        final_url: str = target_url
        content_type: str = ""

        import httpx  # type: ignore
        from httpx import ReadTimeout, RequestError

        # Get browser-like headers
        headers = self._get_browser_headers(user_agent)

        # Extract domain for session management
        from urllib.parse import urlparse
        domain = urlparse(target_url).netloc

        # Cookie jar scoped to (session, domain). Keying by domain ALONE leaked
        # one user's authenticated cookies to every other user scraping the same
        # site. The session_id isolates jars per caller; None ("_shared") keeps
        # the legacy behavior for internal/CLI calls without a session.
        cookie_key = (session_id or "_shared", domain)
        if cookie_key not in self._sessions:
            # FIFO-evict oldest jars when over the cap (dicts keep insertion
            # order). Evicting a jar just drops cached cookies for that
            # (session, domain) - the next request re-establishes them.
            while len(self._sessions) >= self._max_cookie_jars:
                oldest = next(iter(self._sessions))
                self._sessions.pop(oldest, None)
            self._sessions[cookie_key] = httpx.Cookies()
        cookie_jar = self._sessions[cookie_key]

        # Resolve proxy: an explicit per-request proxy wins; otherwise rotate
        # through the configured pool. NEVER mutate self._proxies (that raced
        # across concurrent requests and leaked on early return).
        proxy_url = request_proxy
        if not proxy_url and self._proxies:
            proxy_index = hash(domain) % len(self._proxies)
            proxy_url = self._proxies[proxy_index]

        try:
            # SSRF guard: follow redirects MANUALLY so every hop is validated.
            # follow_redirects=True would let a public host 302 straight to an
            # internal target (e.g. the cloud-metadata IP) without a check.
            client_kwargs = {
                "follow_redirects": False,
                "verify": self.ssl_verify,
                "headers": headers,
                "cookies": cookie_jar,
                "timeout": timeout,
            }
            if proxy_url:
                client_kwargs["proxy"] = proxy_url  # httpx uses 'proxy' (singular)

            max_hops = 10
            current_url = target_url
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = None
                for hop in range(max_hops + 1):
                    # Validate BEFORE each request (initial URL + every redirect)
                    await self._assert_url_safe(current_url)
                    resp = await client.get(current_url)
                    if resp.is_redirect and resp.headers.get("location") and hop < max_hops:
                        # Resolve Location relative to the current URL and loop
                        current_url = urllib.parse.urljoin(current_url, resp.headers["location"])
                        continue
                    break

                status_code = resp.status_code
                final_url = str(resp.url)
                content_type = resp.headers.get("content-type", "").lower()

                # Check if content is actually HTML/text before processing
                if any(ct in content_type for ct in ["text/html", "text/plain", "application/xml", "text/xml"]):
                    html = resp.text or ""
                else:
                    # Non-HTML content detected
                    html = f"[Non-HTML content detected: {content_type}. Content type not supported for text extraction.]"
        except WebScraperSSRFError:
            # Security rejection (initial URL or a redirect hop) - propagate.
            raise
        except ReadTimeout:
            logger.warning(f"ReadTimeout fetching {target_url}")
            return "", 0, target_url, ""
        except RequestError as e:
            logger.warning(f"RequestError fetching {target_url}: {e}")
            return "", 0, target_url, ""
        except Exception as e:
            # Any other error - log and return empty
            logger.error(f"Unexpected error fetching {target_url}: {type(e).__name__}: {e}", exc_info=True)
            return "", 0, target_url, ""

        return html, status_code, final_url, content_type

    def _sanitize_html(self, html: str) -> str:
        """Sanitize HTML content to remove problematic characters before parsing."""
        if not html:
            return ""
        
        try:
            # Ensure proper UTF-8 encoding
            if isinstance(html, bytes):
                html = html.decode('utf-8', errors='ignore')
            
            # Re-encode to clean up any encoding issues
            html_bytes = html.encode('utf-8', errors='ignore')
            html = html_bytes.decode('utf-8', errors='ignore')
            
            # Remove null bytes and other problematic control characters
            import re
            html = re.sub(r'\x00', '', html)  # null bytes
            html = re.sub(r'[\x01-\x08\x0B\x0C\x0E-\x1F]', '', html)  # control chars except \t, \n, \r
            
            return html
        except Exception:
            # Fallback: return empty string if sanitization fails
            return ""

    async def page(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Scrape a web page and extract content.
        
        Tool method - automatically called by generic dispatcher.
        Tool name: {{ name }}_page → Method: page (after stripping {{ name }}_ prefix)
        """
        operation = params.get("operation", "content")
        if operation not in ("content", "links"):
            return {"error": f"Unknown operation: {operation}. Supported: 'content', 'links'"}

        url = params.get("url", "")
        if not url or not isinstance(url, str):
            return {"error": "Missing required parameter 'url' (string)"}

        # Get status object (mandatory from framework)
        status = params["_status"]

        # SSRF guard: reject internal/metadata/non-http targets before any
        # network access (redirect hops are validated inside the fetch).
        try:
            await self._assert_url_safe(url)
        except WebScraperSSRFError as e:
            logger.warning("web_scraper blocked SSRF target %r: %s", url, e)
            try:
                await status.error(f"Blocked URL: {e}")
            except Exception:
                pass
            return {"error": f"Blocked URL (SSRF protection): {e}"}

        timeout = float(params.get("timeout", 20))
        user_agent = params.get("user_agent", self._get_random_user_agent())

        include_html = bool(params.get("include_html", False))
        max_chars = int(params.get("max_chars", 8000))
        extract_tables = bool(params.get("extract_tables", False))
        extract_forms = bool(params.get("extract_forms", False))
        extract_lists = bool(params.get("extract_lists", False))
        
        # Cache control options
        ignore_cache = bool(params.get("ignore_cache", False))
        custom_cache_ttl = params.get("cache_ttl")  # Optional custom TTL
        
        # Create cache key from relevant parameters, scoped by session so one
        # user's cached (possibly authenticated) page body can't be served to
        # another user.
        cache_key = self._create_cache_key(url, operation, max_chars, extract_tables,
                                         extract_forms, extract_lists, include_html,
                                         session_id=params.get("_session_id"))
        
        # Try to get from cache first (unless ignore_cache is True)
        if self.cache_enabled and not ignore_cache:
            cached_result = await self.cache.get(cache_key)
            if cached_result is not None:
                # Discriminate on the OPERATION, not on a key: the content
                # path attaches its extracted links too (":links attach" a few
                # hundred lines down), and caches them under this very key --
                # so keying on "links" reported a link count for a content
                # scrape of any page that has anchors, which is all of them.
                if operation == "links":
                    got = f"{len(cached_result.get('links') or [])} link(s)"
                else:
                    got = f"{len(cached_result.get('text') or '')} chars"
                await status.end(f"{got} (cached) -- {url[:60]}",
                                 meta={"cache_hit": True})
                logger.debug(f"Cache hit for URL: {url[:80]}...")
                return cached_result

        # Links-specific options
        include_nofollow = bool(params.get("include_nofollow", False))
        only_same_domain = bool(params.get("only_same_domain", False))
        max_links = int(params.get("max_links", 0))

        # Per-request proxy (if any) is threaded through params into the fetch;
        # we no longer mutate the shared self._proxies singleton (that raced
        # across concurrent requests and leaked on early return).

        # fetch HTML (async) and parse according to requested action
        # notify start of fetch
        try:
            await status.progress(f"Fetching {url}")
        except Exception:
            # status publishing must not break functionality
            pass

        try:
            html, status_code, final_url, content_type = await self._fetch_with_retry(url, user_agent, timeout, params=params)
        except WebScraperSSRFError as e:
            # A redirect hop pointed at an internal/metadata target.
            logger.warning("web_scraper blocked SSRF redirect for %r: %s", url, e)
            try:
                await status.error(f"Blocked redirect: {e}")
            except Exception:
                pass
            return {"error": f"Blocked URL (SSRF protection): {e}"}

        # Sanitize HTML before processing to remove problematic characters
        html = self._sanitize_html(html)

        # Check for blocked responses (Cloudflare, 403, etc.)
        is_blocked, block_reason = self._is_blocked_response(html, status_code, final_url)
        if is_blocked:
            try:
                await status.error(f"Blocked response from {url}: {block_reason}")
            except Exception:
                pass
            return {
                "url": url,
                "final_url": final_url,
                "status_code": status_code,
                "title": None,
                "text": block_reason,
                "html": html if include_html else None,
                "content_type": content_type,
            }

        # If fetch failed, publish error and return minimal payload
        if not html and status_code == 0:
            try:
                await status.error(f"Failed to fetch {url}")
            except Exception:
                pass
            return {
                "url": url,
                "final_url": final_url,
                "status_code": status_code,
                "title": None,
                "text": "",
                "html": html if include_html else None,
                "content_type": content_type,
            }

        # Check if content is non-HTML and handle appropriately
        if html.startswith("[Non-HTML content detected:"):
            # Return early for non-HTML content with appropriate message
            result = {
                "url": url,
                "final_url": final_url,
                "status_code": status_code,
                "title": None,
                "text": html,  # Contains the descriptive message about non-HTML content
                "content_type": content_type,
            }
            
            if include_html:
                result["html"] = html
                
            try:
                await status.end(f"Completed fetch {url} - non-HTML content detected ({content_type})", meta={"final_url": final_url, "status_code": status_code, "content_type": content_type})
            except Exception:
                pass
            return result

        # Extract readable text and structured data when needed
        title: str | None = None
        text: str = ""
        tables: list[dict[str, Any]] = []
        forms: list[dict[str, Any]] = []
        lists: list[dict[str, Any]] = []

        try:
            from bs4 import BeautifulSoup  # type: ignore
            # Prefer lxml if available, else fallback to html.parser
            try:
                parser = "lxml"
                import lxml  # noqa: F401
            except Exception:
                parser = "html.parser"
            soup = BeautifulSoup(html, parser)

            # Extract structured data before removing scripts/styles
            if extract_tables:
                tables = self._extract_tables(soup)
            if extract_forms:
                forms = self._extract_forms(soup)
            if extract_lists:
                lists = self._extract_lists(soup)

            # Remove scripts/styles for text extraction
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            title = soup.title.string.strip() if soup.title and soup.title.string else None
            text = soup.get_text("\n")
            # Clean up excessive whitespace and newlines
            text = self._clean_text(text)
        except Exception:
            # Naive fallback: strip tags
            no_script = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
            no_style = re.sub(r"<style[\s\S]*?</style>", " ", no_script, flags=re.IGNORECASE)
            m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, flags=re.IGNORECASE)
            title = m.group(1).strip() if m else None
            text = re.sub(r"<[^>]+>", " ", no_style)
            text = self._clean_text(text)

        if max_chars and max_chars > 0:
            text = text[:max_chars]

        # Final text sanitization to ensure OpenAI compatibility
        text = self._clean_text(text)

        # By default include extracted links in the fetch result. This
        # mirrors the 'links' action but is returned automatically so
        # callers get anchor metadata without extra parameters.
        links: list[dict[str, Any]] = []
        try:
            from bs4 import BeautifulSoup  # type: ignore
            soup_links = soup.find_all("a", href=True)
            for a in soup_links:
                href = a.get("href")
                if not href:
                    continue
                abs_url = urllib.parse.urljoin(final_url, href)
                rel = a.get("rel") or []
                if isinstance(rel, str):
                    rel_list = [r.strip().lower() for r in rel.split()]
                else:
                    rel_list = [r.strip().lower() for r in rel]
                # exclude nofollow by default
                if "nofollow" in rel_list:
                    continue
                links.append({
                    "href": href,
                    "abs_url": abs_url,
                    "text": a.get_text(strip=True) or None,
                    "rel": rel_list,
                })
        except Exception:
            hrefs = re.findall(r'href\s*=\s*"([^"]+)"', html, flags=re.IGNORECASE)
            for href in hrefs:
                try:
                    abs_url = urllib.parse.urljoin(final_url, href)
                except Exception:
                    abs_url = href
                links.append({"href": href, "abs_url": abs_url, "text": None, "rel": []})


        # If caller asked for links, extract anchors and return them
        if operation == "links":
            links: list[dict[str, Any]] = []
            try:
                from bs4 import BeautifulSoup  # type: ignore
                soup = BeautifulSoup(html, "html.parser")
                anchors = soup.find_all("a", href=True)
                for a in anchors:
                    href = a.get("href")
                    if not href:
                        continue
                    abs_url = urllib.parse.urljoin(final_url, href)
                    rel = a.get("rel") or []
                    # normalize rel list to strings
                    if isinstance(rel, str):
                        rel_list = [r.strip().lower() for r in rel.split()]
                    else:
                        rel_list = [r.strip().lower() for r in rel]

                    if not include_nofollow and "nofollow" in rel_list:
                        continue

                    if only_same_domain:
                        try:
                            base_net = urllib.parse.urlparse(final_url).netloc
                            link_net = urllib.parse.urlparse(abs_url).netloc
                            if base_net != link_net:
                                continue
                        except Exception:
                            pass

                    link_obj = {
                        "href": href,
                        "abs_url": abs_url,
                        "text": a.get_text(strip=True) or None,
                        "rel": rel_list,
                    }
                    links.append(link_obj)
                    if max_links and len(links) >= max_links:
                        break
            except Exception:
                # fallback regex approach: find href="..."
                hrefs = re.findall(r'href\s*=\s*"([^"]+)"', html, flags=re.IGNORECASE)
                for href in hrefs:
                    try:
                        abs_url = urllib.parse.urljoin(final_url, href)
                    except Exception:
                        abs_url = href
                    links.append({"href": href, "abs_url": abs_url, "text": None, "rel": []})
                    if max_links and len(links) >= max_links:
                        break

            links_result = {"url": url, "final_url": final_url, "status_code": status_code, "links": links}

            # Cache the result
            if self.cache_enabled:
                await self.cache.set(cache_key, links_result, ttl=custom_cache_ttl)
                logger.debug(f"Cached links for URL: {url[:80]}...")

            # The whole links branch never ended -- half the tool surface
            # closed on the scope default "completed".
            if status:
                await status.end(
                    f"Extracted {len(links)} link(s), HTTP {status_code} -- {url[:60]}",
                    meta={"final_url": final_url, "status_code": status_code,
                          "link_count": len(links)})

            return links_result

        # otherwise return full fetch-style result

        result: dict[str, Any] = {
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "title": title,
            "text": text,
            "content_type": content_type,
        }

        # Add structured data if requested
        if extract_tables and tables:
            result["tables"] = tables
        if extract_forms and forms:
            result["forms"] = forms
        if extract_lists and lists:
            result["lists"] = lists

        if include_html:
            result["html"] = html
        # attach default links list
        if links:
            result["links"] = links
        # publish success
        try:
            # Same shape as the cache-hit line above, so the second call for
            # the same page does not read differently from the first. Outcome
            # first: the WebUI cuts on the right, and the url can be long.
            await status.end(
                f"{len(text or '')} chars, HTTP {status_code} -- {url[:60]}",
                meta={"final_url": final_url, "status_code": status_code,
                      "content_type": content_type})
        except Exception:
            pass
        
        # Cache the result before returning
        if self.cache_enabled:
            await self.cache.set(cache_key, result, ttl=custom_cache_ttl)
            logger.debug(f"Cached content for URL: {url[:80]}...")

        return result

    def _extract_tables(self, soup) -> list[dict[str, Any]]:
        """Extract structured table data from HTML."""
        tables: list[dict[str, Any]] = []
        for i, table in enumerate(soup.find_all("table")):
            table_data = {
                "table_id": i,
                "headers": [],
                "rows": [],
                "caption": None,
                "summary": None,
            }

            # Extract caption
            caption = table.find("caption")
            if caption:
                table_data["caption"] = caption.get_text(strip=True)

            # Extract headers
            header_row = table.find("tr")
            if header_row:
                headers = header_row.find_all(["th", "td"])
                table_data["headers"] = [h.get_text(strip=True) for h in headers]

            # Extract data rows
            for row in table.find_all("tr")[1:]:  # Skip header row
                cells = row.find_all(["td", "th"])
                row_data = [cell.get_text(strip=True) for cell in cells]
                if row_data:  # Only add non-empty rows
                    table_data["rows"].append(row_data)

            # Add summary info
            table_data["summary"] = f"Table with {len(table_data['headers'])} columns and {len(table_data['rows'])} rows"

            if table_data["headers"] or table_data["rows"]:
                tables.append(table_data)

        return tables

    def _extract_forms(self, soup) -> list[dict[str, Any]]:
        """Extract structured form data from HTML."""
        forms: list[dict[str, Any]] = []
        for i, form in enumerate(soup.find_all("form")):
            form_data = {
                "form_id": i,
                "action": form.get("action", ""),
                "method": form.get("method", "GET").upper(),
                "fields": [],
                "summary": None,
            }

            # Extract form fields
            for field in form.find_all(["input", "textarea", "select"]):
                field_info = {
                    "type": field.name,
                    "name": field.get("name", ""),
                    "id": field.get("id", ""),
                    "placeholder": field.get("placeholder", ""),
                    "required": field.has_attr("required"),
                    "value": field.get("value", "")
                }

                if field.name == "input":
                    field_info["input_type"] = field.get("type", "text")
                elif field.name == "select":
                    options = [opt.get_text(strip=True) for opt in field.find_all("option")]
                    field_info["options"] = options

                form_data["fields"].append(field_info)

            form_data["summary"] = f"Form with {len(form_data['fields'])} fields"
            forms.append(form_data)

        return forms

    def _extract_lists(self, soup) -> list[dict[str, Any]]:
        """Extract structured list data from HTML."""
        lists: list[dict[str, Any]] = []
        for i, list_elem in enumerate(soup.find_all(["ul", "ol", "dl"])):
            list_data = {
                "list_id": i,
                "type": list_elem.name,
                "items": [],
                "summary": None,
            }

            if list_elem.name in ["ul", "ol"]:
                items = list_elem.find_all("li", recursive=False)
                list_data["items"] = [item.get_text(strip=True) for item in items]
            elif list_elem.name == "dl":
                # Definition lists
                items = []
                for dt in list_elem.find_all("dt"):
                    term = dt.get_text(strip=True)
                    definition = ""
                    dd = dt.find_next_sibling("dd")
                    if dd:
                        definition = dd.get_text(strip=True)
                    items.append({"term": term, "definition": definition})
                list_data["items"] = items

            list_data["summary"] = f"{list_elem.name.upper()} list with {len(list_data['items'])} items"
            lists.append(list_data)

        return lists
