PLUGIN_NAME = "duckduckgo_search"

class DuckDuckGoServer:
    def __init__(self, name, cfg=None, ssl_verify=True):
        self.name = name
        self.cfg = cfg or {}
        self.ssl_verify = ssl_verify

    async def call(self, tool: str, params: dict):
        # Simple wrapper delegating to server implementation in server.py
        from .server import DuckDuckGoSearchServer
        srv = DuckDuckGoSearchServer(self.name, self.cfg, ssl_verify=self.ssl_verify)
        return await srv.call(tool, params)

    def get_schema(self):
        # Delegate schema to the underlying implementation
        from .server import DuckDuckGoSearchServer
        srv = DuckDuckGoSearchServer(self.name, self.cfg, ssl_verify=self.ssl_verify)
        return srv.get_schema()

    def get_default_action(self):
        from .server import DuckDuckGoSearchServer
        srv = DuckDuckGoSearchServer(self.name, self.cfg, ssl_verify=self.ssl_verify)
        return srv.get_default_action()

PLUGIN_FACTORY = DuckDuckGoServer
