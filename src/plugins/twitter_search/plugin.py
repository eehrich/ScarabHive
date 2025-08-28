"""Plugin factory for twitter_search"""

from typing import Any

PLUGIN_NAME = "twitter_search"


def PLUGIN_FACTORY(key: str, cfg: dict[str, Any], ssl_verify: bool = True):
    # Lazy import to avoid heavy deps at import time
    from .server import TwitterSearchServer

    return TwitterSearchServer(key, cfg, ssl_verify=ssl_verify)

