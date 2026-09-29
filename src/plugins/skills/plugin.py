"""Skills plugin entrypoint (standardized pattern)."""

from __future__ import annotations

from .server import SkillsServer


PLUGIN_FACTORY = SkillsServer
