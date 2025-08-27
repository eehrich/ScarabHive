PLUGIN_NAME = "duckduckgo_search"

class DuckDuckGoServer:
    def __init__(self, name, cfg=None, ssl_verify=True):
        self.name = name
        self.cfg = cfg or {}
        self.ssl_verify = ssl_verify

    async def call(self, tool: str, params: dict):
        # Simple wrapper delegating to server implementation in server.py
        from .server import DuckDuckGoSearchServer
        srv = DuckDuckGoSearchServer(self.name)
        return await srv.call(tool, params)


PLUGIN_FACTORY = DuckDuckGoServer
