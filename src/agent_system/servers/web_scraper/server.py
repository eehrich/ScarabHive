from __future__ import annotations

from typing import Any
import re

from ...mcp.base import MCPServer


class WebScraperServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool != "fetch":
            raise ValueError(f"Unknown tool: {tool}")

        url = params.get("url") or ""
        if not url or not isinstance(url, str):
            raise ValueError("Missing 'url' (string)")
        timeout = float(params.get("timeout", 20))
        include_html = bool(params.get("include_html", False))
        max_chars = int(params.get("max_chars", 0))
        user_agent = params.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        )

        html: str = ""
        status_code: int = 0
        final_url: str = url
        headers: dict[str, Any] = {}

        # Try httpx first (async); fall back to urllib if not available
        try:
            import httpx  # type: ignore
            async with httpx.AsyncClient(
                follow_redirects=True,
                verify=self.ssl_verify,
                headers={"User-Agent": user_agent},
                timeout=timeout,
            ) as client:
                resp = await client.get(url)
                status_code = resp.status_code
                final_url = str(resp.url)
                headers = dict(resp.headers)
                # Prefer server-declared encoding; httpx handles decoding via .text
                html = resp.text or ""
        except Exception:
            # Fallback: urllib (sync) with simple decoding
            import ssl
            from urllib.request import Request, urlopen
            from urllib.error import URLError, HTTPError
            ctx = None
            if not self.ssl_verify:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            try:
                req = Request(url, headers={"User-Agent": user_agent})
                with urlopen(req, context=ctx, timeout=timeout) as r:  # type: ignore[arg-type]
                    final_url = r.geturl()
                    status_code = getattr(r, "status", 200)
                    headers = dict(getattr(r, "headers", {}))
                    data = r.read()
                    try:
                        html = data.decode("utf-8", errors="ignore")
                    except Exception:
                        html = data.decode(errors="ignore")
            except (URLError, HTTPError):
                # Bubble up a minimal error payload
                return {
                    "url": url,
                    "final_url": final_url,
                    "status_code": status_code or 0,
                    "title": None,
                    "text": "",
                    "html": html if include_html else None,
                }

        # Extract readable text and title
        title: str | None = None
        text: str = ""
        try:
            from bs4 import BeautifulSoup  # type: ignore
            # Prefer lxml if available, else fallback to html.parser
            try:
                parser = "lxml"
                import lxml  # noqa: F401
            except Exception:
                parser = "html.parser"
            soup = BeautifulSoup(html, parser)
            # Remove scripts/styles
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            title = soup.title.string.strip() if soup.title and soup.title.string else None
            text = soup.get_text("\n")
        except Exception:
            # Naive fallback: strip tags
            # Remove script/style blocks
            no_script = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
            no_style = re.sub(r"<style[\s\S]*?</style>", " ", no_script, flags=re.IGNORECASE)
            # Title
            m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, flags=re.IGNORECASE)
            title = m.group(1).strip() if m else None
            # Strip all remaining tags
            text = re.sub(r"<[^>]+>", " ", no_style)
            text = re.sub(r"\s+", " ", text).strip()

        if max_chars and max_chars > 0:
            text = text[:max_chars]

        result: dict[str, Any] = {
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "title": title,
            "text": text,
        }
        if include_html:
            result["html"] = html
        return result

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for web scraper."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Fetch and read a web page by URL to extract its text content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["fetch"], "description": "Use 'fetch' to download the page"},
                        "url": {"type": "string", "description": "The absolute URL to fetch"},
                        "timeout": {"type": "number", "default": 20, "description": "Request timeout in seconds"},
                        "include_html": {"type": "boolean", "default": False, "description": "Include raw HTML in response"},
                        "max_chars": {"type": "integer", "default": 0, "description": "If >0, truncate extracted text to this length"},
                        "user_agent": {"type": "string", "description": "Custom User-Agent header for the request"},
                    },
                    "required": ["url"],
                    "additionalProperties": True,
                },
            },
        }
