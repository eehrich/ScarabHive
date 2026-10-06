#!/usr/bin/env sh
# ScarabHive: install into .venv, give this installation its own signing key and its admin a password of its own,
# start the API, open the browser.
#   ./install.sh  or  sh install.sh   (Linux, macOS, Git Bash on Windows; Windows PowerShell: install.ps1)
# Running it again installs what a git pull added and starts the API (stop a running one first).
# SVG layers in images need the cairo library, a C compiler and Python's headers: where they are
# missing the script installs them with Homebrew (macOS: cairo) or apt-get (Debian/Ubuntu, through
# sudo), and goes on without SVG layers where it cannot. SCARABHIVE_NO_SYSTEM_PACKAGES=1 leaves the system's packages alone and only says what
# is missing.
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

# requirements/optional.txt: what may fail without failing the install. Today that is reportlab's
# cairo backend, which draws SVG layers in images; pycairo has wheels for Windows only and builds
# everywhere else, against cairo found through pkg-config.
KERNEL=$(uname -s)  # not OS: Git Bash exports OS=Windows_NT to everything this script starts
PYVER=$("$BIN/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || PYVER=3
if [ "$KERNEL" = Darwin ] && ! command -v brew >/dev/null 2>&1; then
  # Homebrew installed but not on PATH (Apple Silicon's /opt/homebrew until the profile says so);
  # its shellenv also puts its pkg-config on PATH for the pycairo build. The variable is for tests.
  for b in ${_SCARABHIVE_BREW_PROBE:-/opt/homebrew/bin/brew /usr/local/bin/brew}; do
    if [ -x "$b" ]; then eval "$("$b" shellenv)"; break; fi
  done
fi
# The backend may be there already (an earlier run, a cairo since removed): then SVG layers work.
has_svg() { "$BIN/python" -c 'import rlPyCairo' >/dev/null 2>&1; }
# What pycairo builds with: cairo found through pkg-config, a C compiler and the venv Python's headers.
has_cairo() { command -v pkg-config >/dev/null 2>&1 && pkg-config --exists cairo; }
has_compiler() {
  # macOS has /usr/bin/cc even without the Command Line Tools: a stand-in that offers to install them.
  if [ "$KERNEL" = Darwin ]; then xcode-select -p >/dev/null 2>&1
  else command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 || command -v clang >/dev/null 2>&1; fi
}
has_headers() {
  "$BIN/python" -c 'import os, sys, sysconfig
sys.exit(not os.path.exists(os.path.join(sysconfig.get_paths()["include"], "Python.h")))' >/dev/null 2>&1
}
# pythonX.Y-dev only where Python.h is missing: a pyenv or uv Python has its headers and no such
# package, and a name apt does not know fails the whole apt call.
APT_PACKAGES="build-essential libcairo2-dev pkg-config"
has_headers || APT_PACKAGES="$APT_PACKAGES python$PYVER-dev"
if [ "$KERNEL" = Darwin ]; then
  CAIRO_CMD="brew install cairo pkg-config"
elif command -v apt-get >/dev/null 2>&1; then
  CAIRO_CMD="sudo apt-get update && sudo apt-get install -y $APT_PACKAGES"
elif command -v dnf >/dev/null 2>&1; then
  # python3-devel, not python3.X-devel: Fedora names only its other Pythons' headers by version.
  CAIRO_CMD="sudo dnf install -y cairo-devel pkgconf-pkg-config gcc python3-devel"
elif command -v pacman >/dev/null 2>&1; then
  CAIRO_CMD="sudo pacman -S --needed cairo pkgconf gcc"
else
  CAIRO_CMD="install cairo's development files, pkg-config, a C compiler and Python's headers"
fi
if [ "$BIN" = .venv/Scripts ]; then
  SVG_FIX="run ./install.sh again"  # a Windows Python: pycairo comes as a wheel
else
  SVG_FIX="$CAIRO_CMD, then run ./install.sh again"
  if [ "$KERNEL" = Darwin ] && ! command -v brew >/dev/null 2>&1; then
    SVG_FIX="install Homebrew (https://brew.sh), then $SVG_FIX"
  fi
  if [ "$KERNEL" = Darwin ] && ! has_compiler; then
    SVG_FIX="xcode-select --install (Apple's Command Line Tools), then $SVG_FIX"
  fi
fi
# root needs no sudo; without a terminal sudo must not wait for a password nobody can type.
as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; elif [ -t 0 ]; then sudo "$@"; else sudo -n "$@"; fi
}
BUILDABLE=""  # pip may install requirements/optional.txt
if [ "$BIN" = .venv/Scripts ] || has_svg || { has_cairo && has_compiler && has_headers; }; then
  BUILDABLE=1
elif [ "${SCARABHIVE_NO_SYSTEM_PACKAGES:-0}" != 0 ]; then
  :  # the system's packages are left alone; the note below says what is missing
elif [ "$KERNEL" = Darwin ] && ! has_compiler; then
  :  # the Command Line Tools open a dialog of their own; the note below names them
elif [ "$KERNEL" = Darwin ] && command -v brew >/dev/null 2>&1; then
  echo "Installing cairo and pkg-config with Homebrew (needed to draw SVG layers in images) ..."
  if brew install cairo pkg-config; then BUILDABLE=1; fi
elif [ "$KERNEL" != Darwin ] && command -v apt-get >/dev/null 2>&1; then
  echo "Installing $APT_PACKAGES with apt-get (needed to draw SVG layers in images;" \
       "SCARABHIVE_NO_SYSTEM_PACKAGES=1 skips this) ..."
  # shellcheck disable=SC2086 -- one word per package
  if as_root apt-get update && as_root apt-get install -y $APT_PACKAGES; then BUILDABLE=1; fi
fi
if [ -n "$BUILDABLE" ]; then
  "$BIN/python" -m pip install -r requirements/optional.txt || true  # its output stays visible
fi
SVG_OFF=""
if ! has_svg; then
  SVG_OFF=1
  echo "Going on without SVG layers in images (requirements/optional.txt, built against the cairo" \
       "library). To add them: $SVG_FIX" >&2
fi

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
if [ -n "$SVG_OFF" ]; then echo "SVG layers in images are off: $SVG_FIX"; fi
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
