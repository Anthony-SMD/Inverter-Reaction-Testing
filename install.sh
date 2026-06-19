#!/usr/bin/env bash
#
# install.sh - set up inverter_reaction_tester.py on Linux (also works on macOS).
#
# Creates a self-contained Python virtual environment in ./.venv and installs the
# dependencies into it. A venv is used because modern Debian/Ubuntu/Fedora block
# system-wide "pip install" (PEP 668 "externally-managed-environment").
#
# Usage:
#   ./install.sh
#
set -euo pipefail
cd "$(dirname "$0")"

# --- locate python3 ---------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install it first, for example:" >&2
  echo "  Debian/Ubuntu : sudo apt install python3 python3-venv python3-pip" >&2
  echo "  Fedora/RHEL   : sudo dnf install python3 python3-pip" >&2
  echo "  Arch          : sudo pacman -S python" >&2
  exit 1
fi
echo "Using $(python3 --version) at $(command -v python3)"

# --- create the virtual environment ----------------------------------------
# Fresh Debian/Ubuntu/Raspberry Pi OS often lacks ensurepip (it ships in the
# separate python3-venv package), so a normal "python3 -m venv" fails with
# "ensurepip is not available" and a half-made venv has no pip ("No module named
# pip"). We create the venv WITHOUT pip (always works) and bootstrap pip below.
if [ ! -d .venv ]; then
  echo "Creating virtual environment in ./.venv ..."
  if ! python3 -m venv .venv 2>/dev/null && ! python3 -m venv --without-pip --clear .venv; then
    echo "ERROR: python3 cannot create a venv (the venv module is missing)." >&2
    echo "  sudo apt install python3-venv     # Debian/Ubuntu/Raspberry Pi OS" >&2
    exit 1
  fi
fi

VENV_PY="$PWD/.venv/bin/python"

# get-pip.py has version-specific URLs for old Pythons; the generic one refuses to
# run on < 3.8 ("minimum supported Python version is 3.10"). Pick the right URL.
PYVER="$("$VENV_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
  2.*|3.[0-7]) GETPIP_URL="https://bootstrap.pypa.io/pip/$PYVER/get-pip.py" ;;
  *)           GETPIP_URL="https://bootstrap.pypa.io/get-pip.py" ;;
esac

# --- make sure pip exists inside the venv ----------------------------------
if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  echo "pip is not in the venv yet; bootstrapping it (Python $PYVER) ..."
  if "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1; then
    :                                            # ensurepip worked (offline)
  elif command -v curl >/dev/null 2>&1; then
    echo "  fetching $GETPIP_URL ..."
    curl -fsSL "$GETPIP_URL" | "$VENV_PY" -
  elif command -v wget >/dev/null 2>&1; then
    echo "  fetching $GETPIP_URL ..."
    wget -qO- "$GETPIP_URL" | "$VENV_PY" -
  else
    echo "ERROR: could not bootstrap pip (no ensurepip, and no curl/wget to fetch it)." >&2
    echo "Install the system packages, then re-run:" >&2
    echo "  sudo apt install python3-venv python3-pip   # Debian/Ubuntu/Raspberry Pi OS" >&2
    echo "  rm -rf .venv && bash install.sh" >&2
    exit 1
  fi
fi

# --- install dependencies into the venv ------------------------------------
echo "Installing dependencies ..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1 || true   # best-effort
"$VENV_PY" -m pip install -r requirements.txt

# --- make the scripts executable -------------------------------------------
chmod +x inverter_reaction_tester.py run.sh 2>/dev/null || true

cat <<'EOF'

Done.

Run with the wrapper (uses ./.venv automatically):
  ./run.sh --monitor                       # check the meter is being received
  ./run.sh --config default_config.json    # run the reaction test

Or activate the venv yourself:
  source .venv/bin/activate
  python inverter_reaction_tester.py --help

If the meter shows nothing, see the "Linux" section of README.md (firewall + interface).
EOF
