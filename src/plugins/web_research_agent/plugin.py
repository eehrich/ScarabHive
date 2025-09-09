PLUGIN_NAME = "web_research_agent"

class WebResearchAgentServer:
    def __init__(self, name, cfg=None, ssl_verify=True):
        self.name = name
        self.cfg = cfg or {}
        self.ssl_verify = ssl_verify

    @property
    def description(self):
        # Get description from config or provide default
        return self.cfg.get("description", "Specialized web research agent combining DuckDuckGo search with web scraping capabilities")

    async def call(self, tool: str, params: dict):
        # Simple wrapper delegating to server implementation in server.py
        from .server import WebResearchAgent
        srv = WebResearchAgent(self.name, self.cfg, ssl_verify=self.ssl_verify)
        return await srv.call(tool, params)

    def get_schema(self):
        # Delegate schema to the underlying implementation
        from .server import WebResearchAgent
        srv = WebResearchAgent(self.name, self.cfg, ssl_verify=self.ssl_verify)
        # This will raise if schema.yaml is missing, as required
        return srv.get_schema()

    def get_default_action(self):
        from .server import WebResearchAgent
        srv = WebResearchAgent(self.name, self.cfg, ssl_verify=self.ssl_verify)
        return srv.get_default_action()

PLUGIN_FACTORY = WebResearchAgentServer
