"""Blender plugin entrypoint (standardized pattern)."""

from __future__ import annotations

from .server import BlenderServer

PLUGIN_FACTORY = BlenderServer
