from .server import DuckDuckGoSearchServer
from ..http_server import serve_mcp_server


def main() -> None:
    serve_mcp_server(DuckDuckGoSearchServer("duckduckgo_search"))


if __name__ == "__main__":
    main()
