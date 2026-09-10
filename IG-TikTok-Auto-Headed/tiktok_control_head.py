#!/usr/bin/env python3
"""
TikTok Control Head v3
----------------------
Local-only TikTok automation dashboard with:
- DISCONNECTED boot (no TikTok browser/login/API traffic at startup)
- Persistent Playwright Chromium profiles
- Open Login -> user completes TikTok login/challenge manually -> Save Login
- Quick Login validates a saved profile only when explicitly clicked
- Disconnect keeps saved profile; Clear Saved Login removes TikTok cookies/storage
- Per-account modes:
    balanced, upload_only, follow_only, engage_only, dm_only, manual
- Per-account editable persona/caption/DM/comment prompts
- Per-account caption/reply char limits
- Per-account target accounts / hashtags
- Follow-only mode for followers/following/both of configured target accounts
- Conservative shared write pacing across accounts in this process
- Video "watching": samples 3-9 chronological frames (default 5)
- Requires local Ollama vision by default for video posting
- First-person narration for POV/selfie/vlog footage
- Exactly 5 media-aware hashtags appended to every generated post caption
- Recent Posts panel with exact source folder and Open Folder button
- Auto-refresh pauses while typing and can be disabled entirely

This does not bypass TikTok login challenges/CAPTCHAs. Complete those manually
in the persistent browser opened by the Control Head.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError as exc:
    raise SystemExit(
        "Missing Playwright.\n"
        "Install with:\n"
        "  python -m pip install playwright\n"
        "  python -m playwright install chromium"
    ) from exc

try:
    import ollama
except ImportError:
    ollama = None


# =============================================================================
# Paths / configuration
# =============================================================================

HOST = "127.0.0.1"
PORT = int(os.environ.get("TIKTOK_CONTROL_PORT", "8080"))

APP_ROOT = Path(
    os.environ.get(
        "TIKTOK_STATE_ROOT",
        str(Path.home() / ".local" / "share" / "tiktok_dashboard"),
    )
).expanduser()

PROFILE_ROOT = APP_ROOT / "profiles"
DATA_ROOT = APP_ROOT / "data"
HISTORY_ROOT = DATA_ROOT / "history"
CACHE_ROOT = DATA_ROOT / "cache"
ACCOUNTS_FILE = DATA_ROOT / "accounts.json"
SETTINGS_FILE = DATA_ROOT / "control_settings.json"
ANALYTICS_FILE = DATA_ROOT / "analytics.json"
RECENT_POSTS_FILE = DATA_ROOT / "recent_posts.json"

MEDIA_ROOT = Path(
    os.environ.get(
        "TIKTOK_MEDIA_ROOT",
        str(Path.home() / "social-media-pool"),
    )
).expanduser()

TIKTOK_HOME = "https://www.tiktok.com/"
TIKTOK_LOGIN = "https://www.tiktok.com/login"
TIKTOK_UPLOAD = "https://www.tiktok.com/tiktokstudio/upload"
PAGE_TIMEOUT_MS = max(10_000, int(os.environ.get("TIKTOK_PAGE_TIMEOUT_MS", "45000")))

OLLAMA_MODEL = os.environ.get(
    "TIKTOK_OLLAMA_MODEL",
    os.environ.get("OLLAMA_MODEL", "llama3.1"),
).strip() or "llama3.1"

OLLAMA_VISION_MODEL = os.environ.get(
    "TIKTOK_OLLAMA_VISION_MODEL",
    os.environ.get("OLLAMA_VISION_MODEL", ""),
).strip()

GLOBAL_WRITE_MIN_GAP_SECONDS = max(
    5, int(os.environ.get("TIKTOK_GLOBAL_WRITE_GAP", "12"))
)

SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_VIDEOS = {".mp4", ".mov", ".m4v", ".webm"}
SUPPORTED_MEDIA = SUPPORTED_IMAGES | SUPPORTED_VIDEOS

DEFAULT_ACCOUNTS = []


PERSONA_DEFAULT = """You are the account's fictional social-media voice:
very observant, articulate, quick, dry, confident, witty, and mildly
condescending when the situation earns it.

Stay on the ACTUAL subject. Match people's energy. Friendly gets clever-friendly;
questions get real answers; sarcasm gets sarcasm back; rudeness gets a controlled
sting. Obvious advertising, follower-selling, cold promotion, investment pitches,
and spam get a short dismissive refusal.

Do not force trading, climbing, medicine, or any other recurring topic into
unrelated content. Do not invent facts, credentials, identities, relationships,
locations, or private information. Never mention being an AI, bot, automation,
prompt, or Ollama. Do not use slurs, threats, or attacks on protected traits."""


# =============================================================================
# Runtime state
# =============================================================================

STATE_LOCK = threading.RLock()
ACCOUNT_LOCKS: dict[str, threading.Lock] = {}
CONNECTED_ACCOUNTS: set[str] = set()
AUTO_ENABLED_ACCOUNTS: set[str] = set()
ACCOUNT_COOLDOWNS: dict[str, datetime] = {}
NEXT_TASK_OVERRIDE: dict[str, str] = {}
NEXT_DUE: dict[str, float] = {}

LOGIN_SAVE_EVENTS: dict[str, threading.Event] = {}
LOGIN_CANCEL_EVENTS: dict[str, threading.Event] = {}
LOGIN_THREADS: dict[str, threading.Thread] = {}

AUTOMATION_STOP = threading.Event()
AUTOMATION_THREAD: threading.Thread | None = None

WRITE_LOCK = threading.RLock()
ACCOUNT_WRITE_TIMES: dict[str, list[float]] = {}
ACCOUNT_LAST_WRITE: dict[str, float] = {}
ACCOUNT_LAST_UPLOAD: dict[str, float] = {}
GLOBAL_LAST_WRITE = 0.0

VISION_MODEL_CACHE: str | None | bool = False


# =============================================================================
# General persistence
# =============================================================================

def ensure_dirs() -> None:
    for path in (APP_ROOT, PROFILE_ROOT, DATA_ROOT, HISTORY_ROOT, CACHE_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value
    except Exception:
        pass
    return json.loads(json.dumps(default))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def normalize_username(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._@]", "", str(value or "").strip())
    if not value:
        return ""
    return value if value.startswith("@") else "@" + value


def load_accounts() -> list[dict[str, Any]]:
    data = load_json(ACCOUNTS_FILE, DEFAULT_ACCOUNTS)
    if not isinstance(data, list):
        data = json.loads(json.dumps(DEFAULT_ACCOUNTS))
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id", "")).strip()
        user = normalize_username(item.get("username", ""))
        if aid and user:
            out.append(
                {
                    "id": aid,
                    "username": user,
                    "enabled": bool(item.get("enabled", True)),
                }
            )
    return out or json.loads(json.dumps(DEFAULT_ACCOUNTS))


ACCOUNTS = load_accounts()


def save_accounts() -> None:
    save_json(ACCOUNTS_FILE, ACCOUNTS)


def find_account(account_id: str) -> dict[str, Any] | None:
    return next((a for a in ACCOUNTS if a["id"] == account_id), None)


def account_lock(account_id: str) -> threading.Lock:
    with STATE_LOCK:
        return ACCOUNT_LOCKS.setdefault(account_id, threading.Lock())


def add_account(username: str) -> dict[str, Any]:
    username = normalize_username(username)
    if not username:
        raise ValueError("Invalid TikTok username.")
    for a in ACCOUNTS:
        if a["username"].lower() == username.lower():
            return a
    used = {a["id"] for a in ACCOUNTS}
    n = 1
    while f"tt_{n}" in used:
        n += 1
    account = {"id": f"tt_{n}", "username": username, "enabled": True}
    ACCOUNTS.append(account)
    save_accounts()
    ensure_account_analytics(account)
    return account


def rename_account(account_id: str, username: str) -> None:
    account = find_account(account_id)
    if not account:
        raise ValueError("Unknown account.")
    username = normalize_username(username)
    if not username:
        raise ValueError("Invalid username.")
    account["username"] = username
    save_accounts()
    ensure_account_analytics(account)


# =============================================================================
# Analytics
# =============================================================================

def default_account_analytics(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": account["username"],
        "connected": False,
        "auto_enabled": False,
        "status": "Disconnected",
        "followers": None,
        "following": None,
        "likes": None,
        "videos": None,
        "posts_sent": 0,
        "likes_sent": 0,
        "follows_sent": 0,
        "dm_replies": 0,
        "comment_replies": 0,
        "last_action": None,
        "last_checked": None,
        "last_error": None,
        "cooldown_until": None,
    }


def load_analytics() -> dict[str, Any]:
    data = load_json(
        ANALYTICS_FILE,
        {"updated_at": None, "accounts": {}, "activity": []},
    )
    if not isinstance(data, dict):
        data = {"updated_at": None, "accounts": {}, "activity": []}
    data.setdefault("accounts", {})
    data.setdefault("activity", [])
    for account in ACCOUNTS:
        existing = data["accounts"].get(account["id"], {})
        merged = default_account_analytics(account)
        if isinstance(existing, dict):
            merged.update(existing)
        merged["username"] = account["username"]
        merged["connected"] = account["id"] in CONNECTED_ACCOUNTS
        merged["auto_enabled"] = account["id"] in AUTO_ENABLED_ACCOUNTS
        data["accounts"][account["id"]] = merged
    return data


def save_analytics(data: dict[str, Any]) -> None:
    data["updated_at"] = now_iso()
    save_json(ANALYTICS_FILE, data)


def ensure_account_analytics(account: dict[str, Any]) -> None:
    data = load_analytics()
    data["accounts"].setdefault(
        account["id"], default_account_analytics(account)
    )
    data["accounts"][account["id"]]["username"] = account["username"]
    save_analytics(data)


def update_account(account_id: str, **changes: Any) -> None:
    data = load_analytics()
    account = find_account(account_id)
    if account:
        data["accounts"].setdefault(
            account_id, default_account_analytics(account)
        )
    data["accounts"].setdefault(account_id, {})
    data["accounts"][account_id].update(changes)
    save_analytics(data)


def add_activity(message: str, level: str = "info") -> None:
    data = load_analytics()
    data["activity"].insert(
        0,
        {"time": now_iso(), "level": level, "message": message},
    )
    data["activity"] = data["activity"][:250]
    save_analytics(data)
    log(message)


# =============================================================================
# Per-account settings
# =============================================================================

def normalize_list(value: Any, strip_prefix: str = "") -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[\n,]+", value)
    elif isinstance(value, list):
        parts = value
    else:
        parts = []
    out = []
    for item in parts:
        value_s = str(item or "").strip()
        if strip_prefix:
            value_s = value_s.lstrip(strip_prefix)
        if value_s and value_s not in out:
            out.append(value_s)
    return out[:100]


def default_settings(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "balanced",
        "persona_prompt": PERSONA_DEFAULT,
        "caption_prompt": (
            "Narrate what actually happens in the video. For POV/selfie/vlog "
            "footage, write naturally in first person as the fictional filmer. "
            "Be specific to the visible sequence."
        ),
        "dm_prompt": (
            "Follow the actual conversation, answer what they said, match their "
            "energy, reject obvious advertising, and stay off unrelated pet topics."
        ),
        "comment_prompt": (
            "Reply to the actual comment with a concise, context-aware response. "
            "Match its energy and vary phrasing."
        ),
        "caption_char_limit": 350,
        "reply_char_limit": 220,
        "target_accounts": [],
        "target_hashtags": ["fyp"],
        "follow_source": "both",
        "follow_limit": 2,
        "video_frames": 5,
        "require_video_vision": True,
        "enable_dm_replies": True,
        "enable_comment_replies": True,
        "headless_automation": False,
        "max_writes_per_window": 6,
        "write_window_seconds": 900,
        "write_min_gap_seconds": 30,
        "upload_cooldown_seconds": 1800,
        "max_writes_per_workflow": 3,
        "workflow_min_seconds": 300,
        "workflow_max_seconds": 720,
    }


def sanitize_settings(account: dict[str, Any], raw: Any) -> dict[str, Any]:
    base = default_settings(account)
    raw = raw if isinstance(raw, dict) else {}

    mode = str(raw.get("mode", base["mode"])).strip().lower()
    if mode not in {
        "balanced", "upload_only", "follow_only",
        "engage_only", "dm_only", "manual",
    }:
        mode = "balanced"

    source = str(raw.get("follow_source", base["follow_source"])).strip().lower()
    if source not in {"followers", "following", "both"}:
        source = "both"

    def iv(key: str, lo: int, hi: int) -> int:
        try:
            value = int(raw.get(key, base[key]))
        except Exception:
            value = int(base[key])
        return max(lo, min(hi, value))

    wmin = iv("workflow_min_seconds", 60, 7200)
    wmax = max(wmin, iv("workflow_max_seconds", wmin, 14400))

    return {
        "mode": mode,
        "persona_prompt": str(
            raw.get("persona_prompt", base["persona_prompt"])
        )[:6000],
        "caption_prompt": str(
            raw.get("caption_prompt", base["caption_prompt"])
        )[:4000],
        "dm_prompt": str(raw.get("dm_prompt", base["dm_prompt"]))[:4000],
        "comment_prompt": str(
            raw.get("comment_prompt", base["comment_prompt"])
        )[:4000],
        "caption_char_limit": iv("caption_char_limit", 80, 2000),
        "reply_char_limit": iv("reply_char_limit", 20, 1000),
        "target_accounts": normalize_list(
            raw.get("target_accounts", base["target_accounts"]), "@"
        ),
        "target_hashtags": normalize_list(
            raw.get("target_hashtags", base["target_hashtags"]), "#"
        ),
        "follow_source": source,
        "follow_limit": iv("follow_limit", 1, 10),
        "video_frames": iv("video_frames", 3, 9),
        "require_video_vision": bool(
            raw.get("require_video_vision", base["require_video_vision"])
        ),
        "enable_dm_replies": bool(
            raw.get("enable_dm_replies", base["enable_dm_replies"])
        ),
        "enable_comment_replies": bool(
            raw.get("enable_comment_replies", base["enable_comment_replies"])
        ),
        "headless_automation": bool(
            raw.get("headless_automation", base["headless_automation"])
        ),
        "max_writes_per_window": iv("max_writes_per_window", 1, 20),
        "write_window_seconds": iv("write_window_seconds", 60, 7200),
        "write_min_gap_seconds": iv("write_min_gap_seconds", 10, 600),
        "upload_cooldown_seconds": iv("upload_cooldown_seconds", 300, 21600),
        "max_writes_per_workflow": iv("max_writes_per_workflow", 1, 8),
        "workflow_min_seconds": wmin,
        "workflow_max_seconds": wmax,
    }


def load_settings_file() -> dict[str, Any]:
    value = load_json(SETTINGS_FILE, {})
    return value if isinstance(value, dict) else {}


def get_settings(account_id: str) -> dict[str, Any]:
    account = find_account(account_id)
    if not account:
        raise ValueError("Unknown account.")
    raw = load_settings_file().get(account_id, {})
    return sanitize_settings(account, raw)


def save_settings(account_id: str, patch: Any) -> dict[str, Any]:
    account = find_account(account_id)
    if not account:
        raise ValueError("Unknown account.")
    if not isinstance(patch, dict):
        raise ValueError("Settings must be an object.")

    data = load_settings_file()
    current = data.get(account_id, {})
    current = dict(current) if isinstance(current, dict) else {}
    current.update(patch)
    clean = sanitize_settings(account, current)
    data[account_id] = clean
    save_json(SETTINGS_FILE, data)
    add_activity(
        f"{account['username']}: saved Control Head settings "
        f"(mode={clean['mode']})",
        "good",
    )
    return clean


# =============================================================================
# History / recent posts
# =============================================================================

def history_path(account_id: str) -> Path:
    return HISTORY_ROOT / f"{account_id}.json"


def load_history(account_id: str) -> dict[str, Any]:
    default = {
        "posted_ids": [],
        "replied_dms": [],
        "sent_dm_texts": [],
        "dm_reply_memory": {},
        "replied_comments": [],
        "liked_videos": [],
        "followed_users": [],
    }
    value = load_json(history_path(account_id), default)
    if not isinstance(value, dict):
        value = default
    for key, dv in default.items():
        value.setdefault(key, json.loads(json.dumps(dv)))
    return value


def save_history(account_id: str, history: dict[str, Any]) -> None:
    save_json(history_path(account_id), history)


def load_recent_posts() -> list[dict[str, Any]]:
    value = load_json(RECENT_POSTS_FILE, [])
    return value if isinstance(value, list) else []


def record_recent_post(
    account_id: str,
    source_folder: Path,
    caption: str,
    post_url: str = "",
) -> None:
    account = find_account(account_id)
    items = load_recent_posts()
    items.insert(
        0,
        {
            "account_id": account_id,
            "account": account["username"] if account else account_id,
            "at": now_iso(),
            "folder": str(source_folder.resolve()),
            "caption": str(caption or "")[:500],
            "post_url": str(post_url or ""),
        },
    )
    save_json(RECENT_POSTS_FILE, items[:100])


def stable_key(*parts: str) -> str:
    payload = "\x1f".join(str(p) for p in parts).encode("utf-8", "replace")
    return hashlib.sha256(payload).hexdigest()[:24]


# =============================================================================
# Pacing
# =============================================================================

def _prune_write_window(account_id: str) -> list[float]:
    settings = get_settings(account_id)
    cutoff = time.monotonic() - int(settings["write_window_seconds"])
    times = ACCOUNT_WRITE_TIMES.setdefault(account_id, [])
    times[:] = [t for t in times if t >= cutoff]
    return times


def can_write_now(account_id: str, action_type: str = "write") -> tuple[bool, str]:
    global GLOBAL_LAST_WRITE
    settings = get_settings(account_id)
    now = time.monotonic()
    times = _prune_write_window(account_id)

    if len(times) >= int(settings["max_writes_per_window"]):
        wait = int(
            max(
                1,
                times[0] + int(settings["write_window_seconds"]) - now,
            )
        )
        return False, f"rolling write budget; retry in ~{wait}s"

    last = ACCOUNT_LAST_WRITE.get(account_id, 0.0)
    if now - last < int(settings["write_min_gap_seconds"]):
        wait = int(
            max(1, int(settings["write_min_gap_seconds"]) - (now - last))
        )
        return False, f"account write gap; retry in ~{wait}s"

    if now - GLOBAL_LAST_WRITE < GLOBAL_WRITE_MIN_GAP_SECONDS:
        wait = int(max(1, GLOBAL_WRITE_MIN_GAP_SECONDS - (now - GLOBAL_LAST_WRITE)))
        return False, f"global multi-account gap; retry in ~{wait}s"

    if action_type == "upload":
        last_upload = ACCOUNT_LAST_UPLOAD.get(account_id, 0.0)
        if now - last_upload < int(settings["upload_cooldown_seconds"]):
            wait = int(
                max(
                    1,
                    int(settings["upload_cooldown_seconds"])
                    - (now - last_upload),
                )
            )
            return False, f"upload cooldown; retry in ~{wait}s"

    return True, ""


def wait_for_write_slot(
    account_id: str,
    action_type: str = "write",
    max_wait: int = 120,
) -> bool:
    start = time.monotonic()
    reason = ""
    while time.monotonic() - start < max_wait:
        with WRITE_LOCK:
            ok, reason = can_write_now(account_id, action_type)
            if ok:
                return True
        time.sleep(2)
    account = find_account(account_id)
    add_activity(
        f"{account['username'] if account else account_id}: "
        f"deferred {action_type}: {reason}",
        "warn",
    )
    return False


def record_write(account_id: str, action_type: str = "write") -> None:
    global GLOBAL_LAST_WRITE
    now = time.monotonic()
    with WRITE_LOCK:
        _prune_write_window(account_id).append(now)
        ACCOUNT_LAST_WRITE[account_id] = now
        GLOBAL_LAST_WRITE = now
        if action_type == "upload":
            ACCOUNT_LAST_UPLOAD[account_id] = now


# =============================================================================
# Browser / login helpers
# =============================================================================

def profile_path(account_id: str) -> Path:
    return PROFILE_ROOT / account_id


def profile_is_locked(path: Path) -> bool:
    return any(
        (path / name).exists()
        for name in (
            "SingletonLock",
            "SingletonCookie",
            "SingletonSocket",
            "DevToolsActivePort",
        )
    )


def launch_profile(
    playwright,
    account_id: str,
    headless: bool = False,
):
    path = profile_path(account_id)
    path.mkdir(parents=True, exist_ok=True)
    if profile_is_locked(path):
        raise RuntimeError(
            f"Chromium profile is already in use: {path}. "
            "Close its browser window first."
        )
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(path),
        headless=headless,
        viewport={"width": 1440, "height": 950},
        args=[],
    )


def goto(page, url: str, pause_ms: int = 1800) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(pause_ms)


def explicit_login_button_visible(page) -> bool:
    selectors = [
        "a[data-e2e='nav-login-button']",
        "#header-login-button",
        "#top-right-login-button",
        "#top-right-action-bar-login-button",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible(timeout=1000):
                return True
        except Exception:
            pass
    return False


def current_profile_username(page) -> str:
    selectors = [
        "[data-e2e='nav-profile']",
        "a[data-e2e='profile-icon']",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if not loc.count():
                continue
            href = loc.get_attribute("href") or ""
            m = re.search(r"/@([^/?#]+)", href)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    return ""


def verify_saved_login(page, account: dict[str, Any]) -> tuple[bool, str]:
    """
    Validate the persistent browser session only after an explicit user action.
    TikTok challenges/CAPTCHA are never bypassed.
    """
    try:
        goto(page, TIKTOK_UPLOAD, 3000)
    except Exception as exc:
        return False, f"Could not open TikTok Studio: {exc}"

    if "/login" in page.url.lower() or explicit_login_button_visible(page):
        return False, "TikTok is asking for login."

    try:
        file_input = page.locator("input[type='file']").first
        studio_usable = bool(file_input.count())
    except Exception:
        studio_usable = False

    detected = current_profile_username(page)
    expected = account["username"].lstrip("@")

    if detected and detected.lower() != expected.lower():
        return (
            False,
            f"Profile identity mismatch: expected @{expected}, "
            f"browser appears to be @{detected}.",
        )

    if studio_usable:
        identity_note = (
            f" verified as @{detected}" if detected else " (identity selector unavailable)"
        )
        return True, f"TikTok Studio is usable{identity_note}."

    # Studio layout can vary. Fall back to home only when no explicit login UI exists.
    try:
        goto(page, TIKTOK_HOME, 1800)
    except Exception as exc:
        return False, f"TikTok session validation failed: {exc}"

    if explicit_login_button_visible(page) or "/login" in page.url.lower():
        return False, "TikTok is asking for login."

    detected = current_profile_username(page)
    if detected and detected.lower() != expected.lower():
        return (
            False,
            f"Profile identity mismatch: expected @{expected}, "
            f"browser appears to be @{detected}.",
        )

    return True, "Saved browser session appears usable."


def open_login_worker(account_id: str) -> None:
    account = find_account(account_id)
    if not account:
        return

    lock = account_lock(account_id)
    if not lock.acquire(blocking=False):
        add_activity(
            f"{account['username']}: browser/profile is already busy",
            "warn",
        )
        return

    save_event = LOGIN_SAVE_EVENTS.setdefault(account_id, threading.Event())
    cancel_event = LOGIN_CANCEL_EVENTS.setdefault(account_id, threading.Event())
    save_event.clear()
    cancel_event.clear()

    try:
        add_activity(
            f"{account['username']}: opening persistent login browser",
            "info",
        )
        update_account(
            account_id,
            status="Login browser open",
            connected=False,
            last_error=None,
        )

        with sync_playwright() as p:
            context = launch_profile(p, account_id, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            goto(page, TIKTOK_LOGIN, 1500)

            add_activity(
                f"{account['username']}: complete login/challenge in Chromium, "
                "then press Save Login in Control Head",
                "warn",
            )

            while True:
                if cancel_event.wait(0.5):
                    update_account(
                        account_id,
                        status="Disconnected",
                        connected=False,
                    )
                    add_activity(
                        f"{account['username']}: login browser cancelled",
                        "warn",
                    )
                    break

                if save_event.is_set():
                    save_event.clear()
                    ok, reason = verify_saved_login(page, account)
                    if ok:
                        CONNECTED_ACCOUNTS.add(account_id)
                        update_account(
                            account_id,
                            status="Connected",
                            connected=True,
                            last_error=None,
                            last_checked=now_iso(),
                        )
                        add_activity(
                            f"{account['username']}: ✅ login saved/verified; "
                            "persistent profile ready for Quick Login",
                            "good",
                        )
                        break
                    else:
                        update_account(
                            account_id,
                            status="Login browser open",
                            connected=False,
                            last_error=reason,
                        )
                        add_activity(
                            f"{account['username']}: login not ready: {reason}",
                            "warn",
                        )

                try:
                    if not context.pages:
                        update_account(
                            account_id,
                            status="Disconnected",
                            connected=False,
                        )
                        break
                except Exception:
                    break

            try:
                context.close()
            except Exception:
                pass

    except Exception as exc:
        update_account(
            account_id,
            status="Login error",
            connected=False,
            last_error=str(exc),
        )
        add_activity(
            f"{account['username']}: login browser error: {exc}",
            "error",
        )
    finally:
        lock.release()


def start_login_browser(account_id: str) -> None:
    thread = LOGIN_THREADS.get(account_id)
    if thread and thread.is_alive():
        raise RuntimeError("Login browser is already open for this account.")
    thread = threading.Thread(
        target=open_login_worker,
        args=(account_id,),
        daemon=True,
        name=f"tiktok-login-{account_id}",
    )
    LOGIN_THREADS[account_id] = thread
    thread.start()


def request_save_login(account_id: str) -> None:
    thread = LOGIN_THREADS.get(account_id)
    if not thread or not thread.is_alive():
        raise RuntimeError("Open Login first.")
    LOGIN_SAVE_EVENTS.setdefault(account_id, threading.Event()).set()


def cancel_login(account_id: str) -> None:
    LOGIN_CANCEL_EVENTS.setdefault(account_id, threading.Event()).set()


def quick_login_worker(account_id: str) -> None:
    account = find_account(account_id)
    if not account:
        return
    lock = account_lock(account_id)
    if not lock.acquire(blocking=False):
        add_activity(f"{account['username']}: profile is busy", "warn")
        return

    try:
        update_account(
            account_id,
            status="Checking saved login",
            connected=False,
            last_error=None,
        )
        with sync_playwright() as p:
            context = launch_profile(p, account_id, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            ok, reason = verify_saved_login(page, account)
            context.close()

        if ok:
            CONNECTED_ACCOUNTS.add(account_id)
            update_account(
                account_id,
                status="Connected",
                connected=True,
                last_error=None,
                last_checked=now_iso(),
            )
            add_activity(
                f"{account['username']}: ✅ Quick Login verified saved profile",
                "good",
            )
        else:
            CONNECTED_ACCOUNTS.discard(account_id)
            AUTO_ENABLED_ACCOUNTS.discard(account_id)
            update_account(
                account_id,
                status="Login needed",
                connected=False,
                auto_enabled=False,
                last_error=reason,
            )
            add_activity(
                f"{account['username']}: Quick Login failed: {reason}",
                "warn",
            )
    except Exception as exc:
        CONNECTED_ACCOUNTS.discard(account_id)
        update_account(
            account_id,
            status="Login error",
            connected=False,
            last_error=str(exc),
        )
        add_activity(
            f"{account['username']}: Quick Login error: {exc}",
            "error",
        )
    finally:
        lock.release()


def quick_login(account_id: str) -> None:
    threading.Thread(
        target=quick_login_worker,
        args=(account_id,),
        daemon=True,
        name=f"tiktok-quick-login-{account_id}",
    ).start()


def disconnect_account(account_id: str) -> None:
    account = find_account(account_id)
    if not account:
        raise ValueError("Unknown account.")
    CONNECTED_ACCOUNTS.discard(account_id)
    AUTO_ENABLED_ACCOUNTS.discard(account_id)
    cancel_login(account_id)
    update_account(
        account_id,
        status="Disconnected",
        connected=False,
        auto_enabled=False,
    )
    add_activity(
        f"{account['username']}: disconnected locally; saved browser profile kept",
        "good",
    )


def clear_saved_login_worker(account_id: str) -> None:
    account = find_account(account_id)
    if not account:
        return
    lock = account_lock(account_id)
    if not lock.acquire(blocking=False):
        add_activity(f"{account['username']}: profile is busy", "warn")
        return
    try:
        CONNECTED_ACCOUNTS.discard(account_id)
        AUTO_ENABLED_ACCOUNTS.discard(account_id)

        with sync_playwright() as p:
            context = launch_profile(p, account_id, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                goto(page, TIKTOK_HOME, 1000)
            except Exception:
                pass
            try:
                context.clear_cookies()
            except Exception:
                pass
            try:
                page.evaluate(
                    """() => {
                        try { localStorage.clear(); } catch(e) {}
                        try { sessionStorage.clear(); } catch(e) {}
                    }"""
                )
            except Exception:
                pass
            context.close()

        update_account(
            account_id,
            status="Disconnected",
            connected=False,
            auto_enabled=False,
            last_error=None,
        )
        add_activity(
            f"{account['username']}: cleared saved TikTok login cookies/storage",
            "good",
        )
    except Exception as exc:
        add_activity(
            f"{account['username']}: clear-login error: {exc}",
            "error",
        )
    finally:
        lock.release()


# =============================================================================
# Media / Ollama vision
# =============================================================================

def find_ffmpeg() -> str | None:
    override = os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    if override and Path(override).exists():
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    return None


def ffprobe_duration(path: Path) -> float | None:
    candidates = []
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        candidates.append(ffprobe)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        sibling = Path(ffmpeg).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe"
        )
        if sibling.exists():
            candidates.append(str(sibling))

    for exe in candidates:
        try:
            result = subprocess.run(
                [
                    exe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            duration = float(result.stdout.strip())
            if duration > 0:
                return duration
        except Exception:
            pass
    return None


def cache_name(path: Path, suffix: str, salt: str = "") -> Path:
    stat = path.stat()
    token = (
        f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{salt}"
    )
    digest = hashlib.sha256(
        token.encode("utf-8", "replace")
    ).hexdigest()[:22]
    return CACHE_ROOT / f"{digest}{suffix}"


def extract_video_frames(path: Path, count: int = 5) -> list[Path]:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return []

    count = max(3, min(9, int(count)))
    duration = ffprobe_duration(path)
    if duration and duration > 1:
        positions = [
            duration * ((i + 1) / (count + 1))
            for i in range(count)
        ]
    else:
        positions = [0.2 + i for i in range(count)]

    frames = []
    for index, sec in enumerate(positions, 1):
        out = cache_name(path, ".jpg", f"story_{count}_{index}")
        if out.exists() and out.stat().st_size > 1024:
            frames.append(out)
            continue
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-y",
                    "-ss", f"{sec:.3f}",
                    "-i", str(path),
                    "-frames:v", "1",
                    "-vf", "scale=960:-2:force_original_aspect_ratio=decrease",
                    "-q:v", "3",
                    str(out),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=45,
            )
            if out.exists() and out.stat().st_size > 1024:
                frames.append(out)
        except Exception:
            pass
    return frames


def image_to_video(path: Path) -> Path:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg is required to convert still images into TikTok video posts."
        )
    out = cache_name(path, ".mp4", "still_6_seconds")
    if out.exists() and out.stat().st_size > 4096:
        return out

    vf = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
        "format=yuv420p"
    )
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-i", str(path),
            "-t", "6", "-r", "30",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    return out


def ollama_model_names() -> list[str]:
    if ollama is None:
        return []
    try:
        result = ollama.list()
        models = (
            result.get("models", [])
            if isinstance(result, dict)
            else getattr(result, "models", [])
        )
        names = []
        for model in models:
            if isinstance(model, dict):
                name = model.get("model") or model.get("name") or ""
            else:
                name = (
                    getattr(model, "model", "")
                    or getattr(model, "name", "")
                )
            if name:
                names.append(str(name))
        return names
    except Exception:
        return []


def vision_model() -> str | None:
    global VISION_MODEL_CACHE
    if OLLAMA_VISION_MODEL:
        return OLLAMA_VISION_MODEL
    if VISION_MODEL_CACHE is not False:
        return (
            VISION_MODEL_CACHE
            if isinstance(VISION_MODEL_CACHE, str)
            else None
        )

    names = ollama_model_names()
    hints = (
        "qwen3-vl",
        "qwen2.5vl",
        "qwen2-vl",
        "gemma3",
        "llava",
        "minicpm-v",
        "moondream",
        "bakllava",
    )
    for hint in hints:
        for name in names:
            if hint in name.lower():
                VISION_MODEL_CACHE = name
                add_activity(
                    f"Auto-selected Ollama vision model: {name}",
                    "good",
                )
                return name
    VISION_MODEL_CACHE = None
    return None


def sidecar_context(folder: Path, source_username: str = "") -> str:
    pieces = []
    try:
        candidates = sorted(folder.glob("*.txt"))[:5]
    except Exception:
        candidates = []

    for path in candidates:
        try:
            value = path.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
            if value:
                pieces.append(value[:3000])
        except Exception:
            pass

    text = "\n".join(pieces)
    if source_username:
        text = re.sub(
            rf"@?{re.escape(source_username)}\b",
            "",
            text,
            flags=re.I,
        )
    text = re.sub(r"(?<!\w)@[A-Za-z0-9._]{2,}", "", text)
    return re.sub(r"\s+", " ", text).strip()[:4000]


def analyze_media_sequence(
    media_path: Path,
    settings: dict[str, Any],
    sidecar: str = "",
) -> dict[str, Any]:
    model = vision_model()
    is_video = media_path.suffix.lower() in SUPPORTED_VIDEOS
    frames = (
        extract_video_frames(media_path, int(settings["video_frames"]))
        if is_video
        else [media_path]
    )

    if (
        is_video
        and settings["require_video_vision"]
        and (not model or len(frames) < 3)
    ):
        return {
            "ok": False,
            "reason": (
                f"video vision required but model={model or 'none'} "
                f"and frames={len(frames)}"
            ),
            "model": model,
            "frames": len(frames),
        }

    perspective = "UNKNOWN"
    visual = ""

    if model and frames:
        if is_video:
            prompt = """
These are chronological frames sampled across ONE TikTok video.
Watch the sequence by comparing ALL frames from earliest to latest.

Return exactly:
PERSPECTIVE: POV_FIRST_PERSON | SELFIE_VLOG | THIRD_PERSON | UNKNOWN
SEQUENCE: <3-7 concise sentences describing what changes/happens over time>
NARRATION_NOTES: <short notes useful for a first-person caption>

Rules:
- Use only visible evidence.
- Notice actions, movement, setting changes, objects and readable text.
- If it is POV/selfie/vlog footage, make that explicit.
- Do not identify people by name or infer private traits.
- Do not mention usernames, source accounts, filenames, reposting or archive metadata.
- Do not introduce trading or another topic unless it is actually visible.
""".strip()
        else:
            prompt = """
Analyze this image for a TikTok caption.

Return exactly:
PERSPECTIVE: SELFIE_VLOG | THIRD_PERSON | UNKNOWN
SEQUENCE: <1-3 concise sentences about what is visibly happening>
NARRATION_NOTES: <short notes useful for a first-person caption>

Do not identify people by name, infer private traits, mention usernames/files,
or invent a topic not visible in the image.
""".strip()

        try:
            response = ollama.chat(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [str(frame) for frame in frames],
                    }
                ],
                options={"temperature": 0.12, "top_p": 0.8},
            )
            visual = str(
                response.get("message", {}).get("content", "")
                if isinstance(response, dict)
                else getattr(
                    getattr(response, "message", None),
                    "content",
                    "",
                )
            ).strip()
            match = re.search(
                r"PERSPECTIVE:\s*"
                r"(POV_FIRST_PERSON|SELFIE_VLOG|THIRD_PERSON|UNKNOWN)",
                visual,
                flags=re.I,
            )
            if match:
                perspective = match.group(1).upper()
        except Exception as exc:
            add_activity(
                f"Vision analysis failed for {media_path.name}: "
                f"{str(exc)[:100]}",
                "warn",
            )
            visual = ""

    context_parts = []
    if visual:
        context_parts.append("VISUAL SEQUENCE:\n" + visual)
    if sidecar:
        context_parts.append("SUPPORTING TEXT:\n" + sidecar)
    if not context_parts:
        context_parts.append(
            "No reliable semantic description is available. "
            "Do not invent a topic or identity."
        )

    return {
        "ok": True,
        "context": "\n\n".join(context_parts),
        "perspective": perspective,
        "model": model,
        "frames": len(frames),
    }


def clean_generation(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"^```[A-Za-z]*\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    value = value.strip().strip('"').strip()
    return re.sub(r"\s+", " ", value)


def clip_chars(value: str, limit: int) -> str:
    value = str(value or "").strip()
    if len(value) <= limit:
        return value
    cut = value[:limit].rstrip()
    for sep in (". ", "! ", "? "):
        pos = cut.rfind(sep)
        if pos >= max(20, int(limit * 0.55)):
            return cut[: pos + 1].strip()
    return cut.rstrip(" ,;:-") + "…"


def ollama_text(
    system_prompt: str,
    prompt: str,
    attempts: int = 4,
) -> str | None:
    if ollama is None:
        return None
    for attempt in range(1, attempts + 1):
        repair = ""
        if attempt > 1:
            repair = (
                "\nRewrite from scratch. The prior answer was incomplete, "
                "malformed or ignored the requested constraints."
            )
        try:
            response = ollama.chat(
                model=OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt + repair},
                ],
                options={"temperature": 0.8, "top_p": 0.9},
            )
            value = clean_generation(
                response.get("message", {}).get("content", "")
                if isinstance(response, dict)
                else getattr(
                    getattr(response, "message", None),
                    "content",
                    "",
                )
            )
            if not value:
                continue
            lower = value.lower()
            if any(
                token in lower
                for token in ("as an ai", "ollama", "language model")
            ):
                continue
            if value[-1] not in ".!?…)]}" and len(value.split()) > 5:
                continue
            return value
        except Exception:
            continue
    return None


def generate_caption_and_tags(
    account_id: str,
    media_context: dict[str, Any],
) -> tuple[str, list[str]] | None:
    settings = get_settings(account_id)
    perspective = media_context["perspective"]
    context = media_context["context"]

    if perspective in {"POV_FIRST_PERSON", "SELFIE_VLOG"}:
        perspective_rule = (
            "This is POV/selfie/vlog footage. Narrate it naturally in FIRST "
            "PERSON as the fictional filmer/account voice: I, I'm, my, we, "
            "what I'm seeing/doing. Do not describe the filmer from outside."
        )
    elif perspective == "THIRD_PERSON":
        perspective_rule = (
            "Use first-person account voice presenting/reacting to the scene, "
            "but do not falsely claim to literally be an identifiable person shown."
        )
    else:
        perspective_rule = (
            "Use first-person account voice without inventing who people are."
        )

    caption_prompt = f"""
Write ONE TikTok caption for this exact media.

MEDIA ANALYSIS:
{context}

PERSPECTIVE:
{perspective}
{perspective_rule}

ACCOUNT-SPECIFIC CAPTION INSTRUCTIONS:
{settings['caption_prompt']}

Requirements:
- Narrate the sequence that actually happens in the video.
- Never mention original/source usernames, filenames, reposting or archive folders.
- Never force HFT/trading or another stock topic into unrelated footage.
- Smart, natural, specific and coherent.
- NO hashtags in this output.
- Keep under {settings['caption_char_limit']} characters.
- Complete sentence(s).
Output only the caption.
""".strip()

    caption = ollama_text(
        settings["persona_prompt"],
        caption_prompt,
        attempts=4,
    )
    if not caption:
        return None

    hashtag_prompt = f"""
Generate EXACTLY 5 TikTok hashtags based only on this actual media analysis:

{context}

Rules:
- exactly 5
- directly relevant to the visible content
- searchable
- no usernames
- no HFT/trading unless the media itself visibly concerns it
- output only 5 space-separated hashtags
""".strip()

    raw_tags = ollama_text(
        settings["persona_prompt"],
        hashtag_prompt,
        attempts=4,
    ) or ""

    tags = []
    for token in raw_tags.replace("\n", " ").split():
        token = token.strip(" ,.;:!?")
        if token and not token.startswith("#"):
            token = "#" + token
        if re.fullmatch(r"#[A-Za-z0-9_]+", token) and token not in tags:
            tags.append(token)
        if len(tags) == 5:
            break

    if len(tags) < 5:
        for fallback in ("#video", "#fyp", "#creator", "#daily", "#explore"):
            if fallback not in tags:
                tags.append(fallback)
            if len(tags) == 5:
                break

    tag_line = " ".join(tags[:5])
    caption_room = max(
        30,
        int(settings["caption_char_limit"]) - len(tag_line) - 1,
    )
    caption = clip_chars(caption, caption_room)
    return caption, tags[:5]


# =============================================================================
# Media pool
# =============================================================================

def discover_media_folders() -> list[dict[str, Any]]:
    if not MEDIA_ROOT.exists():
        return []

    folders = []
    for folder in MEDIA_ROOT.iterdir():
        try:
            if not folder.is_dir():
                continue
            media = [
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in SUPPORTED_MEDIA
            ]
            if not media:
                continue
            folders.append(
                {
                    "id": folder.name,
                    "path": folder,
                    "media": sorted(media),
                }
            )
        except OSError:
            continue
    return folders


def choose_unused_folder(
    account_id: str,
    folders: list[dict[str, Any]],
) -> dict[str, Any] | None:
    history = load_history(account_id)
    used = set(history["posted_ids"])
    available = [f for f in folders if f["id"] not in used]
    return random.choice(available) if available else None


# =============================================================================
# TikTok actions
# =============================================================================

def prepare_caption_editor(page, full_caption: str) -> None:
    selectors = [
        "div[class*='public-DraftEditor-content']",
        "div[contenteditable='true']",
        "textarea",
    ]
    editor = None
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible(timeout=1200):
                editor = loc
                break
        except Exception:
            pass

    if editor is None:
        raise RuntimeError("Caption editor was not found.")

    try:
        editor.click(timeout=2500)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        editor.fill(full_caption)
    except Exception:
        editor.click(timeout=2500)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        editor.press_sequentially(full_caption, delay=2)


def execute_upload(
    page,
    account_id: str,
    history: dict[str, Any],
) -> int:
    account = find_account(account_id)
    settings = get_settings(account_id)
    folders = discover_media_folders()
    selected = choose_unused_folder(account_id, folders)
    if not selected:
        add_activity(
            f"{account['username']}: no unused media folders remain",
            "warn",
        )
        return 0

    # Prefer an actual video; otherwise convert first image to a 6-second MP4.
    video = next(
        (p for p in selected["media"] if p.suffix.lower() in SUPPORTED_VIDEOS),
        None,
    )
    source_media = video or selected["media"][0]
    upload_path = (
        source_media
        if source_media.suffix.lower() in SUPPORTED_VIDEOS
        else image_to_video(source_media)
    )

    sidecar = sidecar_context(selected["path"])
    analysis = analyze_media_sequence(
        source_media,
        settings,
        sidecar=sidecar,
    )
    if not analysis["ok"]:
        add_activity(
            f"{account['username']}: skipped {selected['id']}: "
            f"{analysis['reason']}",
            "warn",
        )
        return 0

    add_activity(
        f"{account['username']}: 👁️ watched media using "
        f"{analysis['model'] or 'no vision model'}; "
        f"frames={analysis['frames']}, "
        f"perspective={analysis['perspective']}",
        "info",
    )

    generated = generate_caption_and_tags(account_id, analysis)
    if not generated:
        add_activity(
            f"{account['username']}: caption generation failed; upload skipped",
            "warn",
        )
        return 0

    caption, tags = generated
    full_caption = f"{caption}\n{' '.join(tags)}".strip()

    add_activity(
        f"{account['username']}: caption ready "
        f"({len(full_caption)} chars, exactly 5 hashtags): "
        f"{caption[:100]}",
        "info",
    )

    if not wait_for_write_slot(account_id, "upload", max_wait=180):
        return 0

    goto(page, TIKTOK_UPLOAD, 3000)
    if explicit_login_button_visible(page) or "/login" in page.url.lower():
        raise RuntimeError("TikTok login/session expired.")

    file_input = page.locator("input[type='file']").first
    if not file_input.count():
        raise RuntimeError("TikTok Studio file input was not found.")

    file_input.set_input_files(str(upload_path))
    page.wait_for_timeout(6000)
    prepare_caption_editor(page, full_caption)

    post = page.get_by_role(
        "button",
        name=re.compile(r"^Post$", re.I),
    ).first
    if not post.count():
        post = page.locator("button:has-text('Post')").first
    if not post.count():
        raise RuntimeError("Post button was not found.")

    deadline = time.time() + 75
    while time.time() < deadline:
        try:
            if post.is_enabled(timeout=1000):
                break
        except Exception:
            pass
        page.wait_for_timeout(1000)

    if not post.is_enabled(timeout=1000):
        raise RuntimeError("Post button never became enabled.")

    pre_url = page.url
    post.click(timeout=5000)
    record_write(account_id, "upload")

    confirmed = False
    final_url = ""
    success_patterns = (
        r"uploaded",
        r"posted",
        r"processing",
        r"your video",
        r"manage posts",
    )
    deadline = time.time() + 55
    while time.time() < deadline:
        page.wait_for_timeout(1200)
        current_url = page.url
        if current_url != pre_url and "/upload" not in current_url.lower():
            confirmed = True
            final_url = current_url
            break
        try:
            body = page.locator("body").inner_text(timeout=1500).lower()
        except Exception:
            body = ""
        if any(re.search(p, body, re.I) for p in success_patterns):
            confirmed = True
            final_url = current_url
            break
        try:
            if not file_input.is_visible(timeout=500):
                confirmed = True
                final_url = current_url
                break
        except Exception:
            pass

    if not confirmed:
        raise RuntimeError(
            "Post clicked, but TikTok upload success was not confirmed."
        )

    history["posted_ids"].append(selected["id"])
    record_recent_post(
        account_id,
        selected["path"],
        full_caption,
        final_url if "/video/" in final_url else "",
    )
    add_activity(
        f"{account['username']}: ✅ uploaded from folder {selected['id']}",
        "good",
    )
    return 1


def looks_like_ad(text: str) -> bool:
    lower = str(text or "").lower()
    phrases = (
        "promote your", "promotion", "paid promo", "brand ambassador",
        "collaboration opportunity", "grow your account", "more followers",
        "buy followers", "marketing agency", "investment opportunity",
        "forex signals", "crypto signals", "guaranteed returns",
        "shop now", "buy now", "dm us for", "our service", "special offer",
    )
    return any(p in lower for p in phrases)


def normalize_reply(value: str) -> str:
    value = str(value or "").lower()
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"@\w[\w.]*", "", value)
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def reply_too_similar(
    candidate: str,
    prior: list[str],
    threshold: float = 0.76,
) -> bool:
    norm = normalize_reply(candidate)
    if not norm:
        return True
    for old in prior[-12:]:
        prev = normalize_reply(old)
        if not prev:
            continue
        if norm == prev:
            return True
        if difflib.SequenceMatcher(None, norm, prev).ratio() >= threshold:
            return True
        if len(norm.split()) >= 6 and len(prev.split()) >= 6:
            if norm.split()[:5] == prev.split()[:5]:
                return True
    return False


def generate_reply(
    account_id: str,
    context_type: str,
    incoming: str,
    conversation: str = "",
    prior: list[str] | None = None,
) -> str | None:
    settings = get_settings(account_id)
    prior = list(prior or [])

    if context_type == "dm" and looks_like_ad(incoming):
        options = [
            "Not interested. Take the sales pitch somewhere else.",
            "No thanks. This inbox is for conversations, not cold sales scripts.",
            "Hard pass. Try a more optimistic inbox.",
        ]
        return clip_chars(random.choice(options), settings["reply_char_limit"])

    extra = (
        settings["dm_prompt"]
        if context_type == "dm"
        else settings["comment_prompt"]
    )

    avoid = "\n".join(f"- {x}" for x in prior[-8:]) or "(none)"

    for attempt in range(1, 5):
        prompt = f"""
Write ONE TikTok {context_type} reply.

MOST RECENT MESSAGE:
{incoming!r}

RECENT CONVERSATION:
{conversation or "(none)"}

ACCOUNT-SPECIFIC INSTRUCTIONS:
{extra}

RECENT REPLIES TO AVOID COPYING:
{avoid}

Rules:
- Respond to what was actually said.
- Match the person's tone/effort.
- Do not inject trading/HFT or another unrelated topic.
- Do not repeat the same opening, joke, punchline or sentence pattern.
- If obvious advertising/solicitation, say you're not interested and dismiss it.
- Keep under {settings['reply_char_limit']} characters.
- Complete sentence(s).
- Output only the reply.
Attempt {attempt}/4.
""".strip()
        reply = ollama_text(
            settings["persona_prompt"],
            prompt,
            attempts=2,
        )
        if reply:
            reply = clip_chars(reply, settings["reply_char_limit"])
            if not reply_too_similar(reply, prior):
                return reply
    return None


def handle_dms(
    page,
    account_id: str,
    history: dict[str, Any],
) -> int:
    account = find_account(account_id)
    settings = get_settings(account_id)
    if not settings["enable_dm_replies"]:
        return 0

    sent = 0
    goto(page, "https://www.tiktok.com/messages", 2200)

    selectors = [
        "[data-e2e='chat-list-item']",
        "[class*='MessageItem']",
    ]
    threads = None
    for selector in selectors:
        loc = page.locator(selector)
        try:
            if loc.count():
                threads = loc
                break
        except Exception:
            pass
    if threads is None:
        return 0

    for index in range(min(4, threads.count())):
        if sent >= min(2, int(settings["max_writes_per_workflow"])):
            break

        try:
            threads.nth(index).click(timeout=3000)
            page.wait_for_timeout(1000)
        except Exception:
            continue

        bubbles = page.locator(
            "[data-e2e='chat-message'], [class*='ChatBubble']"
        )
        count = bubbles.count()
        if not count:
            continue

        texts = []
        for i in range(max(0, count - 8), count):
            try:
                value = bubbles.nth(i).inner_text(timeout=1200).strip()
                if value:
                    texts.append(value)
            except Exception:
                pass
        if not texts:
            continue

        latest = texts[-1]
        # Prevent replying to the bot's own last reply when DOM ownership
        # selectors are unavailable.
        if any(
            normalize_reply(latest) == normalize_reply(old)
            for old in history["sent_dm_texts"][-30:]
        ):
            continue

        incoming_key = stable_key(
            "dm",
            page.url,
            latest,
        )
        if incoming_key in history["replied_dms"]:
            continue

        thread_key = stable_key("thread", page.url or str(index))
        prior = history["dm_reply_memory"].get(thread_key, [])
        conversation = "\n".join(texts[-8:])

        reply = generate_reply(
            account_id,
            "dm",
            latest,
            conversation=conversation,
            prior=prior,
        )
        if not reply:
            continue

        if not wait_for_write_slot(account_id, "dm"):
            break

        editor = page.locator("div[contenteditable='true']").last
        if not editor.count():
            continue
        editor.fill(reply)
        page.keyboard.press("Enter")
        record_write(account_id, "dm")

        history["replied_dms"].append(incoming_key)
        history["replied_dms"] = history["replied_dms"][-5000:]
        history["sent_dm_texts"].append(reply)
        history["sent_dm_texts"] = history["sent_dm_texts"][-200:]
        prior.append(reply)
        history["dm_reply_memory"][thread_key] = prior[-20:]

        sent += 1
        add_activity(
            f"{account['username']}: replied to DM: {reply[:110]}",
            "good",
        )

    return sent


def handle_comments(
    page,
    account_id: str,
    history: dict[str, Any],
) -> int:
    account = find_account(account_id)
    settings = get_settings(account_id)
    if not settings["enable_comment_replies"]:
        return 0

    username = account["username"].lstrip("@")
    goto(page, f"https://www.tiktok.com/@{username}", 2200)
    first_video = page.locator(
        "[data-e2e='user-post-item'], a[href*='/video/']"
    ).first
    if not first_video.count():
        return 0
    first_video.click(timeout=3000)
    page.wait_for_timeout(1600)

    comments = page.locator("[data-e2e='comment-item']")
    sent = 0
    for index in range(min(5, comments.count())):
        if sent >= min(2, int(settings["max_writes_per_workflow"])):
            break
        container = comments.nth(index)
        try:
            text = container.locator(
                "[data-e2e='comment-text']"
            ).first.inner_text(timeout=1800).strip()
            user = container.locator(
                "[data-e2e='comment-username']"
            ).first.inner_text(timeout=1800).strip()
        except Exception:
            continue

        key = stable_key("comment", user, text)
        if not text or key in history["replied_comments"]:
            continue

        reply = generate_reply(
            account_id,
            "comment",
            text,
            prior=[],
        )
        if not reply:
            continue
        if not wait_for_write_slot(account_id, "comment"):
            break

        button = container.get_by_text("Reply", exact=True).first
        if not button.count():
            continue
        button.click(timeout=2500)
        page.wait_for_timeout(400)

        editor = page.locator("div[contenteditable='true']").last
        if not editor.count():
            continue
        editor.fill(reply)

        post = page.locator("[data-e2e='comment-post-button']").first
        if post.count():
            post.click(timeout=2500)
        else:
            page.keyboard.press("Enter")

        record_write(account_id, "comment")
        history["replied_comments"].append(key)
        history["replied_comments"] = history["replied_comments"][-5000:]
        sent += 1
        add_activity(
            f"{account['username']}: replied to @{user}: {reply[:110]}",
            "good",
        )
    return sent


def engage_hashtag(
    page,
    account_id: str,
    history: dict[str, Any],
) -> int:
    account = find_account(account_id)
    settings = get_settings(account_id)
    tags = settings["target_hashtags"]
    if not tags:
        add_activity(
            f"{account['username']}: no target hashtags configured",
            "warn",
        )
        return 0

    tag = random.choice(tags).lstrip("#")
    goto(page, f"https://www.tiktok.com/tag/{tag}", 2600)
    first = page.locator(
        "[data-e2e='challenge-item'], a[href*='/video/']"
    ).first
    if not first.count():
        return 0
    first.click(timeout=3000)
    page.wait_for_timeout(1500)

    actions = 0
    max_actions = int(settings["max_writes_per_workflow"])

    for _ in range(3):
        if actions >= max_actions:
            break
        key = stable_key("video", page.url)
        like = page.locator("[data-e2e='browse-like']").first
        try:
            if (
                key not in history["liked_videos"]
                and like.count()
                and like.is_visible(timeout=1200)
            ):
                if not wait_for_write_slot(account_id, "like"):
                    break
                like.click(timeout=2500)
                record_write(account_id, "like")
                history["liked_videos"].append(key)
                actions += 1
                add_activity(
                    f"{account['username']}: liked a #{tag} video",
                    "good",
                )
        except Exception:
            pass
        try:
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(random.randint(3500, 6500))
        except Exception:
            break
    return actions


def follow_target_network(
    page,
    account_id: str,
    history: dict[str, Any],
) -> int:
    """
    Follow a limited number of users visible in the Followers/Following list
    of one configured target account.
    """
    account = find_account(account_id)
    settings = get_settings(account_id)
    targets = settings["target_accounts"]

    if not targets:
        add_activity(
            f"{account['username']}: no target accounts configured",
            "warn",
        )
        return 0

    target = random.choice(targets).lstrip("@")
    source = settings["follow_source"]
    if source == "both":
        source = random.choice(["followers", "following"])

    goto(page, f"https://www.tiktok.com/@{target}", 2500)

    source_selector = (
        "[data-e2e='followers-count']"
        if source == "followers"
        else "[data-e2e='following-count']"
    )
    trigger = page.locator(source_selector).first
    if not trigger.count():
        # Text fallback for frontend variants.
        trigger = page.get_by_text(
            re.compile(
                r"\bFollowers\b" if source == "followers" else r"\bFollowing\b",
                re.I,
            )
        ).first

    if not trigger.count():
        raise RuntimeError(
            f"Could not open @{target}'s {source} list."
        )

    trigger.click(timeout=3000)
    page.wait_for_timeout(1800)

    dialog = page.locator("[role='dialog']").last
    root = dialog if dialog.count() else page
    buttons = root.get_by_role(
        "button",
        name=re.compile(r"^Follow$", re.I),
    )

    followed = 0
    max_follow = min(
        int(settings["follow_limit"]),
        int(settings["max_writes_per_workflow"]),
    )

    # Re-query after clicks because TikTok can mutate the modal DOM.
    index = 0
    while followed < max_follow and index < 30:
        buttons = root.get_by_role(
            "button",
            name=re.compile(r"^Follow$", re.I),
        )
        if not buttons.count():
            break
        if index >= buttons.count():
            try:
                root.evaluate("(el) => el.scrollBy(0, 650)")
                page.wait_for_timeout(1000)
                index = 0
                continue
            except Exception:
                break

        button = buttons.nth(index)
        parent_text = ""
        try:
            parent_text = button.locator("xpath=..").inner_text(timeout=1000)
        except Exception:
            pass
        candidate = re.search(r"@([A-Za-z0-9._]+)", parent_text)
        candidate_name = candidate.group(1) if candidate else f"candidate_{index}"

        key = stable_key("follow", target, source, candidate_name)
        if key in history["followed_users"]:
            index += 1
            continue

        if not wait_for_write_slot(account_id, "follow"):
            break

        try:
            button.click(timeout=2500)
            record_write(account_id, "follow")
            history["followed_users"].append(key)
            followed += 1
            add_activity(
                f"{account['username']}: followed @{candidate_name} "
                f"from @{target}'s {source}",
                "good",
            )
            page.wait_for_timeout(700)
        except Exception:
            index += 1

    return followed


# =============================================================================
# Profile metrics
# =============================================================================

def parse_number(value: str) -> int | None:
    if not value:
        return None
    text = value.strip().upper().replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)([KMB]?)", text)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
    }[match.group(2)]
    return int(number * multiplier)


def collect_metrics(page, account_id: str) -> None:
    account = find_account(account_id)
    if not account:
        return
    username = account["username"].lstrip("@")
    goto(page, f"https://www.tiktok.com/@{username}", 2200)

    values = {}
    selectors = {
        "followers": "[data-e2e='followers-count']",
        "following": "[data-e2e='following-count']",
        "likes": "[data-e2e='likes-count']",
    }
    for key, selector in selectors.items():
        try:
            loc = page.locator(selector).first
            values[key] = (
                parse_number(loc.inner_text(timeout=1500))
                if loc.count()
                else None
            )
        except Exception:
            values[key] = None

    try:
        videos = page.locator(
            "[data-e2e='user-post-item'], a[href*='/video/']"
        ).count()
    except Exception:
        videos = None

    update_account(
        account_id,
        followers=values["followers"],
        following=values["following"],
        likes=values["likes"],
        videos=videos,
        last_checked=now_iso(),
    )


# =============================================================================
# Automation execution
# =============================================================================

def run_account_once(
    account_id: str,
    forced_task: str | None = None,
) -> None:
    account = find_account(account_id)
    if not account:
        return

    if account_id not in CONNECTED_ACCOUNTS:
        add_activity(
            f"{account['username']}: skipped; account is disconnected",
            "warn",
        )
        return

    lock = account_lock(account_id)
    if not lock.acquire(blocking=False):
        add_activity(
            f"{account['username']}: profile is already busy",
            "warn",
        )
        return

    settings = get_settings(account_id)
    history = load_history(account_id)

    try:
        cooldown = ACCOUNT_COOLDOWNS.get(account_id)
        if cooldown and datetime.now() < cooldown:
            update_account(
                account_id,
                status="Cooldown",
                cooldown_until=cooldown.isoformat(timespec="seconds"),
            )
            return
        ACCOUNT_COOLDOWNS.pop(account_id, None)

        update_account(
            account_id,
            status=f"Running: {settings['mode']}",
            last_error=None,
            cooldown_until=None,
        )

        with sync_playwright() as p:
            context = launch_profile(
                p,
                account_id,
                headless=bool(settings["headless_automation"]),
            )
            page = context.pages[0] if context.pages else context.new_page()

            ok, reason = verify_saved_login(page, account)
            if not ok:
                CONNECTED_ACCOUNTS.discard(account_id)
                AUTO_ENABLED_ACCOUNTS.discard(account_id)
                update_account(
                    account_id,
                    status="Login needed",
                    connected=False,
                    auto_enabled=False,
                    last_error=reason,
                )
                add_activity(
                    f"{account['username']}: automation stopped: {reason}",
                    "warn",
                )
                context.close()
                return

            mode = settings["mode"]
            task = forced_task or NEXT_TASK_OVERRIDE.pop(account_id, None)
            if not task:
                if mode == "manual":
                    task = "none"
                elif mode == "upload_only":
                    task = "upload"
                elif mode == "follow_only":
                    task = "follow"
                elif mode == "engage_only":
                    task = "engage"
                elif mode == "dm_only":
                    task = "dm"
                else:
                    choices = ["upload", "follow", "engage"]
                    task = random.choice(choices)

            dm_count = 0
            comment_count = 0
            post_count = 0
            like_count = 0
            follow_count = 0

            if settings["enable_dm_replies"] and mode not in {"manual"}:
                dm_count = handle_dms(page, account_id, history)

            if (
                settings["enable_comment_replies"]
                and mode in {"balanced", "engage_only"}
            ):
                comment_count = handle_comments(page, account_id, history)

            if task == "upload":
                post_count = execute_upload(page, account_id, history)
            elif task == "follow":
                follow_count = follow_target_network(
                    page, account_id, history
                )
            elif task == "engage":
                like_count = engage_hashtag(
                    page, account_id, history
                )
            elif task == "comments":
                comment_count += handle_comments(
                    page, account_id, history
                )
            elif task == "dm":
                dm_count += handle_dms(
                    page, account_id, history
                )

            save_history(account_id, history)
            current = load_analytics()["accounts"].get(account_id, {})
            update_account(
                account_id,
                status=f"Idle: {mode}",
                connected=True,
                posts_sent=int(current.get("posts_sent") or 0) + post_count,
                likes_sent=int(current.get("likes_sent") or 0) + like_count,
                follows_sent=int(current.get("follows_sent") or 0) + follow_count,
                dm_replies=int(current.get("dm_replies") or 0) + dm_count,
                comment_replies=(
                    int(current.get("comment_replies") or 0) + comment_count
                ),
                last_action=task,
                last_checked=now_iso(),
            )

            try:
                collect_metrics(page, account_id)
            except Exception:
                pass

            context.close()

    except Exception as exc:
        cooldown = datetime.now() + timedelta(minutes=30)
        ACCOUNT_COOLDOWNS[account_id] = cooldown
        update_account(
            account_id,
            status="Error cooldown",
            last_error=str(exc),
            cooldown_until=cooldown.isoformat(timespec="seconds"),
        )
        add_activity(
            f"{account['username']}: automation error: {exc}",
            "error",
        )
        traceback.print_exc()
    finally:
        lock.release()


def start_manual_action(account_id: str, task: str) -> None:
    allowed = {"upload", "follow", "engage", "comments", "dm"}
    if task not in allowed:
        raise ValueError("Unsupported task.")
    if account_id not in CONNECTED_ACCOUNTS:
        raise RuntimeError("Quick Login or Save Login first.")
    thread = threading.Thread(
        target=run_account_once,
        args=(account_id, task),
        daemon=True,
        name=f"tiktok-{task}-{account_id}",
    )
    thread.start()


def enable_auto(account_id: str, enabled: bool) -> None:
    account = find_account(account_id)
    if not account:
        raise ValueError("Unknown account.")
    if enabled and account_id not in CONNECTED_ACCOUNTS:
        raise RuntimeError("Connect the account first.")
    if enabled:
        AUTO_ENABLED_ACCOUNTS.add(account_id)
        NEXT_DUE[account_id] = time.monotonic() + random.randint(5, 15)
        update_account(
            account_id,
            auto_enabled=True,
            status=f"Auto: {get_settings(account_id)['mode']}",
        )
        add_activity(
            f"{account['username']}: automation enabled",
            "good",
        )
    else:
        AUTO_ENABLED_ACCOUNTS.discard(account_id)
        update_account(
            account_id,
            auto_enabled=False,
            status=(
                "Connected"
                if account_id in CONNECTED_ACCOUNTS
                else "Disconnected"
            ),
        )
        add_activity(
            f"{account['username']}: automation disabled",
            "warn",
        )


def scheduler_loop() -> None:
    add_activity(
        "TikTok scheduler started; accounts are serialized to reduce bursts",
        "good",
    )
    while not AUTOMATION_STOP.wait(2):
        due_accounts = [
            aid
            for aid in list(AUTO_ENABLED_ACCOUNTS)
            if aid in CONNECTED_ACCOUNTS
            and time.monotonic() >= NEXT_DUE.get(aid, 0.0)
        ]
        if not due_accounts:
            continue

        # Intentionally run one account at a time.
        account_id = random.choice(due_accounts)
        settings = get_settings(account_id)
        run_account_once(account_id)

        delay = random.randint(
            int(settings["workflow_min_seconds"]),
            int(settings["workflow_max_seconds"]),
        )
        NEXT_DUE[account_id] = time.monotonic() + delay
        account = find_account(account_id)
        add_activity(
            f"{account['username']}: next automatic pass in "
            f"{delay // 60}m {delay % 60}s",
            "info",
        )


def start_scheduler() -> None:
    global AUTOMATION_THREAD
    if AUTOMATION_THREAD and AUTOMATION_THREAD.is_alive():
        return
    AUTOMATION_STOP.clear()
    AUTOMATION_THREAD = threading.Thread(
        target=scheduler_loop,
        daemon=True,
        name="tiktok-control-scheduler",
    )
    AUTOMATION_THREAD.start()


# =============================================================================
# Local folder opening
# =============================================================================

def open_media_folder(folder_value: str) -> dict[str, Any]:
    folder = Path(str(folder_value or "")).expanduser()
    resolved = folder.resolve()
    root = MEDIA_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Folder is outside the configured media root.")
    if not resolved.exists() or not resolved.is_dir():
        raise FileNotFoundError(f"Folder no longer exists: {resolved}")

    if os.name == "nt":
        os.startfile(str(resolved))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(resolved)])
    else:
        subprocess.Popen(["xdg-open", str(resolved)])
    return {"ok": True, "folder": str(resolved)}


# =============================================================================
# API / dashboard
# =============================================================================

def response_json(
    handler: BaseHTTPRequestHandler,
    payload: Any,
    status: int = 200,
) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header(
        "Content-Type", "application/json; charset=utf-8"
    )
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length > 128_000:
        raise ValueError("Request too large.")
    if not length:
        return {}
    value = json.loads(handler.rfile.read(length).decode("utf-8"))
    return value if isinstance(value, dict) else {}


def metrics_payload() -> dict[str, Any]:
    data = load_analytics()
    accounts_payload = {}
    for account in ACCOUNTS:
        aid = account["id"]
        row = dict(
            data["accounts"].get(
                aid, default_account_analytics(account)
            )
        )
        settings = get_settings(aid)
        row["username"] = account["username"]
        row["connected"] = aid in CONNECTED_ACCOUNTS
        row["auto_enabled"] = aid in AUTO_ENABLED_ACCOUNTS
        row["settings"] = settings
        row["profile_exists"] = profile_path(aid).exists()
        row["login_browser_open"] = bool(
            LOGIN_THREADS.get(aid)
            and LOGIN_THREADS[aid].is_alive()
        )
        row["write_budget_used"] = len(_prune_write_window(aid))
        row["write_budget_max"] = settings["max_writes_per_window"]
        accounts_payload[aid] = row

    return {
        "updated_at": data.get("updated_at"),
        "accounts": accounts_payload,
        "activity": data.get("activity", [])[:150],
        "recent_posts": load_recent_posts()[:30],
        "engine": {
            "media_root": str(MEDIA_ROOT),
            "ollama_model": OLLAMA_MODEL,
            "vision_model": vision_model() or "",
            "ffmpeg": find_ffmpeg() or "",
        },
    }


HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TikTok Control Head v3</title>
<style>
:root{color-scheme:dark;--bg:#0b0d12;--panel:#131722;--panel2:#0d1018;--border:#293043;--text:#edf1f7;--muted:#8e99ad;--ok:#6ee7b7;--warn:#facc6b;--bad:#fb7185}
*{box-sizing:border-box}body{margin:0;padding:18px;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:14px}h1{font-size:21px;margin:0}.small{font-size:11px;color:var(--muted)}
.toolbar,.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:15px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:11px;padding:14px}.cardhead{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
h2{margin:0;font-size:17px}.badge{font-size:10px;padding:4px 7px;border-radius:999px;background:#252b3d}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:10px 0}.stat{background:var(--panel2);padding:8px;border-radius:7px;text-align:center}.stat b{display:block}.stat span{font-size:10px;color:var(--muted)}
.section{border-top:1px solid var(--border);margin-top:11px;padding-top:10px}.fieldgrid{display:grid;grid-template-columns:1fr 1fr;gap:7px}
label{display:block;font-size:11px;color:var(--muted);margin:6px 0 3px}input,textarea,select{width:100%;background:#0c0f16;color:var(--text);border:1px solid var(--border);border-radius:7px;padding:8px;font:inherit}textarea{min-height:72px;resize:vertical}
button{background:#282e40;color:var(--text);border:1px solid var(--border);border-radius:7px;padding:8px 10px;cursor:pointer}button:hover{background:#353d55}button:disabled{opacity:.45;cursor:not-allowed}.goodbtn{border-color:#346b5a}.danger{border-color:#713846}.warnbtn{border-color:#76612e}
.logs{height:260px;overflow:auto;background:#0c0f16;border-radius:7px;padding:8px;font:11px ui-monospace,Consolas,monospace}.logrow{padding:4px 0;border-bottom:1px solid #1b2030}
.notice{padding:10px;background:#111521;border:1px solid var(--border);border-radius:8px;margin-bottom:14px;color:var(--muted);font-size:12px}.postrow{padding:9px 0;border-bottom:1px solid var(--border)}
.toast{position:fixed;right:14px;bottom:14px;background:#23293a;border:1px solid var(--border);padding:10px 12px;border-radius:8px;display:none;z-index:99;max-width:450px}
@media(max-width:650px){.fieldgrid{grid-template-columns:1fr}.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div>
    <h1>🎛️ TikTok Control Head v3</h1>
    <div class="small">Boots DISCONNECTED. Starting/stopping this program does not log any account in.</div>
  </div>
  <div class="toolbar">
    <label style="margin:0"><input id="autoRefresh" type="checkbox" checked style="width:auto"> Auto Refresh</label>
    <button onclick="refreshNow(true)">Refresh Now</button>
    <button onclick="addAccountPrompt()">Add Account</button>
    <span id="refreshState" class="small"></span>
  </div>
</header>

<div class="notice">
  <b>Login flow:</b> Open Login → finish login/CAPTCHA/challenge yourself in Chromium → press Save Login.
  Future runs can use Quick Login. Disconnect keeps the persistent profile. Clear Saved Login removes TikTok cookies/storage.
  Automation never starts just because the Python process started.
</div>

<div id="accounts" class="grid"></div>

<div class="card" style="margin-top:15px">
  <div class="cardhead"><div><h2>Recent Posts</h2><div class="small">Exact source folder used by each successful upload.</div></div></div>
  <div id="recentPosts" class="section"></div>
</div>

<div class="card" style="margin-top:15px">
  <div class="cardhead"><div><h2>Activity</h2><div id="engineInfo" class="small"></div></div></div>
  <div id="activity" class="logs section"></div>
</div>

<div id="toast" class="toast"></div>

<script>
let editing=false,refreshBusy=false;

function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function toast(msg,bad=false){const t=document.getElementById("toast");t.textContent=msg;t.style.display="block";t.style.color=bad?"var(--bad)":"var(--text)";clearTimeout(window.__tt);window.__tt=setTimeout(()=>t.style.display="none",5000);}
function splitList(v){return String(v||"").split(/[\n,]+/).map(x=>x.trim()).filter(Boolean);}
async function api(action,payload={}){const r=await fetch("/api/control",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,...payload})});const d=await r.json().catch(()=>({ok:false,error:"Invalid response"}));if(!r.ok||!d.ok)throw new Error(d.error||`HTTP ${r.status}`);return d;}

document.addEventListener("focusin",e=>{if(e.target.matches("input,textarea,select"))editing=true});
document.addEventListener("focusout",()=>setTimeout(()=>{editing=!!document.querySelector("input:focus,textarea:focus,select:focus")},0));

function accountCard(id,a){
  const s=a.settings||{};
  const connected=!!a.connected,auto=!!a.auto_enabled,loginOpen=!!a.login_browser_open;
  const status=connected?"CONNECTED":"DISCONNECTED";
  const tags=(s.target_hashtags||[]).join(", ");
  const targets=(s.target_accounts||[]).join(", ");
  return `<div class="card">
    <div class="cardhead">
      <div>
        <h2>${esc(a.username)}</h2>
        <div class="small">${esc(id)} · ${esc(a.status||"")}</div>
      </div>
      <div class="row">
        <span class="badge ${connected?"ok":"bad"}">${status}</span>
        <span class="badge ${auto?"ok":"warn"}">${auto?"AUTO ON":"AUTO OFF"}</span>
        ${loginOpen?'<span class="badge warn">LOGIN WINDOW OPEN</span>':""}
      </div>
    </div>

    <div class="stats">
      <div class="stat"><b>${a.posts_sent||0}</b><span>Posts</span></div>
      <div class="stat"><b>${a.follows_sent||0}</b><span>Follows</span></div>
      <div class="stat"><b>${a.likes_sent||0}</b><span>Likes</span></div>
      <div class="stat"><b>${a.write_budget_used||0}/${a.write_budget_max||0}</b><span>Write budget</span></div>
    </div>

    <div class="section">
      <b>Login / Session</b>
      <div class="row">
        <button class="goodbtn" onclick="act('${id}','quick_login')" ${connected||loginOpen?"disabled":""}>Quick Login</button>
        <button onclick="act('${id}','open_login')" ${loginOpen?"disabled":""}>Open Login</button>
        <button class="goodbtn" onclick="act('${id}','save_login')" ${!loginOpen?"disabled":""}>Save Login</button>
        <button onclick="act('${id}','cancel_login')" ${!loginOpen?"disabled":""}>Close Login</button>
        <button onclick="act('${id}','disconnect')" ${!connected?"disabled":""}>Disconnect</button>
        <button class="danger" onclick="clearLogin('${id}')">Clear Saved Login</button>
      </div>
      ${a.last_error?`<div class="small bad">${esc(a.last_error)}</div>`:""}
    </div>

    <div class="section">
      <b>Automation</b>
      <div class="row">
        <button class="goodbtn" onclick="act('${id}','auto_on')" ${!connected||auto?"disabled":""}>Enable Auto</button>
        <button class="warnbtn" onclick="act('${id}','auto_off')" ${!auto?"disabled":""}>Disable Auto</button>
        <button onclick="task('${id}','upload')" ${!connected?"disabled":""}>Post Once</button>
        <button onclick="task('${id}','follow')" ${!connected?"disabled":""}>Follow Target</button>
        <button onclick="task('${id}','engage')" ${!connected?"disabled":""}>Engage Hashtag</button>
        <button onclick="task('${id}','dm')" ${!connected?"disabled":""}>DM Scan</button>
        <button onclick="task('${id}','comments')" ${!connected?"disabled":""}>Reply Comments</button>
      </div>
    </div>

    <div class="section">
      <b>Behavior / Mode Settings</b>
      <div class="fieldgrid">
        <div><label>Account username</label><input id="name_${id}" value="${esc(a.username)}"></div>
        <div><label>Mode</label><select id="mode_${id}">${["balanced","upload_only","follow_only","engage_only","dm_only","manual"].map(m=>`<option value="${m}" ${s.mode===m?"selected":""}>${m.replaceAll("_"," ")}</option>`).join("")}</select></div>
        <div><label>Follow source</label><select id="fsource_${id}">${["both","followers","following"].map(m=>`<option value="${m}" ${s.follow_source===m?"selected":""}>${m}</option>`).join("")}</select></div>
        <div><label>Follow limit / pass</label><input id="followlim_${id}" type="number" min="1" max="10" value="${s.follow_limit||2}"></div>
        <div><label>Caption char limit</label><input id="caplim_${id}" type="number" min="80" max="2000" value="${s.caption_char_limit||350}"></div>
        <div><label>Reply char limit</label><input id="replim_${id}" type="number" min="20" max="1000" value="${s.reply_char_limit||220}"></div>
        <div><label>Video frames to watch</label><input id="frames_${id}" type="number" min="3" max="9" value="${s.video_frames||5}"></div>
        <div><label>Writes / window</label><input id="maxwrites_${id}" type="number" min="1" max="20" value="${s.max_writes_per_window||6}"></div>
        <div><label>Window seconds</label><input id="window_${id}" type="number" min="60" max="7200" value="${s.write_window_seconds||900}"></div>
        <div><label>Min write gap seconds</label><input id="gap_${id}" type="number" min="10" max="600" value="${s.write_min_gap_seconds||30}"></div>
        <div><label>Upload cooldown seconds</label><input id="uploadgap_${id}" type="number" min="300" max="21600" value="${s.upload_cooldown_seconds||1800}"></div>
        <div><label>Workflow min seconds</label><input id="wmin_${id}" type="number" min="60" max="7200" value="${s.workflow_min_seconds||300}"></div>
        <div><label>Workflow max seconds</label><input id="wmax_${id}" type="number" min="60" max="14400" value="${s.workflow_max_seconds||720}"></div>
      </div>

      <label>Target accounts (comma/newline separated)</label>
      <textarea id="targets_${id}">${esc(targets)}</textarea>
      <label>Target hashtags (comma/newline separated)</label>
      <textarea id="tags_${id}">${esc(tags)}</textarea>

      <label>Persona / attitude prompt</label>
      <textarea id="persona_${id}" style="min-height:130px">${esc(s.persona_prompt||"")}</textarea>
      <label>Caption behavior prompt</label>
      <textarea id="capprompt_${id}">${esc(s.caption_prompt||"")}</textarea>
      <label>DM behavior prompt</label>
      <textarea id="dmprompt_${id}">${esc(s.dm_prompt||"")}</textarea>
      <label>Comment behavior prompt</label>
      <textarea id="commentprompt_${id}">${esc(s.comment_prompt||"")}</textarea>

      <div class="row">
        <label><input id="reqvision_${id}" type="checkbox" style="width:auto" ${s.require_video_vision?"checked":""}> Require real multi-frame vision for videos</label>
        <label><input id="dmen_${id}" type="checkbox" style="width:auto" ${s.enable_dm_replies?"checked":""}> Auto-reply DMs</label>
        <label><input id="commenten_${id}" type="checkbox" style="width:auto" ${s.enable_comment_replies?"checked":""}> Auto-reply comments</label>
        <label><input id="headless_${id}" type="checkbox" style="width:auto" ${s.headless_automation?"checked":""}> Headless automation</label>
      </div>
      <div class="row">
        <button class="goodbtn" onclick="saveSettings('${id}')">Save Settings</button>
        <button onclick="saveName('${id}')">Save Username</button>
      </div>
    </div>
  </div>`;
}

function renderPosts(items){
  const box=document.getElementById("recentPosts");
  if(!items||!items.length){box.innerHTML='<div class="small">No recorded posts yet.</div>';return;}
  box.innerHTML=items.map(p=>`<div class="postrow">
    <div><b>${esc(p.account||"")}</b> <span class="small">${esc(p.at||"")}</span></div>
    <div class="small" style="margin:4px 0">${esc(p.caption||"")}</div>
    <div class="small" style="word-break:break-all">${esc(p.folder||"")}</div>
    <div class="row">
      <button onclick='openFolder(${JSON.stringify(p.folder||"")})'>Open Folder</button>
      ${p.post_url?`<button onclick='window.open(${JSON.stringify(p.post_url)},"_blank")'>Open Post</button>`:""}
    </div>
  </div>`).join("");
}

function render(data){
  const accounts=data.accounts||{};
  document.getElementById("accounts").innerHTML=Object.entries(accounts).map(([id,a])=>accountCard(id,a)).join("");
  renderPosts(data.recent_posts||[]);
  const act=data.activity||[];
  document.getElementById("activity").innerHTML=act.map(x=>`<div class="logrow ${x.level==="error"?"bad":x.level==="warn"?"warn":x.level==="good"?"ok":""}"><span class="small">${esc(x.time||"")}</span> ${esc(x.message||"")}</div>`).join("");
  const e=data.engine||{};
  document.getElementById("engineInfo").textContent=`Media: ${e.media_root||""} · Ollama: ${e.ollama_model||""} · Vision: ${e.vision_model||"none"} · ffmpeg: ${e.ffmpeg?"yes":"no"}`;
}

async function refreshNow(force=false){
  if(refreshBusy)return;
  const auto=document.getElementById("autoRefresh").checked;
  if(!force&&(!auto||editing)){document.getElementById("refreshState").textContent=editing?"Refresh paused while typing":"Auto refresh off";return;}
  refreshBusy=true;
  try{const r=await fetch("/api/metrics",{cache:"no-store"});const d=await r.json();render(d);document.getElementById("refreshState").textContent="Updated "+new Date().toLocaleTimeString();}
  catch(e){document.getElementById("refreshState").textContent="Refresh error";}
  finally{refreshBusy=false;}
}

async function act(id,action){try{await api(action,{account_id:id});await refreshNow(true);toast(`${action} requested.`);}catch(e){toast(e.message,true)}}
async function task(id,taskName){try{await api("task",{account_id:id,task:taskName});toast(`Started ${taskName}.`);}catch(e){toast(e.message,true)}}
async function clearLogin(id){if(!confirm("Clear saved TikTok login cookies/storage for this account?"))return;try{await api("clear_login",{account_id:id});toast("Clear-login started.");}catch(e){toast(e.message,true)}}
async function openFolder(folder){try{await api("open_folder",{folder});toast("Opened source folder.");}catch(e){toast(e.message,true)}}
async function saveName(id){const username=document.getElementById(`name_${id}`).value;try{await api("save_name",{account_id:id,username});editing=false;await refreshNow(true);toast("Username saved.");}catch(e){toast(e.message,true)}}

async function saveSettings(id){
  const s={
    mode:document.getElementById(`mode_${id}`).value,
    follow_source:document.getElementById(`fsource_${id}`).value,
    follow_limit:Number(document.getElementById(`followlim_${id}`).value),
    caption_char_limit:Number(document.getElementById(`caplim_${id}`).value),
    reply_char_limit:Number(document.getElementById(`replim_${id}`).value),
    video_frames:Number(document.getElementById(`frames_${id}`).value),
    max_writes_per_window:Number(document.getElementById(`maxwrites_${id}`).value),
    write_window_seconds:Number(document.getElementById(`window_${id}`).value),
    write_min_gap_seconds:Number(document.getElementById(`gap_${id}`).value),
    upload_cooldown_seconds:Number(document.getElementById(`uploadgap_${id}`).value),
    workflow_min_seconds:Number(document.getElementById(`wmin_${id}`).value),
    workflow_max_seconds:Number(document.getElementById(`wmax_${id}`).value),
    target_accounts:splitList(document.getElementById(`targets_${id}`).value),
    target_hashtags:splitList(document.getElementById(`tags_${id}`).value),
    persona_prompt:document.getElementById(`persona_${id}`).value,
    caption_prompt:document.getElementById(`capprompt_${id}`).value,
    dm_prompt:document.getElementById(`dmprompt_${id}`).value,
    comment_prompt:document.getElementById(`commentprompt_${id}`).value,
    require_video_vision:document.getElementById(`reqvision_${id}`).checked,
    enable_dm_replies:document.getElementById(`dmen_${id}`).checked,
    enable_comment_replies:document.getElementById(`commenten_${id}`).checked,
    headless_automation:document.getElementById(`headless_${id}`).checked,
  };
  try{await api("save_settings",{account_id:id,settings:s});editing=false;await refreshNow(true);toast("Settings saved.");}
  catch(e){toast(e.message,true)}
}

async function addAccountPrompt(){
  const u=prompt("TikTok username to add:");
  if(!u)return;
  try{await api("add_account",{username:u});await refreshNow(true);}
  catch(e){toast(e.message,true)}
}

document.getElementById("autoRefresh").addEventListener("change",()=>refreshNow(true));
setInterval(()=>refreshNow(false),5000);
refreshNow(true);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            raw = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/html; charset=utf-8"
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/metrics":
            response_json(self, metrics_payload())
            return

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        response_json(self, {"ok": False, "error": "Not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/control":
            response_json(self, {"ok": False, "error": "Not found"}, 404)
            return

        try:
            body = read_body(self)
            action = str(body.get("action", "")).strip()
            account_id = str(body.get("account_id", "")).strip()

            if action == "add_account":
                result = add_account(body.get("username", ""))
                response_json(self, {"ok": True, "account": result})
                return

            if action == "open_folder":
                response_json(
                    self,
                    open_media_folder(body.get("folder", "")),
                )
                return

            account = find_account(account_id)
            if not account:
                raise ValueError("Unknown account.")

            if action == "quick_login":
                quick_login(account_id)
            elif action == "open_login":
                start_login_browser(account_id)
            elif action == "save_login":
                request_save_login(account_id)
            elif action == "cancel_login":
                cancel_login(account_id)
            elif action == "disconnect":
                disconnect_account(account_id)
            elif action == "clear_login":
                threading.Thread(
                    target=clear_saved_login_worker,
                    args=(account_id,),
                    daemon=True,
                ).start()
            elif action == "auto_on":
                enable_auto(account_id, True)
            elif action == "auto_off":
                enable_auto(account_id, False)
            elif action == "task":
                start_manual_action(
                    account_id,
                    str(body.get("task", "")),
                )
            elif action == "save_settings":
                clean = save_settings(
                    account_id,
                    body.get("settings", {}),
                )
                response_json(
                    self,
                    {"ok": True, "settings": clean},
                )
                return
            elif action == "save_name":
                rename_account(
                    account_id,
                    body.get("username", ""),
                )
            else:
                raise ValueError(f"Unknown action: {action}")

            response_json(self, {"ok": True, "action": action})

        except Exception as exc:
            response_json(
                self,
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:400]}",
                },
                400,
            )


# =============================================================================
# Startup
# =============================================================================

def initialize() -> None:
    ensure_dirs()
    save_accounts()

    # Important testing behavior: every Python process starts disconnected.
    CONNECTED_ACCOUNTS.clear()
    AUTO_ENABLED_ACCOUNTS.clear()
    LOGIN_SAVE_EVENTS.clear()
    LOGIN_CANCEL_EVENTS.clear()

    for account in ACCOUNTS:
        ensure_account_analytics(account)
        update_account(
            account["id"],
            connected=False,
            auto_enabled=False,
            status="Disconnected",
            last_error=None,
        )

    add_activity(
        "TikTok Control Head started DISCONNECTED; "
        "no account login/browser validation occurred at boot",
        "good",
    )

    print()
    print("TikTok Control Head v3")
    print("======================")
    print(f"Dashboard:   http://{HOST}:{PORT}")
    print(f"Media root:  {MEDIA_ROOT}")
    print(f"Profiles:    {PROFILE_ROOT}")
    print("Boot mode:   DISCONNECTED")
    print(
        "Use Open Login -> complete TikTok challenge manually -> Save Login, "
        "or Quick Login for an already-saved profile."
    )
    print()


def main() -> None:
    initialize()
    start_scheduler()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping TikTok Control Head.")
    finally:
        AUTOMATION_STOP.set()
        server.server_close()


if __name__ == "__main__":
    main()
