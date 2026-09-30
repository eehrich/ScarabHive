# Google Gemini Providers

Google's Gemini models, reached directly -- no gateway in between. An LLM provider plugin: no tools, no hooks, no
panel.

- **`gemini`** -- Gemini's REST API over httpx.
- **`gemini_sdk`** -- the same API through Google's `google-genai` SDK.
- **`gemini` (batch)** -- the Gemini Batch API, for `provider: batch` entries.
- **`gemini_tts`** -- text to speech with Gemini's TTS models.

Nothing to enable: name one of the providers in a model entry of `config/llm.yaml` and point a profile at it. The
API key is the entry's `api_key`, else `GEMINI_API_KEY` or `GOOGLE_API_KEY`. Unlike Gemini behind OpenRouter, these
providers honour `safety_settings`.

The full manual -- which provider to take, every model entry key, timeouts and streaming, retries, thinking, what
the hooks and cost figures see, errors, batch and speech -- is the plugin's guide, `llm_gemini.guide`, in the Help
panel.

## License

Apache-2.0 -- see `LICENSE`.
