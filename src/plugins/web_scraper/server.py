from __future__ import annotations

from typing import Any
import re
import urllib.parse
from pathlib import Path

from agent_system.mcp.base import MCPServer
from agent_system.mcp.improved_status import (
    publish_status_improved,
    StatusPhase,
)


class WebScraperServer(MCPServer):
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

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        # support two actions: 'fetch' (existing) and 'links' (new)
        action = params.get("action") or params.get("task") or "fetch"

        # normalize action name when provided as top-level tool param
        if tool and tool != "fetch":
            # Allow callers to specify action via tool parameter (backwards compat)
            action = tool

        if action not in ("fetch", "links"):
            raise ValueError(f"Unknown action: {action}")

        url = params.get("url") or ""
        if not url or not isinstance(url, str):
            raise ValueError("Missing 'url' (string)")

            # correlate status events with provided request_id when available
        request_id = params.get("request_id") or params.get("requestId")

        timeout = float(params.get("timeout", 20))
        user_agent = params.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        )

        include_html = bool(params.get("include_html", False))
        max_chars = int(params.get("max_chars", 0))
        extract_tables = bool(params.get("extract_tables", False))
        extract_forms = bool(params.get("extract_forms", False))
        extract_lists = bool(params.get("extract_lists", False))

        # Links-specific options
        include_nofollow = bool(params.get("include_nofollow", False))
        only_same_domain = bool(params.get("only_same_domain", False))
        max_links = int(params.get("max_links", 0))

        async def _fetch_html(target_url: str) -> tuple[str, int, str, str]:
            html: str = ""
            status_code: int = 0
            final_url: str = target_url
            content_type: str = ""
            try:
                import httpx  # type: ignore
                from httpx import ReadTimeout, RequestError
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    verify=self.ssl_verify,
                    headers={"User-Agent": user_agent},
                    timeout=timeout,
                ) as client:
                    try:
                        resp = await client.get(target_url)
                        status_code = resp.status_code
                        final_url = str(resp.url)
                        content_type = resp.headers.get("content-type", "").lower()

                        # Check if content is actually HTML/text before processing
                        if any(ct in content_type for ct in ["text/html", "text/plain", "application/xml", "text/xml"]):
                            html = resp.text or ""
                        else:
                            # Non-HTML content detected
                            html = f"[Non-HTML content detected: {content_type}. Content type not supported for text extraction.]"
                    except ReadTimeout:
                        try:
                            await publish_status_improved(
                                self.name,
                                f"Timeout fetching {target_url}",
                                request_id=request_id,
                                level="error",
                                phase=StatusPhase.ERROR,
                                meta={"error": "read_timeout", "timeout": timeout},
                            )
                        except Exception:
                            pass
                        return "", 0, target_url, ""
                    except RequestError as err:
                        try:
                            await publish_status_improved(
                                self.name,
                                f"Request error fetching {target_url}: {err}",
                                request_id=request_id,
                                level="error",
                                phase=StatusPhase.ERROR,
                                meta={"error": str(err)},
                            )
                        except Exception:
                            pass
                        return "", 0, target_url, ""
            except Exception:
                # Fallback sync approach
                import ssl
                from urllib.request import Request, urlopen
                from urllib.error import URLError, HTTPError
                ctx = None
                if not self.ssl_verify:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                try:
                    req = Request(target_url, headers={"User-Agent": user_agent})
                    with urlopen(req, context=ctx, timeout=timeout) as r:  # type: ignore[arg-type]
                        final_url = r.geturl()
                        status_code = getattr(r, "status", 200)
                        content_type = r.headers.get("content-type", "").lower()
                        data = r.read()
                        
                        # Check content type and data header for binary content
                        if any(ct in content_type for ct in ["text/html", "text/plain", "application/xml", "text/xml"]) and not data.startswith(b'%PDF'):
                            try:
                                html = data.decode("utf-8", errors="ignore")
                            except Exception:
                                html = data.decode(errors="ignore")
                        else:
                            # Non-HTML content detected
                            html = f"[Non-HTML content detected: {content_type}. Content type not supported for text extraction.]"
                except (URLError, HTTPError):
                    return "", status_code or 0, final_url, content_type

            return html, status_code, final_url, content_type

        # fetch HTML (async) and parse according to requested action
        # notify start of fetch
        try:
            await publish_status_improved(self.name, f"Fetching {url}", request_id=request_id, phase=StatusPhase.START)
        except Exception:
            # status publishing must not break functionality
            pass

        html, status_code, final_url, content_type = await _fetch_html(url)

        # Sanitize HTML before processing to remove problematic characters
        html = self._sanitize_html(html)

        # If fetch failed, publish error and return minimal payload
        if not html and status_code == 0:
            try:
                await publish_status_improved(
                    self.name,
                    f"Failed to fetch {url}",
                    request_id=request_id,
                    level="error",
                    phase=StatusPhase.ERROR,
                )
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
                await publish_status_improved(
                    self.name,
                    f"Completed fetch {url} - non-HTML content detected ({content_type})",
                    request_id=request_id,
                    phase=StatusPhase.END,
                    meta={"final_url": final_url, "status_code": status_code, "content_type": content_type},
                )
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
        if action == "links":
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

            return {"url": url, "final_url": final_url, "status_code": status_code, "links": links}

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
            await publish_status_improved(
                self.name,
                f"Completed fetch {url} (status={status_code})",
                request_id=request_id,
                phase=StatusPhase.END,
                meta={"final_url": final_url, "status_code": status_code, "content_type": content_type},
            )
        except Exception:
            pass
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

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for web scraper."""
        # Try loading schema from a schema.yaml in the plugin directory
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema = load_schema_from_dir(Path(__file__).parent, template_vars={"name": self.name})
        if not schema:
            raise RuntimeError("Missing required schema.yaml for web_scraper plugin")
        return schema

    def _get_default_action_impl(self) -> str:
        """Return the default action for web scraper."""
        return "fetch"
