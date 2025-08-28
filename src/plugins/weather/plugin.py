"""Weather plugin entrypoint for AgentSystem plugin loader."""

from .server import WeatherServer

PLUGIN_NAME = "weather"

def PLUGIN_FACTORY(name: str, config: dict | None = None, ssl_verify: bool = True):
    return WeatherServer(name, config=config, ssl_verify=ssl_verify)
