"""LLM Router plugin entrypoint (standardized)."""

from __future__ import annotations
from typing import Any

from .server import LLMRouterServer


PLUGIN_FACTORY = LLMRouterServer

