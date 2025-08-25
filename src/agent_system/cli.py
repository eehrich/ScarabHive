from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config.loader import load_config
from .mcp.base import MCPRegistry
from .agent.core import Agent
from .servers.bootstrap import bootstrap_servers
from .utils.logging import setup_logging
import logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent System CLI")
    parser.add_argument("task", nargs="?", default="What can you do?", help="Task to run")
    parser.add_argument("--config", dest="config", default=str(Path("config/agent.yaml")), help="Path to config")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print progress messages")
    args = parser.parse_args()

    def vprint(msg: str) -> None:
        if args.verbose:
            print(msg, flush=True)

    vprint(f"[cli] verbose mode on")
    vprint(f"[cli] loading config: {args.config}")
    config = load_config(args.config)
    # Setup logging from config; file handler is created here. Console level is adjusted below.
    log_file = setup_logging(config.logging.enabled, config.logging.level, config.logging.file)
    logger = logging.getLogger(__name__)
    # If verbose not set, reduce console output to WARNING to avoid noisy logs on stdout
    if not args.verbose:
        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            # FileHandler is a subclass of StreamHandler — exclude it
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.WARNING)
    if log_file:
        logger.info("Logging initialized, file=%s", log_file)
    # Apply SSL bypass if configured
    if not config.network.ssl_verify:
        import os
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        os.environ.setdefault("SSL_CERT_FILE", "")
        os.environ.setdefault("CURL_CA_BUNDLE", "")
        os.environ.setdefault("REQUESTS_CA_BUNDLE", "")
    registry = MCPRegistry()
    vprint("[cli] bootstrapping servers...")
    logger.info("Bootstrapping servers")
    bootstrap_servers(config, registry)
    vprint(f"[cli] servers registered: {', '.join(registry.list())}")
    logger.info("Servers registered: %s", ", ".join(registry.list()))
    agent = Agent(config, registry)

    vprint(f"[cli] running task: {args.task}")
    logger.info("Running task: %s", args.task)
    result = asyncio.run(agent.run(args.task))
    vprint("[cli] done")
    logger.info("Task completed")
    # Always print the JSON result to stdout for consumption
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
