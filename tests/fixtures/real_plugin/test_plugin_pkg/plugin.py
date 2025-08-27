from agent_system.mcp.base import MCPServer

class RealExampleServer:
    def __init__(self, name, cfg=None, ssl_verify=True):
        self.name = name
        self.cfg = cfg
        self.ssl_verify = ssl_verify

    async def call(self, *a, **k):
        return {"status": "real", "name": self.name}


def factory(name=None, cfg=None, ssl_verify=True):
    return RealExampleServer(name or 'real_example', cfg or {})
