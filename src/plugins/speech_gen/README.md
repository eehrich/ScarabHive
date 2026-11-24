# speech_gen

Synthesizes speech from text through configured `llm_system.tts_profiles` (Gemini TTS, ElevenLabs, OpenAI / OpenRouter
Speech), writes 16-bit PCM WAV files inside a path sandbox, and optionally attaches an audio preview for audio-capable
models.

- **Tools** `<instance>_synthesize` (single-speaker, optional multi-speaker where supported, opt-in voice cloning) and
  `<instance>_list_profiles`.
- **Sandboxes & guardrails** Separate `PathSandbox` instances for writing (`output_directories`, default
  `data/speech_gen`) and reading reference audio (`input_directories`, default same as `output_directories`),
  `allow_voice_cloning: false` by default, and a per-call `max_text_chars` limit (default `5000`).
- **Hooks / panel** -- none. `file_checkpoints` counts `<instance>_synthesize` as an untracked write.

Enable an instance in `config/plugins.yaml` (`type: speech_gen`, `output_directories`, optional `profiles`,
`default_profile`, `allow_voice_cloning`) and allow `+<instance>/*` in an agent's tool list.

The full manual -- tools, parameters, model experience, settings and known gaps -- is `speech_gen.guide` in the Help
panel.
