from .server import AbstractWebSearchServer
from ..http_server import serve_mcp_server


def main() -> None:
    serve_mcp_server(AbstractWebSearchServer("websearch_abstract"))


if __name__ == "__main__":
    main()
