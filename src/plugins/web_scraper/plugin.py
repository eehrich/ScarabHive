PLUGIN_NAME = "web_scraper"

def PLUGIN_FACTORY(name, cfg, ssl_verify=True):
    # Lazy import to avoid heavy deps at import time
    from .server import WebScraperServer

    inst = WebScraperServer(name, cfg or {}, ssl_verify=ssl_verify)

    # Attach some metadata for bootstrap logging and discovery
    PLUGIN_FACTORY._plugin_metadata = {
        "name": "web_scraper",
        "version": "0.1.0",
        "author": "Enrico Ehrich",
        "description": "Fetch and extract readable text and structured data (tables/forms/lists) from web pages.",
    }
    return inst
