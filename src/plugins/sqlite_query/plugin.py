"""
SQLite Query Plugin Factory

Simple SQL execution for debugging. No restrictions, no safety checks.
Follows the plugin_contributing.md guidelines.
"""

from __future__ import annotations
import logging

from .server import SqliteQueryServer

logger = logging.getLogger(__name__)


PLUGIN_FACTORY = SqliteQueryServer
