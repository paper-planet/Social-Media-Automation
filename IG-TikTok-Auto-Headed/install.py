#!/usr/bin/env python3
"""Cross-platform installer for Social Control Suite."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def run(cmd):
    print("+", " ".join(str(x) for x in cmd))
    subprocess.check_call([str(x) for x in cmd], cwd=str(ROOT))


def venv_python():
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def main():
    print("Social Control Suite installer")
    print("==============================")

    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ is required.")

    if not VENV.exists():
        run([sys.executable, "-m", "venv", str(VENV)])

    py = venv_python()
    run([py, "-m", "pip", "install", "--upgrade", "pip", "wheel"])
    run([py, "-m", "pip", "install", "-r", "requirements.txt"])
    run([py, "-m", "playwright", "install", "chromium"])

    print("\nInstall complete.")
    print("Optional but recommended:")
    print("  - Install ffmpeg and ensure it is on PATH.")
    print("  - Install/start Ollama.")
    print("  - Pull a text model, e.g. llama3.1.")
    print("  - Pull a vision model so videos can actually be analyzed frame-by-frame.")
    print("\nStart with:")
    if os.name == "nt":
        print("  start_windows.bat")
    else:
        print("  ./start.sh")


if __name__ == "__main__":
    main()
