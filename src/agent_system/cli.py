from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config.loader import load_config
from .mcp.base import MCPRegistry
from .agent.core import Agent
from .servers.bootstrap import bootstrap_servers


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent System CLI")
    parser.add_argument("task", nargs="?", default="What can you do?", help="Task to run")
    parser.add_argument("--config", dest="config", default=str(Path("config/agent.yaml")), help="Path to config")
    args = parser.parse_args()

    config = load_config(args.config)
    # Apply SSL bypass if configured
    if not config.network.ssl_verify:
        import os
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        os.environ.setdefault("SSL_CERT_FILE", "")
        os.environ.setdefault("CURL_CA_BUNDLE", "")
        os.environ.setdefault("REQUESTS_CA_BUNDLE", "")
    registry = MCPRegistry()
    bootstrap_servers(config, registry)
    agent = Agent(config, registry)

    result = asyncio.run(agent.run(args.task))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
