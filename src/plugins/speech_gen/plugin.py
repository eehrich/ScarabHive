"""speech_gen plugin entrypoint."""
from __future__ import annotations

from .server import SpeechGenServer

PLUGIN_FACTORY = SpeechGenServer
