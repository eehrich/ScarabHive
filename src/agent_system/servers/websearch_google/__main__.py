from .server import GoogleWebSearchServer
from ..http_server import serve_mcp_server


def main() -> None:
    serve_mcp_server(GoogleWebSearchServer("websearch_google"))


if __name__ == "__main__":
    main()
