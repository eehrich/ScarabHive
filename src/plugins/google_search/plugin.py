PLUGIN_NAME = "google_search"

def PLUGIN_FACTORY(name: str, config: dict | None = None, ssl_verify: bool = True):
    from plugins.google_search.server import GoogleSearchServer
    return GoogleSearchServer(name, config=config, ssl_verify=ssl_verify)
