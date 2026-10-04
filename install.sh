#!/usr/bin/env sh
# ScarabHive: install into .venv, give this installation its own signing key and its admin a password of its own,
# start the API, open the browser.
#   sh install.sh        (Linux, macOS, Git Bash on Windows; Windows PowerShell: install.ps1)
# Running it again installs what a git pull added and starts the API (stop a running one first).
# INSTALLATION.md has the steps by hand.
set -e
cd "$(dirname "$0")"

# "py -3": Windows with python.org's installer but no PATH entry, where python/python3 are the Store's stand-ins.
# python3.1x: a distribution whose python3 is older than the one installed beside it.
PY=""
for candidate in python3 python python3.13 python3.12 python3.11 "py -3"; do
  if command -v ${candidate%% *} >/dev/null 2>&1 &&
     $candidate -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    PY=$candidate
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer is needed (python3 --version)." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  $PY -m venv .venv || {
    echo "Could not create the virtual environment. Debian/Ubuntu: sudo apt-get install python3-venv" >&2
    rm -rf .venv
    exit 1
  }
fi
if [ -x .venv/bin/python ]; then BIN=.venv/bin; elif [ -x .venv/Scripts/python.exe ]; then BIN=.venv/Scripts; else
  echo ".venv holds no Python: remove the folder and run this again." >&2
  exit 1
fi

"$BIN/python" -m pip install -U pip
if [ "$(uname -s)" = Linux ] && ! command -v nvidia-smi >/dev/null 2>&1; then
  # No NVIDIA GPU: the CPU build of PyTorch, not the CUDA build pip would take (several GB more).
  "$BIN/python" -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
fi
"$BIN/python" -m pip install -e .

"$BIN/python" -m agent_system.config.local_layer signing-key ||
  echo "Going on: the Setup panel shows the signing key." >&2
# Not past a failed or aborted step: an admin of an older install may still open with admin123.
"$BIN/python" -m agent_system.auth.first_admin || {
  echo "The admin password was not set (see above), so the API is not started. Run this script again." >&2
  exit 1
}

URL="http://127.0.0.1:${PORT:-8000}"
echo
if [ "$(uname -s)" = Linux ] && [ -z "$DISPLAY$WAYLAND_DISPLAY" ]; then
  OPEN=""  # no screen: a text browser would open in this terminal, over the API's output
  echo "Starting ScarabHive at $URL -- open it in a browser."
else
  OPEN=1
  echo "Starting ScarabHive at $URL -- the browser opens when it answers."
fi
echo "Log in with the admin account. Then open the Setup panel (grid icon, type 'setup')"
echo "and enter your OpenRouter key. Stop the API with Ctrl+C;"
echo "start it again with: $BIN/agent-api"
echo
[ -n "$OPEN" ] && "$BIN/python" - "$URL" <<'EOF' &
import sys, time, urllib.error, urllib.request, webbrowser
for _ in range(180):
    try:
        urllib.request.urlopen(sys.argv[1], timeout=2)
        break
    except urllib.error.HTTPError:
        break  # it answers, if only with a login
    except OSError:
        time.sleep(1)
else:
    sys.exit(0)
webbrowser.open(sys.argv[1])
EOF
exec "$BIN/agent-api"
