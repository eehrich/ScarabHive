#!/usr/bin/env python3
"""Main entry point for the Script Interpreter MCP Plugin."""

import asyncio
import logging
import sys
from pathlib import Path


def cli_main():
    """Main entry point."""
    # Add src to path for imports
    src_path = Path(__file__).parent.parent.parent
    sys.path.insert(0, str(src_path))

    # Import after path setup
    from plugins.script_interpreter.server import main

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    asyncio.run(main())


if __name__ == "__main__":
    cli_main()