# Social Control Suite

Local Control Heads for Instagram and TikTok automation.

## Important

This repository intentionally contains **no account usernames, passwords, saved
Instagram sessions, TikTok cookies/browser profiles, histories, or personal
target lists**.

By default, private runtime data is stored outside the repository under:

- Windows: `%USERPROFILE%\.social-control-suite`
- Linux/macOS: `~/.social-control-suite`

Both Control Heads boot **disconnected**. Starting or restarting the Python
program does not automatically log accounts in.

Automation through unofficial/private interfaces can be restricted or rate
limited by the platforms. The built-in pacing controls reduce bursty behavior,
but they cannot guarantee an account will not be throttled or restricted.

## Requirements

- Python 3.11+
- Chromium installed through Playwright (installer handles this)
- ffmpeg recommended
- Ollama recommended
- A local Ollama vision model is required when `Require video vision` is enabled

## Install

### Windows

Double-click:

```text
install_windows.bat
```

Then:

```text
start_windows.bat
```

### Linux

```bash
chmod +x install.sh start.sh
./install.sh
./start.sh
```

Some Linux distributions may need Playwright/Chromium system packages. If
Chromium reports missing libraries, follow Playwright's Linux dependency
instructions for your distribution.

### macOS

```bash
chmod +x install.sh start.sh
./install.sh
./start.sh
```

## First run

The launcher opens a setup wizard. It asks for:

- shared media-pool path
- Instagram usernames
- TikTok usernames
- local Ollama text model
- optional local Ollama vision model
- Control Head ports

It does **not** ask for or store passwords.

You can rerun setup at any time:

```bash
python launcher.py setup
```

Or, from the virtual environment:

```bash
.venv/bin/python launcher.py setup
```

On Windows:

```text
.venv\Scripts\python.exe launcher.py setup
```

## Launch commands

```bash
python launcher.py both
python launcher.py instagram
python launcher.py tiktok
python launcher.py setup
python launcher.py paths
```

The default Control Head addresses are:

- Instagram: `http://127.0.0.1:8081`
- TikTok: `http://127.0.0.1:8080`

Both bind only to localhost.

## Instagram login model

Instagram starts disconnected. Use its Control Head to explicitly connect an
account. Saved sessions are stored in the private state directory, not in this
repository.

## TikTok login model

TikTok also starts disconnected.

Recommended flow:

1. Click **Open Login**.
2. Complete TikTok login, verification, or CAPTCHA manually in Chromium.
3. Leave the account logged in.
4. Click **Save Login** in the Control Head.
5. On later program starts, click **Quick Login** to validate/reuse that
   persistent profile.

The program does not attempt to bypass TikTok verification challenges.

## Behavior modes

Each account can independently use:

- Balanced
- Upload Only
- Follow Only
- Engage Only
- DM Only
- Manual

The Control Heads expose editable:

- persona / attitude prompt
- caption prompt
- DM prompt
- comment prompt
- caption/reply character limits
- target accounts
- target hashtags
- follower/following/both target mode
- follow limit per pass
- action pacing / rolling write limits
- upload cooldown
- video frame sample count

## Video captions

When a compatible local Ollama vision model is configured, the programs sample
multiple chronological frames from a video and ask the model to describe the
sequence. POV/selfie/vlog footage is then captioned in a first-person account
voice. Posts append exactly five hashtags based on the same visual context.

With `Require video vision` enabled, a video is skipped rather than pretending
it was visually analyzed when no suitable vision model/frames are available.

## Recent Posts

Each Control Head records successful uploads and shows the exact local source
folder. Use **Open Folder** to inspect/remove unwanted source material from the
media pool.
