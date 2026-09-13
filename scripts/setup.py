"""One-command Windows setup for Tora AI.

Usage from the extracted project folder:
    python scripts/setup.py
Use --skip-install when Python packages are already installed in .venv.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
TOKEN_PATH = ROOT / ".local-token"
ENV_EXAMPLE = ROOT / ".env.example"
ENV_PATH = ROOT / ".env"


def run(command: list[str], *, check: bool = True) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=check)


def ensure_token() -> str:
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
        TOKEN_PATH.unlink()
    token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up a private Tora AI Windows install")
    parser.add_argument("--skip-install", action="store_true", help="Do not install requirements into .venv")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        print("Tora requires Python 3.11 or newer. Download it from python.org.", file=sys.stderr)
        return 2
    if not VENV.exists():
        print(f"Creating {VENV.name}...")
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(VENV)
    vpy = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not vpy.is_file():
        print(f"Virtual environment Python was not found at {vpy}.", file=sys.stderr)
        return 1
    if not args.skip_install:
        run([str(vpy), "-m", "pip", "install", "--upgrade", "pip"])
        run([str(vpy), "-m", "pip", "install", "-r", "requirements.txt"])
        run([str(vpy), "-m", "playwright", "install", "chromium"])
    if not ENV_PATH.exists():
        shutil.copyfile(ENV_EXAMPLE, ENV_PATH)
        print("Created .env from .env.example. Add BOT_TOKEN before starting.")
    token = ensure_token()
    print(f"Created/kept private extension pairing key: {TOKEN_PATH.name} ({len(token)} characters)")
    print()
    print("Next steps:")
    print("1. Open .env and paste your Telegram BOT_TOKEN.")
    print("2. Run the agent once, send /start to your bot, set ALLOWED_CHAT_ID in .env, and restart.")
    print("3. Pull a local model: ollama pull qwen3-coder:30b")
    print("4. Load edge_extension/ in edge://extensions with Developer mode enabled.")
    print(f"5. Open the extension's Options page and paste the contents of {TOKEN_PATH.name}.")
    print(f"6. Start Tora: {vpy} main.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
