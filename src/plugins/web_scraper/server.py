from __future__ import annotations

from typing import Any
import re

from agent_system.mcp.base import MCPServer


class WebScraperServer(MCPServer):
    def _clean_text(self, text: str) -> str:
        """Clean up extracted text by removing excessive whitespace and normalizing newlines."""
        if not text:
            return ""

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

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool != "fetch":
            raise ValueError(f"Unknown tool: {tool}")

        url = params.get("url") or ""
        if not url or not isinstance(url, str):
            raise ValueError("Missing 'url' (string)")
        timeout = float(params.get("timeout", 20))
        include_html = bool(params.get("include_html", False))
        max_chars = int(params.get("max_chars", 0))
        extract_tables = bool(params.get("extract_tables", False))
        extract_forms = bool(params.get("extract_forms", False))
        extract_lists = bool(params.get("extract_lists", False))
        user_agent = params.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        )
        html: str = ""
        status_code: int = 0
        final_url: str = url

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
            # Remove script/style blocks
            no_script = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
            no_style = re.sub(r"<style[\s\S]*?</style>", " ", no_script, flags=re.IGNORECASE)
            # Title
            m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, flags=re.IGNORECASE)
            title = m.group(1).strip() if m else None
            # Strip all remaining tags
            text = re.sub(r"<[^>]+>", " ", no_style)
            # Apply the same cleaning as BeautifulSoup path
            text = self._clean_text(text)

        if max_chars and max_chars > 0:
            text = text[:max_chars]

        result: dict[str, Any] = {
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "title": title,
            "text": text,
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
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Fetch and read a web page by URL to extract text content and structured data (tables, forms, lists). Enhanced with structured data extraction to reduce parsing errors and improve data quality.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["fetch"], "description": "Use 'fetch' to download the page"},
                        "url": {"type": "string", "description": "The absolute URL to fetch"},
                        "timeout": {"type": "number", "default": 20, "description": "Request timeout in seconds"},
                        "include_html": {"type": "boolean", "default": False, "description": "Include raw HTML in response"},
                        "max_chars": {"type": "integer", "default": 0, "description": "If >0, truncate extracted text to this length"},
                        "extract_tables": {"type": "boolean", "default": False, "description": "Extract structured table data with headers and rows"},
                        "extract_forms": {"type": "boolean", "default": False, "description": "Extract form structure with fields and validation info"},
                        "extract_lists": {"type": "boolean", "default": False, "description": "Extract structured list data (ul, ol, dl)"},
                        "user_agent": {"type": "string", "description": "Custom User-Agent header for the request"},
                    },
                    "required": ["url"],
                    "additionalProperties": True,
                },
            },
        }

    def get_default_action(self) -> str:
        """Return the default action for web scraper."""
        return "fetch"
