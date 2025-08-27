from .server import DateTimeServer

PLUGIN_NAME = "datetime"

def factory(name: str, cfg: dict | None = None, ssl_verify: bool = True):
    """Factory returning a DateTimeServer instance compatible with AgentSystem plugin contract."""
    return DateTimeServer(name, cfg or {}, ssl_verify)

PLUGIN_FACTORY = factory
