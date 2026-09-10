#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3.11+ is required."
  exit 1
fi

"$PY" install.py
