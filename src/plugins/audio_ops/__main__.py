#!/usr/bin/env python
"""Audio Operations CLI - Command-line interface for audio manipulation.

Usage:
    audio-ops info <file>                              - Show audio file metadata
    audio-ops cut <src> <dest> -s <sec> -e <sec>       - Extract segment (default)
    audio-ops cut <src> <dest> -s <sec> -e <sec> -m remove  - Remove segment
    audio-ops merge <dest> <file1> <file2> [...]       - Merge files
    audio-ops mix <file1> <file2> <dest> [-f 0.5]      - Mix two files
    audio-ops volume <src> <dest> --gain <dB>          - Adjust volume
    audio-ops create <dest> -d <ms>                    - Create silent audio
    audio-ops detect-silence <file>                    - Find silent segments
    audio-ops compress-silence <src> <dest>            - Compress long silences
    audio-ops list [pattern]                           - List audio files
    audio-ops load <file>                              - Load/info about file
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Global workdir override (set by CLI --workdir)
_workdir_override: Path | None = None


def get_config() -> dict:
    """Load audio_ops plugin configuration."""
    import yaml
    
    possible_paths = [
        Path.cwd() / "config" / "plugins.yaml",
        Path(__file__).parent.parent.parent.parent / "config" / "plugins.yaml",
        Path(__file__).parent.parent.parent / "config" / "plugins.yaml",
    ]
    
    config_path = None
    for path in possible_paths:
        if path.exists():
            config_path = path
            break
    
    if not config_path:
        print("Error: Config file not found.", file=sys.stderr)
        sys.exit(1)
    
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    
    audio_config = config.get("plugins", {}).get("servers", {}).get("audio_ops", {})
    if not audio_config:
        # Return default config
        return {"storage_path": "data/audio_ops"}
    
    return audio_config


def get_storage_path() -> Path:
    """Get storage path from config or CLI override."""
    global _workdir_override
    if _workdir_override is not None:
        return _workdir_override
    config = get_config()
    return Path(config.get("storage_path", "data/audio_ops"))


def _safe_join(storage: Path, filename: str) -> Path:
    """Join filename onto storage, rejecting path traversal.

    Mirrors server.py:_validate_path: reject empty input, embedded NUL bytes,
    and literal '..' segments, then confirm the resolved path is inside the
    resolved storage directory. Raises ValueError on violation.
    """
    if not filename:
        raise ValueError("Invalid filename: empty")
    if "\x00" in filename:
        raise ValueError(f"Invalid filename: {filename!r} (NUL byte not allowed)")
    if ".." in Path(filename).parts:
        raise ValueError(f"Invalid filename: {filename!r} (path traversal not allowed)")
    resolved = (storage / filename).resolve()
    try:
        resolved.relative_to(storage.resolve())
    except ValueError:
        raise ValueError(f"Invalid filename: {filename!r} (escapes storage directory)")
    return resolved


def _safe_glob_pattern(pattern: str) -> str:
    """Validate a glob pattern for cmd_list.

    Path.glob in CPython 3.12+ honours '..' segments and traverses outside
    the search root, and absolute patterns either raise or escape. Reject
    both so the CLI matches the same safety envelope as _safe_join.
    """
    if not pattern:
        raise ValueError("Invalid pattern: empty")
    if "\x00" in pattern:
        raise ValueError(f"Invalid pattern: {pattern!r} (NUL byte not allowed)")
    # Reject absolute patterns (POSIX '/' prefix or Windows drive/UNC).
    p = Path(pattern)
    if p.is_absolute() or pattern.startswith(("/", "\\")):
        raise ValueError(f"Invalid pattern: {pattern!r} (absolute paths not allowed)")
    if ".." in p.parts:
        raise ValueError(f"Invalid pattern: {pattern!r} (path traversal not allowed)")
    return pattern


async def cmd_info(args: argparse.Namespace) -> int:
    """Show audio file info."""
    try:
        from pydub import AudioSegment
    except ImportError:
        print("Error: pydub not installed. Run: pip install pydub", file=sys.stderr)
        return 1
    
    storage = get_storage_path()
    try:
        filepath = _safe_join(storage, args.file)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not filepath.exists():
        print(f"Error: File not found: {filepath}", file=sys.stderr)
        return 1

    ext = filepath.suffix.lower()
    if ext not in {".flac", ".mp3", ".wav"}:
        print(f"Error: Unsupported format: {ext}", file=sys.stderr)
        return 1

    try:
        audio = AudioSegment.from_file(str(filepath), format=ext[1:])
        duration = len(audio) / 1000.0

        print(f"File: {filepath.name}")
        print(f"  Format:      {ext[1:].upper()}")
        print(f"  Duration:    {duration:.2f} seconds")
        print(f"  Channels:    {audio.channels}")
        print(f"  Sample rate: {audio.frame_rate} Hz")
        print(f"  Sample width: {audio.sample_width * 8} bits")
        print(f"  File size:   {filepath.stat().st_size:,} bytes")
        return 0
    except Exception as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        return 1


async def cmd_cut(args: argparse.Namespace) -> int:
    """Cut audio segment (extract or remove)."""
    try:
        from pydub import AudioSegment
    except ImportError:
        print("Error: pydub not installed. Run: pip install pydub", file=sys.stderr)
        return 1
    
    storage = get_storage_path()
    storage.mkdir(parents=True, exist_ok=True)

    try:
        source_path = _safe_join(storage, args.source)
        dest_path = _safe_join(storage, args.dest)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    mode = args.mode
    
    if not source_path.exists():
        print(f"Error: Source file not found: {source_path}", file=sys.stderr)
        return 1
    
    src_ext = source_path.suffix.lower()
    dest_ext = dest_path.suffix.lower()
    
    for ext in [src_ext, dest_ext]:
        if ext not in {".flac", ".mp3", ".wav"}:
            print(f"Error: Unsupported format: {ext}", file=sys.stderr)
            return 1
    
    try:
        audio = AudioSegment.from_file(str(source_path), format=src_ext[1:])
        duration = len(audio) / 1000.0
        
        if args.start < 0:
            print("Error: start time cannot be negative", file=sys.stderr)
            return 1
        
        if args.end <= args.start:
            print("Error: end time must be greater than start time", file=sys.stderr)
            return 1
        
        if args.start >= duration:
            print(f"Error: start time ({args.start}s) exceeds duration ({duration:.2f}s)", file=sys.stderr)
            return 1
        
        if args.end > duration:
            print(f"Error: end time ({args.end}s) exceeds duration ({duration:.2f}s)", file=sys.stderr)
            return 1
        
        start_ms = int(args.start * 1000)
        end_ms = int(args.end * 1000)
        
        if mode == "extract":
            result = audio[start_ms:end_ms]
            operation = f"Extracted {args.start}s-{args.end}s"
        else:  # remove
            before = audio[:start_ms]
            after = audio[end_ms:]
            result = before + after
            operation = f"Removed {args.start}s-{args.end}s"
        
        result.export(str(dest_path), format=dest_ext[1:])
        
        result_duration = len(result) / 1000.0
        print(f"Created: {dest_path.name}")
        print(f"   Mode: {mode}")
        print(f"   Duration: {result_duration:.2f} seconds")
        print(f"   Operation: {operation} from {source_path.name}")
        return 0
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_merge(args: argparse.Namespace) -> int:
    """Merge multiple audio files."""
    try:
        from pydub import AudioSegment
    except ImportError:
        print("Error: pydub not installed. Run: pip install pydub", file=sys.stderr)
        return 1
    
    storage = get_storage_path()
    storage.mkdir(parents=True, exist_ok=True)

    try:
        dest_path = _safe_join(storage, args.dest)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    dest_ext = dest_path.suffix.lower()

    if dest_ext not in {".flac", ".mp3", ".wav"}:
        print(f"Error: Unsupported output format: {dest_ext}", file=sys.stderr)
        return 1

    source_files = args.sources
    if len(source_files) < 2:
        print("Error: At least 2 source files required", file=sys.stderr)
        return 1

    crossfade = args.crossfade

    try:
        segments = []
        total_duration = 0.0

        for filename in source_files:
            try:
                filepath = _safe_join(storage, filename)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            if not filepath.exists():
                print(f"Error: File not found: {filepath}", file=sys.stderr)
                return 1
            
            ext = filepath.suffix.lower()
            if ext not in {".flac", ".mp3", ".wav"}:
                print(f"Error: Unsupported format: {ext} ({filename})", file=sys.stderr)
                return 1
            
            audio = AudioSegment.from_file(str(filepath), format=ext[1:])
            segments.append(audio)
            total_duration += len(audio) / 1000.0
            print(f"  Loaded: {filename} ({len(audio)/1000:.2f}s)")
        
        print(f"\nMerging {len(segments)} files...")
        
        if crossfade > 0:
            result = segments[0]
            for segment in segments[1:]:
                actual_crossfade = min(crossfade, len(result), len(segment))
                result = result.append(segment, crossfade=actual_crossfade)
        else:
            result = AudioSegment.empty()
            for segment in segments:
                result += segment
        
        result.export(str(dest_path), format=dest_ext[1:])
        
        result_duration = len(result) / 1000.0
        print(f"\nCreated: {dest_path.name}")
        print(f"   Files merged: {len(source_files)}")
        print(f"   Total source duration: {total_duration:.2f}s")
        print(f"   Result duration: {result_duration:.2f}s")
        if crossfade > 0:
            print(f"   Crossfade: {crossfade}ms")
        return 0
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_list(args: argparse.Namespace) -> int:
    """List audio files."""
    try:
        from pydub import AudioSegment
    except ImportError:
        print("Error: pydub not installed. Run: pip install pydub", file=sys.stderr)
        return 1
    
    storage = get_storage_path()
    
    if not storage.exists():
        print(f"Storage directory does not exist: {storage}")
        return 0
    
    pattern = args.pattern
    files: list[Path] = []

    if pattern:
        try:
            pattern = _safe_glob_pattern(pattern)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        files.extend(storage.glob(pattern))
    else:
        for ext in [".flac", ".mp3", ".wav"]:
            files.extend(storage.glob(f"*{ext}"))
    
    seen: set[Path] = set()
    audio_files: list[Path] = []
    for f in files:
        if f.suffix.lower() in {".flac", ".mp3", ".wav"} and f.is_file() and f not in seen:
            audio_files.append(f)
            seen.add(f)
    
    if not audio_files:
        print(f"No audio files found in {storage}")
        return 0
    
    print(f"Audio files in {storage}:\n")
    print(f"{'Name':<40} {'Duration':>10} {'Format':>8} {'Size':>12}")
    print("-" * 74)
    
    for filepath in sorted(audio_files):
        try:
            audio = AudioSegment.from_file(str(filepath), format=filepath.suffix[1:].lower())
            duration = f"{len(audio) / 1000.0:.2f}s"
        except Exception:
            duration = "???"
        
        size = f"{filepath.stat().st_size:,}"
        print(f"{filepath.name:<40} {duration:>10} {filepath.suffix[1:].upper():>8} {size:>12}")
    
    print(f"\nTotal: {len(audio_files)} files")
    return 0


async def cmd_load(args: argparse.Namespace) -> int:
    """Load audio file (with optional segment extraction)."""
    try:
        from pydub import AudioSegment
    except ImportError:
        print("Error: pydub not installed. Run: pip install pydub", file=sys.stderr)
        return 1
    
    storage = get_storage_path()
    try:
        filepath = _safe_join(storage, args.file)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not filepath.exists():
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        return 1
    
    ext = filepath.suffix.lower()
    if ext not in {".flac", ".mp3", ".wav"}:
        print(f"Error: Unsupported format: {ext}", file=sys.stderr)
        return 1
    
    try:
        audio = AudioSegment.from_file(str(filepath), format=ext[1:])
        duration = len(audio) / 1000.0
        
        start_time = args.start if args.start is not None else 0.0
        end_time = args.end if args.end is not None else duration
        
        if start_time < 0:
            print("Error: start time cannot be negative", file=sys.stderr)
            return 1
        if end_time <= start_time:
            print("Error: end time must be greater than start time", file=sys.stderr)
            return 1
        if start_time >= duration:
            print(f"Error: start time ({start_time}s) exceeds duration ({duration:.2f}s)", file=sys.stderr)
            return 1
        
        end_time = min(end_time, duration)
        
        is_segment = args.start is not None or args.end is not None
        if is_segment:
            start_ms = int(start_time * 1000)
            end_ms = int(end_time * 1000)
            segment = audio[start_ms:end_ms]
            segment_duration = len(segment) / 1000.0
            
            output_file = storage / f"_temp_segment_{filepath.stem}.wav"
            segment.export(str(output_file), format="wav")
            
            print(f"Loaded segment from: {args.file}")
            print(f"   Original duration: {duration:.2f}s")
            print(f"   Segment: {start_time:.2f}s to {end_time:.2f}s ({segment_duration:.2f}s)")
            print(f"   Output: {output_file.name}")
        else:
            print(f"Loaded: {args.file}")
            print(f"   Duration: {duration:.2f}s")
            print(f"   Format: {ext[1:].upper()}")
            print(f"   Size: {filepath.stat().st_size:,} bytes")
        
        return 0
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_mix(args: argparse.Namespace) -> int:
    """Mix two audio files together."""
    try:
        from pydub import AudioSegment
    except ImportError:
        print("Error: pydub not installed. Run: pip install pydub", file=sys.stderr)
        return 1
    
    storage = get_storage_path()
    storage.mkdir(parents=True, exist_ok=True)

    try:
        file1_path = _safe_join(storage, args.file1)
        file2_path = _safe_join(storage, args.file2)
        dest_path = _safe_join(storage, args.dest)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    if not file1_path.exists():
        print(f"Error: File not found: {file1_path}", file=sys.stderr)
        return 1
    if not file2_path.exists():
        print(f"Error: File not found: {file2_path}", file=sys.stderr)
        return 1
    
    for path in [file1_path, file2_path, dest_path]:
        ext = path.suffix.lower()
        if ext not in {".flac", ".mp3", ".wav"}:
            print(f"Error: Unsupported format: {ext} ({path.name})", file=sys.stderr)
            return 1
    
    mix_factor = args.factor
    if not 0.0 <= mix_factor <= 1.0:
        print(f"Error: mix factor must be between 0.0 and 1.0, got {mix_factor}", file=sys.stderr)
        return 1
    
    try:
        print(f"Loading: {args.file1}")
        audio1 = AudioSegment.from_file(str(file1_path), format=file1_path.suffix[1:].lower())
        print(f"Loading: {args.file2}")
        audio2 = AudioSegment.from_file(str(file2_path), format=file2_path.suffix[1:].lower())
        
        print(f"\nMixing with factor {mix_factor:.2f}")
        print("  (0.0 = 100% file1, 0.5 = equal, 1.0 = 100% file2)")
        
        if len(audio1) != len(audio2):
            max_len = max(len(audio1), len(audio2))
            if len(audio1) < max_len:
                audio1 += AudioSegment.silent(duration=max_len - len(audio1), frame_rate=audio1.frame_rate)
            if len(audio2) < max_len:
                audio2 += AudioSegment.silent(duration=max_len - len(audio2), frame_rate=audio2.frame_rate)
            print("  Padded shorter file to match duration")
        
        import math
        
        if mix_factor == 0.0:
            result = audio1
        elif mix_factor == 1.0:
            result = audio2
        else:
            vol1 = 1.0 - mix_factor
            vol2 = mix_factor
            db1 = 20 * math.log10(max(vol1, 0.001))
            db2 = 20 * math.log10(max(vol2, 0.001))
            adjusted1 = audio1 + db1
            adjusted2 = audio2 + db2
            result = adjusted1.overlay(adjusted2)
        
        dest_format = dest_path.suffix[1:].lower()
        result.export(str(dest_path), format=dest_format)
        
        result_duration = len(result) / 1000.0
        print(f"\nCreated: {dest_path.name}")
        print(f"   Duration: {result_duration:.2f}s")
        print(f"   Mix: {(1-mix_factor)*100:.0f}% {args.file1} + {mix_factor*100:.0f}% {args.file2}")
        return 0
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_volume(args: argparse.Namespace) -> int:
    """Adjust volume of an audio file."""
    try:
        from pydub import AudioSegment
    except ImportError:
        print("Error: pydub not installed. Run: pip install pydub", file=sys.stderr)
        return 1
    
    storage = get_storage_path()
    storage.mkdir(parents=True, exist_ok=True)

    try:
        source_path = _safe_join(storage, args.source)
        dest_path = _safe_join(storage, args.dest)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not source_path.exists():
        print(f"Error: File not found: {source_path}", file=sys.stderr)
        return 1

    for path in [source_path, dest_path]:
        ext = path.suffix.lower()
        if ext not in {".flac", ".mp3", ".wav"}:
            print(f"Error: Unsupported format: {ext} ({path.name})", file=sys.stderr)
            return 1
    
    gain_db = args.gain
    normalize = args.normalize
    
    if gain_db is None and not normalize:
        print("Error: Either --gain or --normalize must be specified", file=sys.stderr)
        return 1

    if gain_db is not None and not -60.0 <= gain_db <= 24.0:
        print(f"Error: gain should be between -60 and +24 dB, got {gain_db}", file=sys.stderr)
        return 1
    
    try:
        print(f"Loading: {args.source}")
        audio = AudioSegment.from_file(str(source_path), format=source_path.suffix[1:].lower())
        
        original_peak = audio.max_dBFS
        result = audio
        
        if gain_db is not None:
            print(f"Applying {gain_db:+.1f} dB gain")
            result = result + gain_db
        
        if normalize:
            peak = result.max_dBFS
            if peak < 0:
                normalize_gain = -peak
                print(f"Normalizing: {normalize_gain:+.1f} dB (peak was {peak:.1f} dB)")
                result = result + normalize_gain
            else:
                print("Already at or above 0 dB, no normalization needed")
        
        dest_format = dest_path.suffix[1:].lower()
        result.export(str(dest_path), format=dest_format)
        
        final_peak = result.max_dBFS
        duration = len(result) / 1000.0
        
        print(f"\nCreated: {dest_path.name}")
        print(f"   Duration: {duration:.2f}s")
        print(f"   Original peak: {original_peak:.1f} dB")
        print(f"   Final peak: {final_peak:.1f} dB")
        return 0
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_create(args: argparse.Namespace) -> int:
    """Create a silent audio file."""
    try:
        from pydub import AudioSegment
    except ImportError:
        print("Error: pydub not installed. Run: pip install pydub", file=sys.stderr)
        return 1
    
    storage = get_storage_path()
    storage.mkdir(parents=True, exist_ok=True)

    try:
        dest_path = _safe_join(storage, args.dest)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    ext = dest_path.suffix.lower()
    if ext not in {".flac", ".mp3", ".wav"}:
        print(f"Error: Unsupported format: {ext}", file=sys.stderr)
        return 1

    duration_ms = args.duration
    sample_rate = args.sample_rate
    channels = args.channels
    
    if duration_ms <= 0:
        print("Error: duration must be positive", file=sys.stderr)
        return 1
    
    if sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000, 96000):
        print(f"Error: Invalid sample rate: {sample_rate}. Use 8000, 16000, 22050, 24000, 44100, 48000, or 96000", file=sys.stderr)
        return 1
    
    if channels not in (1, 2):
        print("Error: channels must be 1 (mono) or 2 (stereo)", file=sys.stderr)
        return 1
    
    try:
        print(f"Creating {duration_ms}ms silent audio ({channels}ch @ {sample_rate}Hz)")
        
        silent = AudioSegment.silent(duration=duration_ms, frame_rate=sample_rate)

        # pydub's AudioSegment.silent() always returns mono; explicitly
        # upmix to stereo when the user asks for it (channels == 2).
        if channels == 2:
            silent = silent.set_channels(2)
        
        dest_format = ext[1:]
        silent.export(str(dest_path), format=dest_format)
        
        print(f"\nCreated: {dest_path.name}")
        print(f"   Duration: {duration_ms}ms ({duration_ms/1000:.2f}s)")
        print(f"   Sample rate: {sample_rate} Hz")
        print(f"   Channels: {channels} ({'mono' if channels == 1 else 'stereo'})")
        print(f"   Size: {dest_path.stat().st_size:,} bytes")
        return 0
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_detect_silence(args: argparse.Namespace) -> int:
    """Detect silent segments in audio file."""
    import subprocess
    import re
    import json
    
    storage = get_storage_path()
    try:
        filepath = _safe_join(storage, args.file)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not filepath.exists():
        print(f"Error: File not found: {filepath}", file=sys.stderr)
        return 1

    threshold_db = args.threshold
    min_duration = args.min_duration
    
    print(f"Detecting silence in: {filepath.name}")
    print(f"  Threshold: {threshold_db} dB")
    print(f"  Min duration: {min_duration}s")
    
    detect_cmd = [
        "ffmpeg", "-i", str(filepath),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}",
        "-f", "null", "-"
    ]
    
    result = subprocess.run(detect_cmd, capture_output=True, text=True)
    
    silence_starts = re.findall(r'silence_start: ([\d.]+)', result.stderr)
    silence_ends = re.findall(r'silence_end: ([\d.]+)', result.stderr)
    silence_durations = re.findall(r'silence_duration: ([\d.]+)', result.stderr)
    
    if not silence_starts:
        print("\nNo silence segments found.")
        return 0
    
    print(f"\nFound {len(silence_starts)} silence segments:\n")
    print(f"{'#':<4} {'Start':>10} {'End':>10} {'Duration':>10}")
    print("-" * 38)
    
    total_silence = 0.0
    for i, start in enumerate(silence_starts):
        start_sec = float(start)
        if i < len(silence_ends):
            end_sec = float(silence_ends[i])
            duration = float(silence_durations[i]) if i < len(silence_durations) else (end_sec - start_sec)
        else:
            probe_cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", str(filepath)
            ]
            probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
            if probe_result.returncode == 0:
                probe_data = json.loads(probe_result.stdout)
                end_sec = float(probe_data.get("format", {}).get("duration", start_sec))
                duration = end_sec - start_sec
            else:
                continue
        
        total_silence += duration
        print(f"{i+1:<4} {start_sec:>10.2f} {end_sec:>10.2f} {duration:>10.2f}")
    
    print("-" * 38)
    print(f"Total silence: {total_silence:.2f}s")
    
    if args.json:
        silences = []
        for i, start in enumerate(silence_starts):
            start_sec = float(start)
            if i < len(silence_ends):
                end_sec = float(silence_ends[i])
                duration = float(silence_durations[i]) if i < len(silence_durations) else (end_sec - start_sec)
                silences.append({"start": start_sec, "end": end_sec, "duration": duration})
        print("\nJSON:")
        print(json.dumps(silences, indent=2))
    
    return 0


async def cmd_compress_silence(args: argparse.Namespace) -> int:
    """Compress long silences in audio file."""
    import subprocess
    import re
    import json
    
    storage = get_storage_path()
    try:
        source_path = _safe_join(storage, args.source)
        dest_path = _safe_join(storage, args.dest)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not source_path.exists():
        print(f"Error: File not found: {source_path}", file=sys.stderr)
        return 1

    max_silence = args.max_silence
    threshold_db = args.threshold
    mp3_bitrate = args.bitrate
    
    print(f"Compressing silence in: {source_path.name}")
    print(f"  Max silence: {max_silence}s")
    print(f"  Threshold: {threshold_db} dB")
    
    # Step 1: Detect all silences (use low min duration)
    min_detect_duration = 0.3
    detect_cmd = [
        "ffmpeg", "-i", str(source_path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_detect_duration}",
        "-f", "null", "-"
    ]
    
    result = subprocess.run(detect_cmd, capture_output=True, text=True)
    
    silence_starts = re.findall(r'silence_start: ([\d.]+)', result.stderr)
    silence_ends = re.findall(r'silence_end: ([\d.]+)', result.stderr)
    
    if not silence_starts:
        print("\nNo silences found, copying file as-is...")
        copy_cmd = ["ffmpeg", "-y", "-i", str(source_path)]
        # Only use -c copy when containers match; otherwise transcode and
        # honour the user-supplied bitrate on the MP3 output (same flags
        # the main compression branch uses below), so the fast-path
        # doesn't silently drop --bitrate.
        if source_path.suffix.lower() == dest_path.suffix.lower():
            copy_cmd.extend(["-c", "copy"])
        elif dest_path.suffix.lower() == ".mp3":
            copy_cmd.extend(["-c:a", "libmp3lame", "-b:a", f"{mp3_bitrate}k"])
        copy_cmd.append(str(dest_path))
        subprocess.run(copy_cmd, capture_output=True, check=True)
        print(f"Copied to: {dest_path.name}")
        return 0
    
    # Get total duration
    probe_cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", str(source_path)
    ]
    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
    total_duration = 0.0
    if probe_result.returncode == 0:
        probe_data = json.loads(probe_result.stdout)
        total_duration = float(probe_data.get("format", {}).get("duration", 0))
    
    # Filter silences exceeding max_silence
    silences_to_compress = []
    for i, start in enumerate(silence_starts):
        start_sec = float(start)
        if i < len(silence_ends):
            end_sec = float(silence_ends[i])
        else:
            end_sec = total_duration
        
        duration = end_sec - start_sec
        if duration > max_silence:
            silences_to_compress.append((start_sec, end_sec, duration))
    
    if not silences_to_compress:
        print(f"\nNo silences exceed {max_silence}s, copying file as-is...")
        copy_cmd = ["ffmpeg", "-y", "-i", str(source_path)]
        # Only use -c copy when containers match; otherwise transcode and
        # honour the user-supplied bitrate on the MP3 output (same flags
        # the main compression branch uses below), so the fast-path
        # doesn't silently drop --bitrate.
        if source_path.suffix.lower() == dest_path.suffix.lower():
            copy_cmd.extend(["-c", "copy"])
        elif dest_path.suffix.lower() == ".mp3":
            copy_cmd.extend(["-c:a", "libmp3lame", "-b:a", f"{mp3_bitrate}k"])
        copy_cmd.append(str(dest_path))
        subprocess.run(copy_cmd, capture_output=True, check=True)
        print(f"Copied to: {dest_path.name}")
        return 0
    
    print(f"\nCompressing {len(silences_to_compress)} silence segments...")
    
    # Build segments to keep
    keep_duration = max_silence / 2.0
    segments = []
    current_pos = 0.0
    
    for start, end, _ in silences_to_compress:
        seg_end = start + keep_duration
        if seg_end > current_pos:
            segments.append((current_pos, seg_end))
        current_pos = end - keep_duration
    
    if current_pos < total_duration:
        segments.append((current_pos, total_duration))
    
    # Build ffmpeg filter
    filter_parts = []
    for i, (seg_start, seg_end) in enumerate(segments):
        if seg_end <= seg_start:
            continue
        filter_parts.append(
            f"[0:a]atrim=start={seg_start:.3f}:end={seg_end:.3f},asetpts=PTS-STARTPTS[s{i}]"
        )
    
    if not filter_parts:
        print("Error: No valid segments to extract", file=sys.stderr)
        return 1
    
    segment_labels = "".join(f"[s{i}]" for i in range(len(filter_parts)))
    filter_complex = ";".join(filter_parts) + f";{segment_labels}concat=n={len(filter_parts)}:v=0:a=1[out]"
    
    ffmpeg_cmd = [
        "ffmpeg", "-y", "-i", str(source_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
    ]
    
    ext = dest_path.suffix.lower()
    if ext == '.flac':
        ffmpeg_cmd.extend(["-c:a", "flac"])
    else:
        ffmpeg_cmd.extend(["-c:a", "libmp3lame", "-b:a", f"{mp3_bitrate}k"])
    
    ffmpeg_cmd.append(str(dest_path))
    
    result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Error: ffmpeg failed: {result.stderr[:500]}", file=sys.stderr)
        return 1
    
    # Get new duration
    probe_result = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", str(dest_path)
    ], capture_output=True, text=True)
    
    new_duration = 0.0
    if probe_result.returncode == 0:
        probe_data = json.loads(probe_result.stdout)
        new_duration = float(probe_data.get("format", {}).get("duration", 0))
    
    time_saved = total_duration - new_duration
    
    print(f"\nCompressed: {dest_path.name}")
    print(f"   Segments compressed: {len(silences_to_compress)}")
    print(f"   Time saved: {time_saved:.1f}s")
    print(f"   Duration: {total_duration:.1f}s -> {new_duration:.1f}s")
    
    return 0


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Audio Operations CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  audio-ops info song.mp3
  audio-ops cut input.wav output.wav -s 10 -e 30           # Extract 10s-30s
  audio-ops cut input.wav output.wav -s 10 -e 30 -m remove # Remove 10s-30s
  audio-ops merge output.mp3 part1.mp3 part2.mp3 part3.mp3
  audio-ops merge output.wav a.wav b.wav --crossfade 500   # With 500ms crossfade
  audio-ops mix track.wav bg.wav mixed.wav -f 0.7          # 30% track, 70% bg
  audio-ops volume input.wav louder.wav --gain 6           # +6 dB (louder)
  audio-ops volume input.wav normalized.wav --normalize    # Normalize to 0 dB
  audio-ops create silence.wav -d 5000                     # 5 second silence
  audio-ops detect-silence podcast.flac                    # Find silent segments
  audio-ops compress-silence input.flac output.mp3 -m 1.5  # Compress silences >1.5s
  audio-ops load song.mp3                                  # Load full file
  audio-ops load song.mp3 -s 10 -e 30                      # Load segment
  audio-ops list "*.wav"
  audio-ops -w /path/to/audio info song.mp3  # Use custom workdir
        """
    )
    
    # Global options
    parser.add_argument("-w", "--workdir", type=str, default=None,
                        help="Working directory for audio files (overrides config)")
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # info command
    info_parser = subparsers.add_parser("info", help="Show audio file metadata")
    info_parser.add_argument("file", help="Audio filename")
    
    # cut command
    cut_parser = subparsers.add_parser("cut", help="Extract or remove audio segment")
    cut_parser.add_argument("source", help="Source audio file")
    cut_parser.add_argument("dest", help="Destination file")
    cut_parser.add_argument("-s", "--start", type=float, required=True, help="Start time in seconds")
    cut_parser.add_argument("-e", "--end", type=float, required=True, help="End time in seconds")
    cut_parser.add_argument("-m", "--mode", choices=["extract", "remove"], default="extract",
                           help="'extract' copies segment (default), 'remove' deletes it")
    
    # merge command
    merge_parser = subparsers.add_parser("merge", help="Merge multiple audio files")
    merge_parser.add_argument("dest", help="Output filename")
    merge_parser.add_argument("sources", nargs="+", help="Source files to merge (in order)")
    merge_parser.add_argument("--crossfade", type=int, default=0, 
                             help="Crossfade duration in milliseconds (default: 0)")
    
    # mix command
    mix_parser = subparsers.add_parser("mix", help="Mix two audio files together")
    mix_parser.add_argument("file1", help="First audio file")
    mix_parser.add_argument("file2", help="Second audio file")
    mix_parser.add_argument("dest", help="Output filename")
    mix_parser.add_argument("-f", "--factor", type=float, default=0.5,
                           help="Mix factor 0.0-1.0 (0=100%% file1, 0.5=equal, 1=100%% file2). Default: 0.5")
    
    # volume command
    volume_parser = subparsers.add_parser("volume", help="Adjust audio volume")
    volume_parser.add_argument("source", help="Source audio file")
    volume_parser.add_argument("dest", help="Destination file")
    volume_parser.add_argument("-g", "--gain", type=float, help="Volume adjustment in dB (-60 to +24)")
    volume_parser.add_argument("-n", "--normalize", action="store_true",
                              help="Normalize to 0 dB peak")
    
    # create command
    create_parser = subparsers.add_parser("create", help="Create silent audio file")
    create_parser.add_argument("dest", help="Output filename")
    create_parser.add_argument("-d", "--duration", type=int, required=True,
                              help="Duration in milliseconds")
    create_parser.add_argument("-r", "--sample-rate", type=int, default=44100,
                              help="Sample rate in Hz (default: 44100)")
    create_parser.add_argument("-c", "--channels", type=int, default=2,
                              help="Number of channels: 1=mono, 2=stereo (default: 2)")
    
    # detect-silence command
    detect_parser = subparsers.add_parser("detect-silence", help="Detect silent segments in audio")
    detect_parser.add_argument("file", help="Audio file to analyze")
    detect_parser.add_argument("-t", "--threshold", type=int, default=-40,
                               help="Silence threshold in dB (default: -40)")
    detect_parser.add_argument("-d", "--min-duration", type=float, default=0.5,
                               help="Minimum silence duration in seconds (default: 0.5)")
    detect_parser.add_argument("--json", action="store_true",
                               help="Also output results as JSON")
    
    # compress-silence command
    compress_parser = subparsers.add_parser("compress-silence", help="Compress long silences")
    compress_parser.add_argument("source", help="Source audio file")
    compress_parser.add_argument("dest", help="Destination file")
    compress_parser.add_argument("-m", "--max-silence", type=float, default=1.0,
                                 help="Maximum silence duration in seconds (default: 1.0)")
    compress_parser.add_argument("-t", "--threshold", type=int, default=-40,
                                 help="Silence threshold in dB (default: -40)")
    compress_parser.add_argument("-b", "--bitrate", type=int, default=192,
                                 help="MP3 bitrate in kbps (default: 192)")
    
    # list command
    list_parser = subparsers.add_parser("list", help="List audio files")
    list_parser.add_argument("pattern", nargs="?", help="Glob pattern filter")
    
    # load command
    load_parser = subparsers.add_parser("load", help="Load audio file for analysis")
    load_parser.add_argument("file", help="Audio filename")
    load_parser.add_argument("-s", "--start", type=float, help="Start time in seconds (optional)")
    load_parser.add_argument("-e", "--end", type=float, help="End time in seconds (optional)")
    
    args = parser.parse_args()
    
    # Set workdir override if provided
    global _workdir_override
    if args.workdir:
        _workdir_override = Path(args.workdir)
        if not _workdir_override.exists():
            print(f"Error: Workdir does not exist: {_workdir_override}", file=sys.stderr)
            return 1
    
    if not args.command:
        parser.print_help()
        return 0
    
    commands = {
        "info": cmd_info,
        "cut": cmd_cut,
        "merge": cmd_merge,
        "mix": cmd_mix,
        "volume": cmd_volume,
        "create": cmd_create,
        "detect-silence": cmd_detect_silence,
        "compress-silence": cmd_compress_silence,
        "list": cmd_list,
        "load": cmd_load,
    }
    
    return asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    sys.exit(main())
