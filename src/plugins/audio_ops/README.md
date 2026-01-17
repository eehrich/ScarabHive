# Audio Operations Plugin

MCP plugin for audio file manipulation - cutting, merging, mixing, and analyzing audio files.

## Features

- **Cut**: Extract or remove audio segments
- **Merge**: Concatenate multiple audio files
- **Mix**: Blend two audio files with customizable balance
- **Volume**: Adjust volume with gain or envelope automation
- **Info**: Get audio file metadata (duration, sample rate, channels)
- **List**: Browse available audio files
- **Load**: Load audio for LLM analysis (multimodal)
- **Create**: Generate silent audio files

## Supported Formats

- **FLAC** - Lossless, recommended for quality
- **WAV** - Uncompressed
- **MP3** - Compressed

## Installation

Requires `pydub` and `ffmpeg`:

```bash
pip install pydub
# ffmpeg must be in PATH or installed via system package manager
```

## Configuration

### Basic Configuration

In `config/plugins.yaml`:

```yaml
plugins:
  servers:
    audio_ops:
      type: audio_ops
      enabled: true
      storage_path: "data/audio_ops"
```

### Session-Based Isolation

For multi-agent scenarios where multiple agents may work with audio files concurrently, use the `{session_id}` template in `storage_path` to isolate files per session:

```yaml
plugins:
  servers:
    writer_audio_ops:
      type: audio_ops
      enabled: true
      # Each session gets its own subdirectory
      storage_path: "data/writer/audio/temp/{session_id}"
```

When `{session_id}` is present in the path:
- Each agent session gets an isolated directory (e.g., `data/writer/audio/temp/sess_abc123/`)
- Prevents file conflicts when multiple agents work in parallel
- If no session_id is available, falls back to the base path (without `{session_id}`)

**Use case**: Multiple audio engineers processing TTS files simultaneously - each gets their own working directory.

## Usage

### Cut Audio

Extract a segment:
```json
{
  "source_file": "interview.flac",
  "dest_file": "intro.flac",
  "start_time": 0.0,
  "end_time": 30.0,
  "mode": "extract"
}
```

Remove a segment (keep rest):
```json
{
  "source_file": "interview.flac",
  "dest_file": "clean.flac",
  "start_time": 45.0,
  "end_time": 60.0,
  "mode": "remove"
}
```

### Merge Audio

```json
{
  "source_files": ["intro.flac", "main.flac", "outro.flac"],
  "dest_file": "combined.flac",
  "crossfade_ms": 500
}
```

### Mix Audio

Blend two tracks with custom balance:
```json
{
  "file1": "voice.flac",
  "file2": "music.flac",
  "dest_file": "final.flac",
  "mix_factor": 0.8
}
```

Mix with envelope (automation):
```json
{
  "file1": "voice.flac",
  "file2": "music.flac",
  "dest_file": "final.flac",
  "mix_envelope": [
    {"time": 0.0, "factor": 0.2},
    {"time": 5.0, "factor": 0.8},
    {"time": 30.0, "factor": 0.2}
  ]
}
```

### Volume Adjustment

Apply gain:
```json
{
  "source_file": "quiet.flac",
  "dest_file": "loud.flac",
  "gain_db": 6.0,
  "normalize": true
}
```

Apply envelope:
```json
{
  "source_file": "source.flac",
  "dest_file": "faded.flac",
  "envelope": [
    {"time": 0.0, "gain_db": -20.0},
    {"time": 2.0, "gain_db": 0.0},
    {"time": 58.0, "gain_db": 0.0},
    {"time": 60.0, "gain_db": -20.0}
  ]
}
```

### Get Info

```json
{
  "file": "interview.flac"
}
```

Response:
```json
{
  "status": "success",
  "file": "interview.flac",
  "duration_seconds": 3600.5,
  "sample_rate": 44100,
  "channels": 2,
  "format": "flac",
  "size_bytes": 125000000
}
```

### List Files

```json
{
  "pattern": "*.flac"
}
```

### Load for LLM Analysis

```json
{
  "file": "sample.flac",
  "start_time": 10.0,
  "end_time": 20.0
}
```

Returns `_multimodal_content` for LLM audio analysis.

### Create Silent Audio

```json
{
  "dest_file": "silence.flac",
  "duration_ms": 5000,
  "sample_rate": 44100,
  "channels": 2
}
```

## Security

- All file operations are restricted to the configured `storage_path`
- Path traversal attempts (e.g., `../`) are blocked
- Absolute paths outside storage are rejected

## Integration with ComfyUI

When used with the ComfyUI plugin for TTS generation, configure both plugins to use the same base directory with session isolation:

```yaml
plugins:
  servers:
    writer_tts_comfyui:
      type: comfyui
      output_dir: "data/writer/audio/temp/{session_id}"
      # ...
    
    writer_audio_ops:
      type: audio_ops
      storage_path: "data/writer/audio/temp/{session_id}"
```

This ensures both plugins work with the same session-isolated directories.
