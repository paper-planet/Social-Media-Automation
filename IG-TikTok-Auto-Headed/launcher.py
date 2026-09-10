#!/usr/bin/env python3
"""
Social Control Suite launcher.

Private configuration is stored under ~/.social-control-suite by default,
not in the Git repository.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUITE_STATE = Path(
    os.environ.get(
        "SOCIAL_CONTROL_STATE_ROOT",
        str(Path.home() / ".social-control-suite"),
    )
).expanduser()
CONFIG_FILE = SUITE_STATE / "config.json"
IG_ROOT = SUITE_STATE / "instagram"
TT_ROOT = SUITE_STATE / "tiktok"
IG_ACCOUNTS_FILE = IG_ROOT / "accounts.json"

DEFAULT_CONFIG = {
    "media_root": str(Path.home() / "social-media-pool"),
    "instagram_port": 8081,
    "tiktok_port": 8080,
    "ollama_text_model": "llama3.1",
    "ollama_vision_model": "",
}


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default):
    try:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, type(default)) else default
    except Exception:
        pass
    return default.copy() if isinstance(default, dict) else list(default)


def config():
    value = load_json(CONFIG_FILE, DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(value)
    return merged


def prompt(label, default=""):
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def parse_accounts(raw: str):
    out = []
    for item in raw.replace("\n", ",").split(","):
        name = item.strip().lstrip("@")
        if name and name not in out:
            out.append(name)
    return out


def setup():
    SUITE_STATE.mkdir(parents=True, exist_ok=True)
    current = config()

    print("\nSocial Control Suite setup")
    print("--------------------------")
    print(f"Private state directory: {SUITE_STATE}")
    print("No passwords are stored by this setup wizard.\n")

    media_root = prompt("Media pool folder", current["media_root"])
    ig_port = int(prompt("Instagram Control Head port", str(current["instagram_port"])))
    tt_port = int(prompt("TikTok Control Head port", str(current["tiktok_port"])))
    text_model = prompt("Ollama text model", current["ollama_text_model"])
    vision_model = prompt(
        "Ollama vision model (blank allowed)",
        current.get("ollama_vision_model", ""),
    )

    existing_ig = load_json(IG_ACCOUNTS_FILE, {"accounts": []}).get("accounts", [])
    existing_ig_names = []
    for item in existing_ig:
        if isinstance(item, str):
            existing_ig_names.append(item)
        elif isinstance(item, dict) and item.get("username"):
            existing_ig_names.append(str(item["username"]))

    ig_raw = prompt(
        "Instagram usernames, comma-separated",
        ",".join(existing_ig_names),
    )
    ig_accounts = parse_accounts(ig_raw)

    tt_accounts_file = TT_ROOT / "data" / "accounts.json"
    existing_tt = load_json(tt_accounts_file, [])
    existing_tt_names = [
        str(x.get("username", "")).lstrip("@")
        for x in existing_tt
        if isinstance(x, dict) and x.get("username")
    ]
    tt_raw = prompt(
        "TikTok usernames, comma-separated",
        ",".join(existing_tt_names),
    )
    tt_accounts = parse_accounts(tt_raw)

    cfg = {
        "media_root": str(Path(media_root).expanduser()),
        "instagram_port": ig_port,
        "tiktok_port": tt_port,
        "ollama_text_model": text_model,
        "ollama_vision_model": vision_model,
    }
    save_json(CONFIG_FILE, cfg)

    save_json(
        IG_ACCOUNTS_FILE,
        {
            "accounts": [
                {
                    "username": name,
                    "enabled": True,
                    "target_hashtags": [],
                    "target_accounts": [],
                }
                for name in ig_accounts
            ]
        },
    )

    save_json(
        tt_accounts_file,
        [
            {
                "id": f"tt_{i}",
                "username": "@" + name,
                "enabled": True,
            }
            for i, name in enumerate(tt_accounts, start=1)
        ],
    )

    Path(cfg["media_root"]).mkdir(parents=True, exist_ok=True)

    print("\nSaved.")
    print(f"Suite config:       {CONFIG_FILE}")
    print(f"Instagram accounts: {IG_ACCOUNTS_FILE}")
    print(f"TikTok accounts:    {tt_accounts_file}")
    print("These files are outside the repository by default.\n")


def env_for(service: str):
    cfg = config()
    env = os.environ.copy()
    env["IG_MEDIA_ROOT"] = cfg["media_root"]
    env["TIKTOK_MEDIA_ROOT"] = cfg["media_root"]

    text_model = str(cfg.get("ollama_text_model", "")).strip()
    vision_model = str(cfg.get("ollama_vision_model", "")).strip()

    if text_model:
        env["IG_OLLAMA_MODEL"] = text_model
        env["TIKTOK_OLLAMA_MODEL"] = text_model
    if vision_model:
        env["IG_OLLAMA_VISION_MODEL"] = vision_model
        env["TIKTOK_OLLAMA_VISION_MODEL"] = vision_model

    if service == "instagram":
        env["IG_DATA_ROOT"] = str(IG_ROOT / "data")
        env["IG_ACCOUNTS_FILE"] = str(IG_ACCOUNTS_FILE)
        env["IG_PORT"] = str(cfg["instagram_port"])
    elif service == "tiktok":
        env["TIKTOK_STATE_ROOT"] = str(TT_ROOT)
        env["TIKTOK_CONTROL_PORT"] = str(cfg["tiktok_port"])
    return env


def ensure_setup():
    if not CONFIG_FILE.exists():
        print("First run: configuration is not set up yet.")
        setup()


def start_one(service: str):
    ensure_setup()
    if service == "instagram":
        script = ROOT / "instagram_control_head.py"
        port = config()["instagram_port"]
    else:
        script = ROOT / "tiktok_control_head.py"
        port = config()["tiktok_port"]

    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(ROOT),
        env=env_for(service),
    )
    time.sleep(1.2)
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass
    return proc


def wait_for_processes(processes):
    try:
        while any(p.poll() is None for p in processes):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping Control Heads...")
        for proc in processes:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        deadline = time.time() + 5
        while time.time() < deadline and any(p.poll() is None for p in processes):
            time.sleep(0.2)
        for proc in processes:
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass


def show_paths():
    cfg = config()
    print(f"Repository:         {ROOT}")
    print(f"Private suite data: {SUITE_STATE}")
    print(f"Config:             {CONFIG_FILE}")
    print(f"Media pool:         {cfg['media_root']}")
    print(f"Instagram data:     {IG_ROOT}")
    print(f"TikTok data:        {TT_ROOT}")


def interactive_menu():
    while True:
        print("\nSocial Control Suite")
        print("1) Start both")
        print("2) Start Instagram")
        print("3) Start TikTok")
        print("4) Setup / edit private config")
        print("5) Show private-data paths")
        print("6) Exit")
        choice = input("> ").strip()
        if choice == "1":
            procs = [start_one("instagram"), start_one("tiktok")]
            wait_for_processes(procs)
            return
        if choice == "2":
            wait_for_processes([start_one("instagram")])
            return
        if choice == "3":
            wait_for_processes([start_one("tiktok")])
            return
        if choice == "4":
            setup()
        elif choice == "5":
            show_paths()
        elif choice == "6":
            return


def main():
    parser = argparse.ArgumentParser(description="Launch the Instagram/TikTok Control Heads.")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["both", "instagram", "tiktok", "setup", "paths"],
    )
    args = parser.parse_args()

    if args.command == "setup":
        setup()
    elif args.command == "paths":
        show_paths()
    elif args.command == "instagram":
        wait_for_processes([start_one("instagram")])
    elif args.command == "tiktok":
        wait_for_processes([start_one("tiktok")])
    elif args.command == "both":
        wait_for_processes([start_one("instagram"), start_one("tiktok")])
    else:
        interactive_menu()


if __name__ == "__main__":
    main()
