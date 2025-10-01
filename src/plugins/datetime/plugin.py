"""Datetime plugin entrypoint (standardized pattern)."""

from __future__ import annotations

from .server import DateTimeServer


PLUGIN_FACTORY = DateTimeServer
