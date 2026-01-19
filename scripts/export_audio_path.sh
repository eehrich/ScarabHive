#!/bin/bash
# Export audio path with standard production settings
# Usage: ./export_audio_path.sh <path_id> <audio_version_id> <output_file>

set -e  # Exit on error

# Check arguments
if [ $# -ne 3 ]; then
    echo "Usage: $0 <path_id> <audio_version_id> <output_file>"
    echo ""
    echo "Example: $0 12 4 path12_4.mp3"
    echo ""
    echo "Settings:"
    echo "  - Normalize volume: clip-level (EBU R128, -16 LUFS)"
    echo "  - Pause between scenes: 2s"
    echo "  - Compress silence: max 2s"
    echo "  - Force: include unreviewed clips, irgnore missing clips"
    exit 1
fi

PATH_ID=$1
AUDIO_VERSION_ID=$2
OUTPUT_FILE=$3

echo "Exporting audio path..."
echo "  Path ID: $PATH_ID"
echo "  Audio Version ID: $AUDIO_VERSION_ID"
echo "  Output: $OUTPUT_FILE"
echo ""

writer-audio export-path \
    --audio-version-id "$AUDIO_VERSION_ID" \
    --path-id "$PATH_ID" \
    -o "$OUTPUT_FILE" \
    --compress-silence \
    --max-silence 2 \
    --normalize clip \
    --pause 2 \
    --force

echo ""
echo "Export complete: $OUTPUT_FILE"
