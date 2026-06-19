#!/usr/bin/env bash
#
# run.sh - run the reaction tester using the project's virtual environment.
# All arguments are passed straight through to inverter_reaction_tester.py.
#
#   ./run.sh --monitor --meter-iface 192.168.1.50
#   ./run.sh --config my_setup.json --trials 5
#
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Virtual environment not found. Run ./install.sh first." >&2
  exit 1
fi

# exec so Ctrl+C / signals go straight to Python
exec ./.venv/bin/python inverter_reaction_tester.py "$@"
