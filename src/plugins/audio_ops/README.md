# Audio Operations Plugin

plugin for audio file manipulation - cutting, merging, mixing, analyzing, and optimizing audio files.

## Features

- **Cut**: Extract or remove audio segments
- **Merge**: Concatenate multiple audio files
- **Mix**: Blend two audio files with customizable balance
- **Volume**: Adjust volume with gain or envelope automation
- **Detect Silence**: Find silent segments in audio files
- **Compress Silence**: Reduce long silences to save time and space
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

### Detect Silence

Find silent segments in audio file:

```json
{
  "source_file": "podcast.flac",
  "threshold_db": -40,
  "min_duration": 0.3
}
```

Response:
```json
{
  "status": "success",
  "silence_count": 15,
  "silences": [
    {"start": 2.87, "end": 3.42, "duration": 0.55},
    {"start": 9.36, "end": 9.80, "duration": 0.44}
  ],
  "threshold_db": -40,
  "min_duration": 0.3
}
```

### Compress Silence

Reduce long silences to maximum duration:

```json
{
  "source_file": "interview.flac",
  "dest_file": "interview_compressed.mp3",
  "max_silence": 1.0,
  "threshold_db": -40,
  "mp3_bitrate": 192
}
```

Response:
```json
{
  "status": "success",
  "compressed_count": 12,
  "time_saved": 45.3,
  "original_duration": 3600.0,
  "new_duration": 3554.7
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

## Command-Line Interface

The `audio-ops` CLI provides direct access to audio operations from the terminal.

### Installation

After installing the package, the CLI is available as `audio-ops`:

```bash
audio-ops --help
```

### Commands

#### info - Show audio file metadata
```bash
audio-ops info song.mp3
```

#### cut - Extract or remove audio segment
```bash
# Extract segment from 10s to 30s
audio-ops cut input.wav output.wav -s 10 -e 30

# Remove segment (keep rest)
audio-ops cut input.wav output.wav -s 10 -e 30 -m remove
```

#### merge - Concatenate multiple audio files
```bash
# Basic merge
audio-ops merge output.mp3 part1.mp3 part2.mp3 part3.mp3

# With crossfade (500ms)
audio-ops merge output.wav a.wav b.wav --crossfade 500
```

#### mix - Blend two audio files
```bash
# Equal mix (50/50)
audio-ops mix voice.wav music.wav mixed.wav

# Custom balance: 30% voice, 70% music
audio-ops mix voice.wav music.wav mixed.wav -f 0.7
```

Mix factor: 0.0 = 100% file1, 0.5 = equal, 1.0 = 100% file2

#### volume - Adjust audio volume
```bash
# Increase volume by 6 dB
audio-ops volume input.wav louder.wav --gain 6

# Decrease volume by 12 dB
audio-ops volume input.wav quieter.wav --gain -12

# Normalize to 0 dB peak
audio-ops volume input.wav normalized.wav --normalize

# Combine: adjust and normalize
audio-ops volume input.wav output.wav --gain -3 --normalize
```

#### create - Generate silent audio
```bash
# 5 second silence (stereo, 44100 Hz)
audio-ops create silence.wav -d 5000

# 1 second mono at 48 kHz
audio-ops create beep.wav -d 1000 -r 48000 -c 1
```

#### list - Browse audio files
```bash
# List all audio files
audio-ops list

# Filter by pattern
audio-ops list "*.wav"
```

#### load - Load audio file (with optional segment)
```bash
# Load full file info
audio-ops load song.mp3

# Load segment (10s to 30s)
audio-ops load song.mp3 -s 10 -e 30
```

### Storage Path

The CLI uses the storage path from `config/plugins.yaml`:

```yaml
plugins:
  servers:
    audio_ops:
      storage_path: "data/audio_ops"
```

All files are relative to this directory.

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
