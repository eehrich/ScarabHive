"""Twitter search plugin entrypoint (standardized)."""

from __future__ import annotations
from typing import Any

from .server import TwitterSearchServer


PLUGIN_FACTORY = TwitterSearchServer


