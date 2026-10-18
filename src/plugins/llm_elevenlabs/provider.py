"""Registry exports of the ElevenLabs plugin (manifest key provides_tts)."""


def build_elevenlabs_tts(cfg):
    """TTS factory; lazy so the client only loads when an elevenlabs model is built."""
    from .elevenlabs_tts_client import build_elevenlabs
    return build_elevenlabs(cfg)


TTS_PROVIDERS = {"elevenlabs": build_elevenlabs_tts}
