"""image_gen plugin entrypoint."""
from __future__ import annotations

from .server import ImageGenServer

PLUGIN_FACTORY = ImageGenServer
