from __future__ import annotations

from ..http_server import serve_mcp_server
from .server import GoogleSearchServer


def main() -> None:
    serve_mcp_server(GoogleSearchServer("google_search"))


if __name__ == "__main__":
    main()
