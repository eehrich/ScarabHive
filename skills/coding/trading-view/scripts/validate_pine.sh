#!/usr/bin/env bash
# Local Pine Script v6 validator (bundled pine-script-validator).
#
# Static syntax + semantic checks for .pine files, fully offline. Useful as a
# fast pre-flight before the user pastes code into the TradingView editor.
#
# Usage:
#   validate_pine.sh <file|dir|glob> [options]
#   validate_pine.sh /path/to/script.pine --no-hints --no-information
#   validate_pine.sh /path/to/scripts/ --json
#
# Options (same as the upstream CLI): --json, --agent-json, --sarif,
# --errors/--no-errors, --warnings/--no-warnings,
# --information/--no-information, --hints/--no-hints.
#
# Exit code: 0 = no error-level diagnostics, 1 = at least one error.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_SRC="$SCRIPT_DIR/pine-validator/src"
export PYTHONPATH="$PKG_SRC${PYTHONPATH:+:$PYTHONPATH}"

# The bundled validator needs Python >= 3.11 (dataclass slots / match).
PY=""
for cand in python3 python py; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver="$("$cand" -c 'import sys; print("%d%02d" % (sys.version_info[0], sys.version_info[1]))' 2>/dev/null || true)"
        if [ -n "$ver" ] && [ "$ver" -ge 311 ]; then
            PY="$cand"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "error: pine-validator needs Python >= 3.11 (none of python3/python/py qualifies)" >&2
    exit 2
fi
exec "$PY" -m pinescript_validator.cli "$@"
