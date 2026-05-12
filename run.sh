#!/usr/bin/env bash
# Run AskMyCFO Simple Template Filler.
# Usage:  ./run.sh        (creates venv on first run, then starts the server)
set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "[setup] creating virtualenv ..."
  python3 -m venv .venv
fi

source .venv/bin/activate

if [ ! -f ".venv/.deps_installed" ]; then
  echo "[setup] installing requirements ..."
  pip install --upgrade pip wheel >/dev/null
  pip install -r requirements.txt
  touch .venv/.deps_installed
fi

echo
echo "─────────────────────────────────────────────"
echo " AskMyCFO Simple Template Filler"
echo "   Upload : http://127.0.0.1:5005/"
echo "   Rules  : http://127.0.0.1:5005/rules"
echo "─────────────────────────────────────────────"
echo

exec python3 app.py
