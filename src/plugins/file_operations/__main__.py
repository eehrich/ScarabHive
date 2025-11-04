"""Run file_operations plugin CLI."""

from .cli import main as async_main
import asyncio
import sys


def main():
    """Entry point wrapper for async main."""
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())
