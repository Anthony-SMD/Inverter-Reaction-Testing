#!/usr/bin/env bash
#
# run.sh - run the reaction tester.
# All arguments are passed straight through to inverter_reaction_tester.py.
#
#   ./run.sh --monitor
#   ./run.sh --config default_config.json --trials 5
#
# Uses the project's ./.venv if present (created by install.sh, gives pymodbus).
# Otherwise falls back to the system python3 -- the tool has a built-in Modbus
# client, so it runs with no venv and no installed packages.
#
set -euo pipefail
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PY="$(command -v python)"
else
  echo "No Python found. Install python3 (e.g. sudo apt install python3)." >&2
  exit 1
fi

# exec so Ctrl+C / signals go straight to Python
exec "$PY" inverter_reaction_tester.py "$@"
