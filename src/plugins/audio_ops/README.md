# Audio operations

Lets agents edit FLAC, MP3 and WAV files in one storage folder -- cut, join, mix, change the volume, find and
shorten pauses, make silence -- and play a file or a piece of it to a model that takes audio input.

- **Tools** `info`, `list`, `load`, `cut`, `merge`, `mix`, `volume`, `create`, `detect_silence`,
  `compress_silence` (prefixed with the instance name). Every file name counts inside `storage_path`;
  `{session_id}` in it gives each session its own folder.
- **Command line** `audio-ops` does the same jobs from a terminal.
- No hooks, no panel.

Enable it in `config/plugins.yaml` (`audio_ops: {type: audio_ops, enabled: true, storage_path: "data/audio_ops"}`)
and allow `+audio_ops/*` in an agent's tool list. Needs pydub, numpy and ffmpeg/ffprobe on the path.

The full manual -- every parameter and answer, the path rules, what the model hears and what it costs, the
command line and the server settings -- is the plugin's guide, `audio_ops.guide`, in the Help panel.
