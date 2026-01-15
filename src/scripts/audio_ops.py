#!/usr/bin/env python
"""Audio Operations CLI - Command-line interface for audio manipulation.

Usage:
    audio-ops info <file>                              - Show audio file metadata
    audio-ops cut <src> <dest> -s <sec> -e <sec>       - Extract segment (default)
    audio-ops cut <src> <dest> -s <sec> -e <sec> -m remove  - Remove segment
    audio-ops merge <dest> <file1> <file2> [...]       - Merge files
    audio-ops list [pattern]                           - List audio files
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


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
    """Get storage path from config."""
    config = get_config()
    return Path(config.get("storage_path", "data/audio_ops"))


async def cmd_info(args: argparse.Namespace) -> int:
    """Show audio file info."""
    try:
        from pydub import AudioSegment
    except ImportError:
        print("Error: pydub not installed. Run: pip install pydub", file=sys.stderr)
        return 1
    
    storage = get_storage_path()
    filepath = storage / args.file
    
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
    
    source_path = storage / args.source
    dest_path = storage / args.dest
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
        print(f"✅ Created: {dest_path.name}")
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
    
    dest_path = storage / args.dest
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
            filepath = storage / filename
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
        print(f"\n✅ Created: {dest_path.name}")
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
        # Use given pattern (should include extension, e.g., "*.wav")
        files.extend(storage.glob(pattern))
    else:
        # List all supported audio files
        for ext in [".flac", ".mp3", ".wav"]:
            files.extend(storage.glob(f"*{ext}"))
    
    # Filter to supported formats and deduplicate
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
    filepath = storage / args.file
    
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
        
        # Validate
        if start_time < 0:
            print("Error: start time cannot be negative", file=sys.stderr)
            return 1
        if end_time <= start_time:
            print("Error: end time must be greater than start time", file=sys.stderr)
            return 1
        if start_time >= duration:
            print(f"Error: start time ({start_time}s) exceeds duration ({duration:.2f}s)", file=sys.stderr)
            return 1
        
        # Clamp end
        end_time = min(end_time, duration)
        
        # Extract segment if not full file
        is_segment = args.start is not None or args.end is not None
        if is_segment:
            start_ms = int(start_time * 1000)
            end_ms = int(end_time * 1000)
            segment = audio[start_ms:end_ms]
            segment_duration = len(segment) / 1000.0
            
            # Save to temp file
            output_file = storage / f"_temp_segment_{filepath.stem}.wav"
            segment.export(str(output_file), format="wav")
            
            print(f"✅ Loaded segment from: {args.file}")
            print(f"   Original duration: {duration:.2f}s")
            print(f"   Segment: {start_time:.2f}s to {end_time:.2f}s ({segment_duration:.2f}s)")
            print(f"   Output: {output_file.name}")
        else:
            print(f"✅ Loaded: {args.file}")
            print(f"   Duration: {duration:.2f}s")
            print(f"   Format: {ext[1:].upper()}")
            print(f"   Size: {filepath.stat().st_size:,} bytes")
        
        return 0
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


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
  audio-ops load song.mp3                                  # Load full file
  audio-ops load song.mp3 -s 10 -e 30                      # Load segment
  audio-ops list "*.wav"
        """
    )
    
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
    
    # list command
    list_parser = subparsers.add_parser("list", help="List audio files")
    list_parser.add_argument("pattern", nargs="?", help="Glob pattern filter")
    
    # load command
    load_parser = subparsers.add_parser("load", help="Load audio file for analysis")
    load_parser.add_argument("file", help="Audio filename")
    load_parser.add_argument("-s", "--start", type=float, help="Start time in seconds (optional)")
    load_parser.add_argument("-e", "--end", type=float, help="End time in seconds (optional)")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 0
    
    commands = {
        "info": cmd_info,
        "cut": cmd_cut,
        "merge": cmd_merge,
        "list": cmd_list,
        "load": cmd_load,
    }
    
    return asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    sys.exit(main())
