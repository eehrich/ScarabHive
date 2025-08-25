from .server import LLMRouterServer
from ..http_server import serve_mcp_server


def main() -> None:
    serve_mcp_server(LLMRouterServer("llm_router"))


if __name__ == "__main__":
    main()
