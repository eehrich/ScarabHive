"""LLM Router plugin entrypoint for AgentSystem plugin loader."""

from .server import LLMRouterServer

PLUGIN_NAME = "llm_router"

def PLUGIN_FACTORY(name: str, config: dict | None = None, ssl_verify: bool = True):
    return LLMRouterServer(name, config=config, ssl_verify=ssl_verify)
