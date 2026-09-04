"""Godot plugin entrypoint (standardized pattern)."""

from __future__ import annotations

from .server import GodotServer

PLUGIN_FACTORY = GodotServer
