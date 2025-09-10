"""LLM Router plugin entrypoint (standardized)."""

from __future__ import annotations

from .server import LLMRouterServer


PLUGIN_FACTORY = LLMRouterServer

