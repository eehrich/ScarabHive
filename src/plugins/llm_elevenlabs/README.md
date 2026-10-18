# ElevenLabs Text to Speech

ElevenLabs as a TTS provider of the LLM registry. An LLM provider plugin: no tools, no hooks, no panel.

- **`elevenlabs`** -- `POST /v1/text-to-speech/{voice_id}` over httpx, raw 24 kHz PCM back. A voice is an id of the
  account; a reference sample becomes an Instant Voice Clone, uploaded once and remembered by the sample's digest
  (`data/cache/elevenlabs_clones.json`).
- `eleven_v4` reads IPA in slashes inside a line (`/kvɔʁk/`); the client says so (`supports_inline_ipa`), and
  writer_audio then sends a book's IPA pronunciation entries along.

Nothing to enable: name the provider in a TTS model entry of `config/llm.yaml` and point a profile at it. The key is
the entry's `api_key`, else `ELEVENLABS_API_KEY`.

The manual -- entry keys, clones, what the hooks see, errors -- is the plugin's guide, `llm_elevenlabs.guide`, in the
Help panel.

## License

Apache-2.0 -- see `LICENSE`.
