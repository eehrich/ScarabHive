from .server import TwitterSearchServer
from ..http_server import serve_mcp_server


def main() -> None:
    serve_mcp_server(TwitterSearchServer("twitter_search"))


if __name__ == "__main__":
    main()
