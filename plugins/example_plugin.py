PLUGIN_NAME = "example"

class ExampleServer:
    def __init__(self, name, cfg, ssl_verify=True):
        self.name = name
        self.cfg = cfg
        self.ssl_verify = ssl_verify

    async def call(self, *args, **kwargs):
        return {"status": "ok", "name": self.name}

PLUGIN_FACTORY = ExampleServer
