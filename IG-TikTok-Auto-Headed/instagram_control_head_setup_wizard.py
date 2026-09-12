from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "Instagram Control Head"
BOT_BUNDLED_NAME = "instagram_control_head_reels_full_audio_analysis_snapback_fix.py"
BOT_INSTALLED_NAME = "instagram_control_head.py"
TEXT_MODEL = "llama3.1"
VISION_MODEL = "gemma3:4b"

HERE = Path(__file__).resolve().parent
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
DEFAULT_INSTALL = LOCALAPPDATA / "InstagramControlHead"
DEFAULT_DATA = Path.home() / ".local" / "share" / "instagram_bot" / "data"
DEFAULT_MEDIA = Path(r"D:\ig-reposts-data") if Path(r"D:\ig-reposts-data").exists() else Path.home() / "social-media-pool"


def quote_cmd(value: str | Path) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def find_executable(name: str, extra=()):
    found = shutil.which(name)
    if found:
        return Path(found)

    for candidate in extra:
        candidate = Path(candidate)
        if candidate.exists():
            return candidate

    return None


def run(cmd, *, log, cwd=None, check=True, env=None):
    if isinstance(cmd, (list, tuple)):
        shown = subprocess.list2cmdline([str(x) for x in cmd])
    else:
        shown = str(cmd)

    log(f"> {shown}")

    process = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=isinstance(cmd, str),
    )

    assert process.stdout is not None

    for line in process.stdout:
        log(line.rstrip())

    code = process.wait()

    if check and code != 0:
        raise RuntimeError(f"Command failed with exit code {code}: {shown}")

    return code


def winget_install(package_id: str, *, log):
    winget = shutil.which("winget")
    if not winget:
        raise RuntimeError(
            f"WinGet is not available. Install {package_id} manually, "
            "then run the wizard again."
        )

    return run(
        [
            winget,
            "install",
            "--id",
            package_id,
            "-e",
            "--source",
            "winget",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
        ],
        log=log,
        check=False,
    )


def find_ollama():
    return find_executable(
        "ollama",
        extra=[
            LOCALAPPDATA / "Programs" / "Ollama" / "ollama.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe",
        ],
    )


def find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)

    candidates = list(
        (LOCALAPPDATA / "Microsoft" / "WinGet" / "Packages").glob(
            "**/ffmpeg.exe"
        )
    )

    if candidates:
        return candidates[-1]

    return None


def ensure_tooling(log):
    log("Checking FFmpeg...")
    ffmpeg = find_ffmpeg()

    if not ffmpeg:
        log("FFmpeg not found; installing Gyan.FFmpeg with WinGet...")
        winget_install("Gyan.FFmpeg", log=log)
        ffmpeg = find_ffmpeg()

    if ffmpeg:
        log(f"FFmpeg: {ffmpeg}")
    else:
        log("WARNING: FFmpeg was not found after installation attempt.")

    log("Checking Ollama...")
    ollama = find_ollama()

    if not ollama:
        log("Ollama not found; installing Ollama.Ollama with WinGet...")
        winget_install("Ollama.Ollama", log=log)
        time.sleep(2)
        ollama = find_ollama()

    if not ollama:
        raise RuntimeError(
            "Ollama was not found after installation. Open the Ollama "
            "installer once, then rerun this wizard."
        )

    log(f"Ollama: {ollama}")
    return ffmpeg, ollama


def create_shortcut(shortcut_path: Path, target: Path, working_dir: Path, *, log):
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)

    ps = (
        "$ws=New-Object -ComObject WScript.Shell;"
        f"$s=$ws.CreateShortcut('{str(shortcut_path).replace(chr(39), chr(39)*2)}');"
        f"$s.TargetPath='{str(target).replace(chr(39), chr(39)*2)}';"
        f"$s.WorkingDirectory='{str(working_dir).replace(chr(39), chr(39)*2)}';"
        "$s.Save();"
    )

    run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        log=log,
        check=False,
    )


class SetupWizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Instagram Control Head Setup Wizard")
        self.geometry("830x650")
        self.minsize(760, 580)

        self.install_dir = tk.StringVar(value=str(DEFAULT_INSTALL))
        self.data_dir = tk.StringVar(value=str(DEFAULT_DATA))
        self.media_dir = tk.StringVar(value=str(DEFAULT_MEDIA))
        self.pull_models = tk.BooleanVar(value=True)
        self.install_whisper = tk.BooleanVar(value=True)
        self.make_desktop_shortcut = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Ready to install.")
        self._installing = False

        self._build()

    def _build(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Instagram Control Head Setup Wizard",
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            outer,
            text=(
                "Installs the bot in an isolated Python environment, "
                "Playwright Chromium, FFmpeg, Ollama, and local Whisper support. "
                "Existing Instagram sessions/data are preserved."
            ),
            wraplength=780,
        ).pack(anchor="w", pady=(4, 14))

        paths = ttk.LabelFrame(outer, text="Folders", padding=10)
        paths.pack(fill="x")

        self._path_row(paths, 0, "Install folder", self.install_dir)
        self._path_row(paths, 1, "Instagram state/data", self.data_dir)
        self._path_row(paths, 2, "Media root", self.media_dir)

        opts = ttk.LabelFrame(outer, text="Components", padding=10)
        opts.pack(fill="x", pady=(12, 0))

        ttk.Checkbutton(
            opts,
            text="Install faster-whisper for local reel audio transcription",
            variable=self.install_whisper,
        ).pack(anchor="w")

        ttk.Checkbutton(
            opts,
            text=f"Pull Ollama models: {TEXT_MODEL} and {VISION_MODEL}",
            variable=self.pull_models,
        ).pack(anchor="w", pady=(4, 0))

        ttk.Checkbutton(
            opts,
            text="Create Desktop shortcut",
            variable=self.make_desktop_shortcut,
        ).pack(anchor="w", pady=(4, 0))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(12, 8))

        self.install_button = ttk.Button(
            buttons,
            text="Install / Repair Everything",
            command=self.start_install,
        )
        self.install_button.pack(side="left")

        ttk.Button(
            buttons,
            text="Open Install Folder",
            command=self.open_install_folder,
        ).pack(side="left", padx=(8, 0))

        self.launch_button = ttk.Button(
            buttons,
            text="Launch Control Head",
            command=self.launch_installed,
        )
        self.launch_button.pack(side="right")

        ttk.Label(
            outer,
            textvariable=self.status,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(4, 4))

        self.progress = ttk.Progressbar(
            outer,
            mode="indeterminate",
        )
        self.progress.pack(fill="x", pady=(0, 8))

        log_frame = ttk.LabelFrame(outer, text="Setup log", padding=6)
        log_frame.pack(fill="both", expand=True)

        self.log_box = tk.Text(
            log_frame,
            wrap="word",
            height=20,
            font=("Consolas", 9),
        )
        self.log_box.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_box.yview,
        )
        scroll.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=scroll.set)

    def _path_row(self, parent, row, label, var):
        ttk.Label(parent, text=label, width=21).grid(
            row=row,
            column=0,
            sticky="w",
            pady=3,
        )

        ttk.Entry(parent, textvariable=var).grid(
            row=row,
            column=1,
            sticky="ew",
            padx=(4, 6),
            pady=3,
        )

        ttk.Button(
            parent,
            text="Browse",
            command=lambda v=var: self.browse(v),
        ).grid(row=row, column=2, pady=3)

        parent.columnconfigure(1, weight=1)

    def browse(self, var):
        selected = filedialog.askdirectory(
            initialdir=var.get() or str(Path.home())
        )

        if selected:
            var.set(selected)

    def log(self, message):
        def append():
            self.log_box.insert("end", str(message) + "\n")
            self.log_box.see("end")

        self.after(0, append)

    def set_status(self, message):
        self.after(0, lambda: self.status.set(message))

    def open_install_folder(self):
        path = Path(self.install_dir.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def launch_installed(self):
        launcher = Path(self.install_dir.get()).expanduser() / "Launch Instagram Control Head.cmd"

        if not launcher.exists():
            messagebox.showinfo(
                "Not installed yet",
                "Run Install / Repair Everything first.",
            )
            return

        os.startfile(launcher)

    def start_install(self):
        if self._installing:
            return

        self._installing = True
        self.install_button.configure(state="disabled")
        self.progress.start(12)
        self.status.set("Installing...")

        threading.Thread(
            target=self._install_worker,
            daemon=True,
        ).start()

    def _install_worker(self):
        try:
            self._install()
        except Exception as exc:
            self.log("")
            self.log(f"ERROR: {type(exc).__name__}: {exc}")
            self.set_status("Setup failed. See the log above.")
            self.after(
                0,
                lambda: messagebox.showerror(
                    "Setup failed",
                    str(exc),
                ),
            )
        else:
            self.set_status("Installation complete.")
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Setup complete",
                    "Instagram Control Head is installed and ready.",
                ),
            )
        finally:
            self._installing = False
            self.after(0, self.progress.stop)
            self.after(
                0,
                lambda: self.install_button.configure(state="normal"),
            )

    def _install(self):
        install_dir = Path(self.install_dir.get()).expanduser().resolve()
        data_dir = Path(self.data_dir.get()).expanduser().resolve()
        media_dir = Path(self.media_dir.get()).expanduser().resolve()

        install_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)

        bundled_bot = HERE / BOT_BUNDLED_NAME

        if not bundled_bot.exists():
            raise RuntimeError(
                f"Bundled bot file is missing: {bundled_bot}"
            )

        self.log(f"Install folder: {install_dir}")
        self.log(f"State/data folder: {data_dir}")
        self.log(f"Media root: {media_dir}")
        self.log("Existing state/session files will not be deleted.")

        ffmpeg, ollama = ensure_tooling(self.log)

        venv = install_dir / ".venv"
        venv_python = venv / "Scripts" / "python.exe"

        if not venv_python.exists():
            self.set_status("Creating isolated Python environment...")
            run(
                [sys.executable, "-m", "venv", str(venv)],
                log=self.log,
            )

        self.set_status("Installing Python packages...")

        run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "pip",
                "setuptools",
                "wheel",
            ],
            log=self.log,
        )

        packages = [
            "instagrapi==2.18.19",
            "pyotp",
            "httpx",
            "requests",
            "moviepy==2.2.1",
            "imageio-ffmpeg",
            "playwright",
            "ollama",
            "Pillow",
        ]

        if self.install_whisper.get():
            packages.append("faster-whisper")

        run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                *packages,
            ],
            log=self.log,
        )

        self.set_status("Installing Playwright Chromium...")
        run(
            [
                str(venv_python),
                "-m",
                "playwright",
                "install",
                "chromium",
            ],
            log=self.log,
        )

        self.set_status("Installing Control Head files...")
        installed_bot = install_dir / BOT_INSTALLED_NAME
        shutil.copy2(bundled_bot, installed_bot)

        config = {
            "install_dir": str(install_dir),
            "data_root": str(data_dir),
            "media_root": str(media_dir),
            "text_model": TEXT_MODEL,
            "vision_model": VISION_MODEL,
            "ffmpeg": str(ffmpeg or ""),
            "ollama": str(ollama),
        }

        (install_dir / "setup_config.json").write_text(
            json.dumps(config, indent=2),
            encoding="utf-8",
        )

        launcher = install_dir / "Launch Instagram Control Head.cmd"

        launcher.write_text(
            "@echo off\n"
            "setlocal\n"
            f"set \"IG_DATA_ROOT={data_dir}\"\n"
            f"set \"IG_MEDIA_ROOT={media_dir}\"\n"
            f"set \"IG_OLLAMA_MODEL={TEXT_MODEL}\"\n"
            f"set \"IG_OLLAMA_VISION_MODEL={VISION_MODEL}\"\n"
            "set \"PYTHONUTF8=1\"\n"
            "cd /d \"%~dp0\"\n"
            "start \"Instagram Control Head\" cmd /k "
            "\"\"%~dp0.venv\\Scripts\\python.exe\" \"%~dp0instagram_control_head.py\"\"\n"
            "timeout /t 3 /nobreak >nul\n"
            "start \"\" \"http://127.0.0.1:8081\"\n"
            "endlocal\n",
            encoding="utf-8",
        )

        repair = install_dir / "Repair Setup.cmd"
        repair.write_text(
            "@echo off\n"
            f"\"{sys.executable}\" \"{HERE / Path(__file__).name}\"\n",
            encoding="utf-8",
        )

        if self.pull_models.get():
            self.set_status("Starting Ollama and pulling AI models...")

            try:
                subprocess.Popen(
                    [str(ollama), "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass

            time.sleep(2)

            for model in (TEXT_MODEL, VISION_MODEL):
                self.log(f"Ensuring Ollama model is installed: {model}")
                run(
                    [str(ollama), "pull", model],
                    log=self.log,
                    check=False,
                )

        start_menu = (
            Path(os.environ.get("APPDATA", Path.home()))
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Instagram Control Head.lnk"
        )

        create_shortcut(
            start_menu,
            launcher,
            install_dir,
            log=self.log,
        )

        if self.make_desktop_shortcut.get():
            desktop = Path.home() / "Desktop" / "Instagram Control Head.lnk"
            create_shortcut(
                desktop,
                launcher,
                install_dir,
                log=self.log,
            )

        self.log("")
        self.log("Installation complete.")
        self.log(f"Launcher: {launcher}")
        self.log(f"Dashboard: http://127.0.0.1:8081")
        self.log("Existing saved browser/session data was preserved.")


if __name__ == "__main__":
    SetupWizard().mainloop()
