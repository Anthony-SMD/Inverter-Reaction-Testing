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
if [ ! -d .venv ]; then
  echo "Creating virtual environment in ./.venv ..."
  if ! python3 -m venv .venv; then
    echo "ERROR: could not create the venv." >&2
    echo "On Debian/Ubuntu install the venv package: sudo apt install python3-venv" >&2
    exit 1
  fi
fi

# --- install dependencies into the venv ------------------------------------
echo "Installing dependencies ..."
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

# --- make the scripts executable -------------------------------------------
chmod +x inverter_reaction_tester.py run.sh 2>/dev/null || true

cat <<'EOF'

Done.

Run with the wrapper (uses ./.venv automatically):
  ./run.sh --monitor --meter-iface <your-LAN-IP>          # check meter reception
  ./run.sh --config my_setup.json                         # run a reaction test

Or activate the venv yourself:
  source .venv/bin/activate
  python inverter_reaction_tester.py --help

If the meter shows nothing, see the "Linux" section of README.md (firewall + interface).
EOF
