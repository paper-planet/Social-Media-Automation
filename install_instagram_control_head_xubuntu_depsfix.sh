#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-$HOME/Documents/insta/ig-bot-1.1}"
cd "$PROJECT_DIR"

echo "==> Installing Xubuntu system dependencies"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip python3-tk ffmpeg curl ca-certificates snapd

if ! command -v chromium >/dev/null 2>&1 && [ ! -x /snap/bin/chromium ]; then
  echo "==> Installing Chromium Snap"
  sudo snap install chromium
fi

if [ ! -d venv ]; then
  echo "==> Creating venv"
  python3 -m venv venv
fi

PY="$PROJECT_DIR/venv/bin/python"

echo "==> Updating pip tooling"
"$PY" -m pip install -U pip setuptools wheel packaging

echo "==> Installing Instagram Control Head Python dependencies"
# instagrapi 2.18.19 requires Pillow >=12.2,<13. MoviePy 2.2.1 still
# declares Pillow<12, so do NOT ask pip to resolve them in the same transaction.
"$PY" -m pip install -U \
  'Pillow>=12.2.0,<13' \
  'instagrapi==2.18.19' \
  pyotp ollama playwright \
  imageio imageio-ffmpeg 'numpy>=1.25' 'proglog<=1.0.0' \
  'python-dotenv>=0.10' decorator yt-dlp faster-whisper

# Official instagrapi guidance for the current Pillow conflict: install MoviePy
# without dependency resolution and keep the newer Pillow required by instagrapi.
echo "==> Installing MoviePy 2.2.1 without its stale Pillow<12 dependency pin"
"$PY" -m pip install -U --no-deps 'moviepy==2.2.1'

# We use system/Snap Chromium on Xubuntu. Install Playwright's Linux shared
# libraries, but intentionally do NOT download Playwright's bundled Chromium,
# because that executable was SIGTRAP/crashing on this machine.
echo "==> Installing Playwright Linux runtime libraries"
sudo "$PY" -m playwright install-deps chromium || true

if ! command -v ollama >/dev/null 2>&1; then
  echo "==> Installing Ollama from the official Linux installer"
  curl -fsSL https://ollama.com/install.sh | sh
fi

if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl enable --now ollama 2>/dev/null || true
fi

if command -v ollama >/dev/null 2>&1; then
  echo "==> Ensuring default Ollama models"
  ollama pull llama3.1 || true
  ollama pull gemma3:4b || true
fi

echo
echo "==> Verification"
"$PY" - <<'PY'
from importlib.metadata import version
checks = ["instagrapi", "Pillow", "moviepy", "playwright", "faster-whisper"]
for name in checks:
    try:
        print(f"{name}: {version(name)}")
    except Exception as exc:
        print(f"{name}: MISSING ({exc})")
PY

echo
echo "Dependency installation complete."
echo "Run:"
echo "  source '$PROJECT_DIR/venv/bin/activate'"
echo "  python '$PROJECT_DIR/instagram_control_head_reels_xubuntu_autosetup_depsfix.py'"
