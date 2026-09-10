#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "Virtual environment not found. Running installer first..."
  ./install.sh
fi

exec ".venv/bin/python" launcher.py
