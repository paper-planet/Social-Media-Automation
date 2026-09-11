#!/usr/bin/env python3
"""
TikTok Control Head v25
----------------------
Local-only TikTok automation dashboard with:
- DISCONNECTED boot (no TikTok browser/login/API traffic at startup)
- Persistent Playwright Chromium profiles
- Open Login -> user completes TikTok login/challenge manually -> Save Login
- Quick Login validates a saved profile only when explicitly clicked
- Disconnect keeps saved profile; Clear Saved Login removes TikTok cookies/storage
- Independent per-account feature switches for posts, follows, engagement, DMs and comments
- Per-account editable persona/caption/DM/comment prompts
- Per-account caption/reply char limits
- Per-account target accounts / hashtags
- Follow feature can use followers/following/both of configured target accounts
- Conservative shared write pacing across accounts in this process
- Video "watching": samples 3 chronological frames (T480 default)
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
REMOVED_ACCOUNTS_FILE = DATA_ROOT / "removed_accounts.json"
SETTINGS_FILE = DATA_ROOT / "control_settings.json"
ANALYTICS_FILE = DATA_ROOT / "analytics.json"
RECENT_POSTS_FILE = DATA_ROOT / "recent_posts.json"

def default_media_root() -> Path:
    """Prefer the known repost archive when mounted, while keeping env override support."""
    preferred = Path("/media/dev/256GB/ig-reposts-data")
    if preferred.exists() and preferred.is_dir():
        return preferred
    return Path.home() / "social-media-pool"


MEDIA_ROOT = Path(
    os.environ.get(
        "TIKTOK_MEDIA_ROOT",
        str(default_media_root()),
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
    os.environ.get("OLLAMA_VISION_MODEL", "qwen2.5vl:3b"),
).strip()

# Qwen2.5-VL 3B needs more context than Ollama's 4096-token default when
# multiple video frames are sent. 8192 is a good CPU/RAM-conscious setting
# for the ThinkPad T480.
OLLAMA_VISION_CONTEXT = max(4096, int(os.environ.get("TIKTOK_OLLAMA_VISION_CONTEXT", "8192")))

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


# Conservative automation pacing defaults to reduce repetitive/spammy behavior.
DM_CHECK_INTERVAL_SECONDS = max(
    60, int(os.environ.get("TIKTOK_DM_CHECK_INTERVAL_SECONDS", "120"))
)
COMMENT_CHECK_INTERVAL_SECONDS = max(
    120, int(os.environ.get("TIKTOK_COMMENT_CHECK_INTERVAL_SECONDS", "300"))
)
DM_THREAD_REPLY_COOLDOWN_SECONDS = max(
    60, int(os.environ.get("TIKTOK_DM_THREAD_REPLY_COOLDOWN_SECONDS", "1800"))
)
MAX_DM_REPLIES_PER_PASS_DEFAULT = max(
    1, int(os.environ.get("TIKTOK_MAX_DM_REPLIES_PER_PASS", "1"))
)
MAX_COMMENT_REPLIES_PER_PASS_DEFAULT = max(
    1, int(os.environ.get("TIKTOK_MAX_COMMENT_REPLIES_PER_PASS", "1"))
)
ACTION_HUMAN_DELAY_MIN_SECONDS = max(
    0, int(os.environ.get("TIKTOK_ACTION_HUMAN_DELAY_MIN_SECONDS", "8"))
)
ACTION_HUMAN_DELAY_MAX_SECONDS = max(
    ACTION_HUMAN_DELAY_MIN_SECONDS,
    int(os.environ.get("TIKTOK_ACTION_HUMAN_DELAY_MAX_SECONDS", "16")),
)
CONTROL_HEAD_REFRESH_SECONDS = max(
    5, int(os.environ.get("TIKTOK_CONTROL_HEAD_REFRESH_SECONDS", "10"))
)

ENGAGE_CLIPS_PER_PASS = max(
    1, min(6, int(os.environ.get("TIKTOK_ENGAGE_CLIPS_PER_PASS", "3")))
)
CHALLENGE_MANUAL_WAIT_SECONDS = max(
    60, int(os.environ.get("TIKTOK_CHALLENGE_MANUAL_WAIT_SECONDS", "600"))
)

# Manual 7-follow batches use a shorter local gap than normal automation.
# This does not bypass TikTok limits; verified restriction/challenge signals still stop the batch.
MANUAL_FOLLOW_GAP_SECONDS = max(2, min(15, int(os.environ.get("TIKTOK_MANUAL_FOLLOW_GAP_SECONDS", "4"))))

# One manual "Follow Target" click queues a faster paced batch of up to 7 confirmed follows.
MANUAL_FOLLOW_BATCH = max(1, min(7, int(os.environ.get("TIKTOK_MANUAL_FOLLOW_BATCH", "7"))))

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
LAST_DM_CHECK: dict[str, float] = {}
LAST_COMMENT_CHECK: dict[str, float] = {}
LAST_TASK_BY_ACCOUNT: dict[str, str] = {}
MANUAL_FOLLOW_LAST_ATTEMPT: dict[str, float] = {}
ACTION_VERIFY_FAILURES: dict[tuple[str, str], int] = {}

LOGIN_SAVE_EVENTS: dict[str, threading.Event] = {}
LOGIN_CANCEL_EVENTS: dict[str, threading.Event] = {}
LOGIN_THREADS: dict[str, threading.Thread] = {}

AUTOMATION_STOP = threading.Event()
AUTOMATION_THREAD: threading.Thread | None = None
# Runtime tombstones prevent removed accounts from being relaunched by workers/scheduler.
REMOVED_RUNTIME_ACCOUNTS: set[str] = set()

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
            out.append({
                "id": aid,
                "username": user,
                "enabled": bool(item.get("enabled", True)),
            })
        if len(out) >= 3:
            break
    return out



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

    if len(ACCOUNTS) >= 3:
        raise RuntimeError("The Control Head supports a maximum of 3 configured TikTok accounts.")

    removed = load_json(REMOVED_ACCOUNTS_FILE, {})
    removed = removed if isinstance(removed, dict) else {}
    restored_id = str(removed.get(username.lower(), "")).strip()

    used = {a["id"] for a in ACCOUNTS}
    reserved = {str(v) for v in removed.values() if v}

    if restored_id and restored_id not in used:
        account_id = restored_id
        removed.pop(username.lower(), None)
        save_json(REMOVED_ACCOUNTS_FILE, removed)
    else:
        n = 1
        while f"tt_{n}" in used or f"tt_{n}" in reserved:
            n += 1
        account_id = f"tt_{n}"

    account = {"id": account_id, "username": username, "enabled": True}
    ACCOUNTS.append(account)
    REMOVED_RUNTIME_ACCOUNTS.discard(account_id)
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


def remove_account(account_id: str) -> dict[str, Any]:
    """
    Remove an account from the Control Head configuration while preserving its
    persistent Chromium profile, history and settings for easy re-add/recovery.
    """
    account = find_account(account_id)
    if not account:
        raise ValueError("Unknown account.")

    # Tombstone first so schedulers/workers stop treating this account as runnable.
    REMOVED_RUNTIME_ACCOUNTS.add(account_id)

    # Cancel an interactive login browser if one is open. Do not block removal on
    # an account lock: an already-running worker may need a moment to unwind, but
    # it must not be scheduled again.
    cancel_login(account_id)

    CONNECTED_ACCOUNTS.discard(account_id)
    AUTO_ENABLED_ACCOUNTS.discard(account_id)
    ACCOUNT_COOLDOWNS.pop(account_id, None)
    NEXT_TASK_OVERRIDE.pop(account_id, None)
    NEXT_DUE.pop(account_id, None)
    LAST_DM_CHECK.pop(account_id, None)
    LAST_COMMENT_CHECK.pop(account_id, None)
    LAST_TASK_BY_ACCOUNT.pop(account_id, None)

    removed = load_json(REMOVED_ACCOUNTS_FILE, {})
    removed = removed if isinstance(removed, dict) else {}
    removed[account["username"].lower()] = account_id
    save_json(REMOVED_ACCOUNTS_FILE, removed)

    ACCOUNTS.remove(account)
    save_accounts()

    data = load_analytics()
    data.get("accounts", {}).pop(account_id, None)
    save_analytics(data)

    add_activity(
        f"{account['username']}: removed from configured accounts; "
        "persistent profile/history/settings were preserved",
        "good",
    )
    return {
        "ok": True,
        "account_id": account_id,
        "username": account["username"],
        "preserved_local_data": True,
    }


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
        "saves_sent": 0,
        "reposts_sent": 0,
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
        "video_frames": 3,
        "require_video_vision": True,

        # Independent feature switches.
        "enable_posts": True,
        "enable_follow": True,
        "enable_engage": True,
        "enable_dms": True,
        "enable_comments": True,

        "headless_automation": False,
        "max_writes_per_window": 15,
        "write_window_seconds": 300,
        "write_min_gap_seconds": 6,
        "upload_cooldown_seconds": 300,
        "max_writes_per_workflow": 6,
        "workflow_min_seconds": 30,
        "workflow_max_seconds": 90,
    }



def sanitize_settings(account: dict[str, Any], raw: Any) -> dict[str, Any]:
    base = default_settings(account)
    raw = raw if isinstance(raw, dict) else {}

    source = str(raw.get("follow_source", base["follow_source"])).strip().lower()
    if source not in {"followers", "following", "both"}:
        source = "both"

    def iv(key: str, lo: int, hi: int) -> int:
        try:
            value = int(raw.get(key, base[key]))
        except Exception:
            value = int(base[key])
        return max(lo, min(hi, value))

    wmin = iv("workflow_min_seconds", 15, 7200)
    wmax = max(wmin, iv("workflow_max_seconds", wmin, 14400))

    # v24 and earlier shipped very restrictive defaults (5 writes / 20 min,
    # 45s gap, 2 writes/workflow). If the saved file still has that exact
    # untouched combination, migrate it to a practical but still bounded set.
    old_default_pacing = (
        int(raw.get("max_writes_per_window", 5) or 5) == 5
        and int(raw.get("write_window_seconds", 1200) or 1200) == 1200
        and int(raw.get("write_min_gap_seconds", 45) or 45) == 45
        and int(raw.get("max_writes_per_workflow", 2) or 2) == 2
    )
    if old_default_pacing:
        raw = dict(raw)
        raw["max_writes_per_window"] = 15
        raw["write_window_seconds"] = 300
        raw["write_min_gap_seconds"] = 6
        raw["max_writes_per_workflow"] = 6

    legacy_mode = str(raw.get("mode", "")).strip().lower()
    legacy_map = {
        "balanced": (True, True, True, True, True),
        "upload_only": (True, False, False, False, False),
        "follow_only": (False, True, False, False, False),
        "engage_only": (False, False, True, False, True),
        "dm_only": (False, False, False, True, False),
        "manual": (False, False, False, False, False),
    }
    legacy = legacy_map.get(legacy_mode, (True, True, True, True, True))

    def feature(name, idx, old_alias=None):
        if name in raw:
            return bool(raw[name])
        if old_alias and old_alias in raw:
            return bool(raw[old_alias])
        if legacy_mode:
            return bool(legacy[idx])
        return bool(base[name])

    return {
        "persona_prompt": str(raw.get("persona_prompt", base["persona_prompt"]))[:6000],
        "caption_prompt": str(raw.get("caption_prompt", base["caption_prompt"]))[:4000],
        "dm_prompt": str(raw.get("dm_prompt", base["dm_prompt"]))[:4000],
        "comment_prompt": str(raw.get("comment_prompt", base["comment_prompt"]))[:4000],
        "caption_char_limit": iv("caption_char_limit", 80, 2000),
        "reply_char_limit": iv("reply_char_limit", 20, 1000),
        "target_accounts": normalize_list(raw.get("target_accounts", base["target_accounts"]), "@"),
        "target_hashtags": normalize_list(raw.get("target_hashtags", base["target_hashtags"]), "#"),
        "follow_source": source,
        "follow_limit": iv("follow_limit", 1, 21),
        # Keep vision input light enough for the T480. This intentionally
        # overrides older saved values such as 5 or 9 frames.
        "video_frames": 3,
        "require_video_vision": bool(raw.get("require_video_vision", base["require_video_vision"])),

        "enable_posts": feature("enable_posts", 0),
        "enable_follow": feature("enable_follow", 1),
        "enable_engage": feature("enable_engage", 2),
        "enable_dms": feature("enable_dms", 3, "enable_dm_replies"),
        "enable_comments": feature("enable_comments", 4, "enable_comment_replies"),

        "headless_automation": bool(raw.get("headless_automation", base["headless_automation"])),
        "max_writes_per_window": iv("max_writes_per_window", 1, 21),
        "write_window_seconds": iv("write_window_seconds", 60, 7200),
        "write_min_gap_seconds": iv("write_min_gap_seconds", 3, 600),
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
    before = sanitize_settings(account, data.get(account_id, {}))
    current = dict(data.get(account_id, {})) if isinstance(data.get(account_id, {}), dict) else {}
    current.update(patch)
    clean = sanitize_settings(account, current)

    if clean != before:
        data[account_id] = clean
        save_json(SETTINGS_FILE, data)
        enabled = [
            label for key, label in (
                ("enable_posts", "posts"),
                ("enable_follow", "follow"),
                ("enable_engage", "engage"),
                ("enable_dms", "DMs"),
                ("enable_comments", "comments"),
            )
            if clean.get(key)
        ]
        add_activity(
            f"{account['username']}: behavior auto-saved: "
            + (", ".join(enabled) if enabled else "all features off"),
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
        "dm_thread_last_reply_at": {},
        "replied_comments": [],
        "liked_videos": [],
        "saved_videos": [],
        "reposted_videos": [],
        "followed_users": [],
        "pending_uploads": [],
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


def choose_active_page(context):
    """Prefer an already-open non-blank tab over a fresh about:blank page."""
    pages = list(context.pages)
    for candidate in reversed(pages):
        try:
            if candidate.url and candidate.url != "about:blank":
                return candidate
        except Exception:
            pass
    return pages[0] if pages else context.new_page()


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
                    "-vf", "scale=640:-2:force_original_aspect_ratio=decrease",
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

    if VISION_MODEL_CACHE is not False:
        return VISION_MODEL_CACHE if isinstance(VISION_MODEL_CACHE, str) else None

    names = ollama_model_names()
    installed = {name.lower(): name for name in names}

    configured = str(OLLAMA_VISION_MODEL or "").strip()
    if configured:
        exact = installed.get(configured.lower())
        if exact:
            VISION_MODEL_CACHE = exact
            return exact

        configured_base = configured.lower().split(":", 1)[0]
        for name in names:
            if name.lower().split(":", 1)[0] == configured_base:
                VISION_MODEL_CACHE = name
                add_activity(
                    f"Vision model {configured} was not an exact installed name; using {name}.",
                    "warn",
                )
                return name

        add_activity(
            f"Configured vision model {configured} is not installed; checking installed vision models.",
            "warn",
        )

    # Prefer installed lightweight/local models, including the user's Gemma 3.
    hints = (
        "gemma3",
        "qwen2.5vl",
        "qwen3-vl",
        "llama3.2-vision",
        "llama3.1-vision",
        "llama3-vision",
        "qwen2-vl",
        "llava",
        "minicpm-v",
        "moondream",
        "bakllava",
    )
    for hint in hints:
        for name in names:
            if hint in name.lower():
                VISION_MODEL_CACHE = name
                add_activity(f"Auto-selected Ollama vision model: {name}", "good")
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


OLLAMA_VISION_TIMEOUT_SECONDS = 0
OLLAMA_TEXT_TIMEOUT_SECONDS = 0


def ollama_chat_timeout(**kwargs):
    """Run Ollama synchronously in quality-first mode.

    A timeout can be supplied for future callers, but the upload workflow passes
    zero so a capable local model is allowed to finish instead of producing a
    generic caption just because inference is slow.
    """
    timeout = float(kwargs.pop("_timeout_seconds", 0) or 0)
    if timeout <= 0:
        return ollama.chat(**kwargs)
    result: dict[str, Any] = {}
    finished = threading.Event()
    def worker() -> None:
        try:
            result["value"] = ollama.chat(**kwargs)
        except Exception as exc:
            result["error"] = exc
        finally:
            finished.set()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    if not finished.wait(timeout):
        raise TimeoutError(f"Ollama request exceeded {timeout:.0f}s")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def extract_video_frames_retry(path: Path, count: int = 5) -> list[Path]:
    """Second-pass frame extraction using a single ffmpeg invocation.

    Some videos/containers behave poorly with repeated -ss seeks.  The first
    extractor remains the fast path; this fallback asks ffmpeg to decode the
    stream once and select evenly spaced frames.  It is deliberately capped
    at nine frames to keep the vision prompt manageable.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return []
    count = max(3, min(9, int(count)))
    duration = ffprobe_duration(path)
    if not duration or duration <= 0:
        return []

    # Use select expressions based on frame number after decoding the stream.
    # This avoids seeking failures on some MP4/MOV files.
    outputs: list[Path] = []
    for index in range(count):
        fraction = (index + 1) / (count + 1)
        sec = max(0.0, duration * fraction)
        out = cache_name(path, ".jpg", f"retry_{count}_{index + 1}")
        if out.exists() and out.stat().st_size > 1024:
            outputs.append(out)
            continue
        try:
            subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(path),
                    "-ss", f"{sec:.3f}",
                    "-frames:v", "1",
                    "-vf", "scale=640:-2:force_original_aspect_ratio=decrease",
                    "-q:v", "3", str(out),
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=60,
            )
            if out.exists() and out.stat().st_size > 1024:
                outputs.append(out)
        except Exception:
            continue
    return outputs


def analyze_media_sequence(
    media_path: Path,
    settings: dict[str, Any],
    sidecar: str = "",
) -> dict[str, Any]:
    model = vision_model()
    is_video = media_path.suffix.lower() in SUPPORTED_VIDEOS
    add_activity(
        f"Media analysis: {media_path.name} | model={model or 'none'} | "
        f"video={is_video}",
        "info",
    )
    frames = (
        extract_video_frames(media_path, int(settings["video_frames"]))
        if is_video
        else [media_path]
    )

    # Do not discard an otherwise usable video just because one or more
    # ffmpeg frame extractions failed.  The old workflow required 3 frames
    # and would silently skip the entire upload when only 1-2 frames made it
    # through.  Quality-first behavior is to retry extraction and, if needed,
    # analyze every successfully extracted frame rather than abandoning the
    # upload.
    if is_video and len(frames) < 3:
        retry_count = 3
        try:
            retry_frames = extract_video_frames_retry(media_path, retry_count)
            if len(retry_frames) > len(frames):
                frames = retry_frames
        except Exception:
            pass

    if is_video and settings["require_video_vision"] and not model:
        return {
            "ok": False,
            "reason": "video vision required but no Ollama vision model is available",
            "model": None,
            "frames": len(frames),
        }

    if is_video and settings["require_video_vision"] and len(frames) < 2:
        return {
            "ok": False,
            "reason": (
                f"video vision could not extract enough usable frames "
                f"(got {len(frames)}); refusing to invent visual details"
            ),
            "model": model,
            "frames": len(frames),
        }

    perspective = "UNKNOWN"
    visual = ""

    if model and frames:
        add_activity(
            f"Vision input ready: {len(frames)} frame(s); sending to {model}",
            "info",
        )
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
            response = ollama_chat_timeout(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [str(frame) for frame in frames],
                    }
                ],
                options={
                    "temperature": 0.12,
                    "top_p": 0.8,
                    "num_ctx": OLLAMA_VISION_CONTEXT,
                },
                _timeout_seconds=OLLAMA_VISION_TIMEOUT_SECONDS,
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
            add_activity(
                f"Vision analysis complete: {media_path.name} | "
                f"perspective={perspective} | response_chars={len(visual)}",
                "good",
            )
        except Exception as exc:
            add_activity(
                f"Vision analysis failed for {media_path.name}: "
                f"{str(exc)[:140]}; continuing without visual description",
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
        "visual_available": bool(visual),
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
            response = ollama_chat_timeout(
                model=OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt + repair},
                ],
                options={"temperature": 0.8, "top_p": 0.9},
                _timeout_seconds=OLLAMA_TEXT_TIMEOUT_SECONDS,
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

    caption = None
    if media_context.get("visual_available"):
        add_activity(
            f"@{account_id}: generating caption with {OLLAMA_MODEL} "
            "(waiting for complete model response)"
        )
        caption = ollama_text(
            settings["persona_prompt"],
            caption_prompt,
            attempts=1,
        )
    else:
        add_activity(
            f"@{account_id}: skipping caption AI because vision was unavailable; "
            "using immediate fallback caption",
            "warn",
        )

    if not caption:
        # Never let a slow/broken local LLM prevent an otherwise valid upload.
        # Prefer sidecar/supporting text when present; otherwise use a neutral
        # generic caption rather than inventing video content.
        fallback_source = ""
        if "SUPPORTING TEXT:\n" in context:
            fallback_source = context.split("SUPPORTING TEXT:\n", 1)[1].strip()
        if not fallback_source or fallback_source.startswith("No reliable semantic"):
            fallback_source = "Just sharing this moment."
        caption = clip_chars(clean_generation(fallback_source), int(settings["caption_char_limit"]))
        add_activity(
            f"@{account_id}: ⚠️ using safe fallback caption",
            "warn",
        )

    # IMPORTANT: a timed-out Ollama request keeps running in its daemon thread
    # and can occupy/queue the local Ollama server. If vision already failed,
    # do not issue more AI requests for this post. Move straight to deterministic
    # fallback tags so the TikTok upload itself is never held hostage by Ollama.
    raw_tags = ""
    if media_context.get("visual_available"):
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

        add_activity(
            f"@{account_id}: generating hashtags with {OLLAMA_MODEL} "
            "(waiting for complete model response)"
        )
        raw_tags = ollama_text(
            settings["persona_prompt"],
            hashtag_prompt,
            attempts=1,
        ) or ""
    else:
        add_activity(
            f"@{account_id}: skipping hashtag AI because vision was unavailable; "
            "using immediate fallback tags",
            "warn",
        )

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
    """Recursively find folders under MEDIA_ROOT that contain supported media."""
    if not MEDIA_ROOT.exists() or not MEDIA_ROOT.is_dir():
        return []

    folders = []
    try:
        candidates = [MEDIA_ROOT] + [p for p in MEDIA_ROOT.rglob("*") if p.is_dir()]
    except OSError:
        candidates = [MEDIA_ROOT]

    for folder in candidates:
        try:
            media = [
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in SUPPORTED_MEDIA
            ]
            if not media:
                continue
            # Use the relative path as the stable ID so same-named nested folders
            # do not collide in posting history.
            try:
                folder_id = str(folder.relative_to(MEDIA_ROOT)) or "."
            except ValueError:
                folder_id = str(folder)
            folders.append(
                {
                    "id": folder_id,
                    "path": folder,
                    "media": sorted(media),
                }
            )
        except (OSError, PermissionError):
            continue
    return folders


def media_key(media_path: Path) -> str:
    """Return a stable per-file identifier for posting history.

    Prefer a path relative to MEDIA_ROOT so the same file remains recognizable
    across runs without depending on the machine-specific absolute path.
    """
    path = Path(media_path).expanduser()
    try:
        return str(path.resolve().relative_to(MEDIA_ROOT.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return str(path.resolve()).replace("\\", "/")


def choose_unused_folder(
    account_id: str,
    folders: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Legacy folder chooser retained for compatibility with old state."""
    history = load_history(account_id)
    used = set(history["posted_ids"])
    available = [f for f in folders if f["id"] not in used]
    return random.choice(available) if available else None


def choose_random_unused_media(
    account_id: str,
    folders: list[dict[str, Any]],
) -> tuple[dict[str, Any], Path] | None:
    """
    Pick one random file not already confirmed or pending.

    Pending media are withheld from retry so an ambiguous TikTok response cannot
    create a duplicate post while profile verification catches up.
    """
    history = load_history(account_id)
    used = set(str(x) for x in history.get("posted_ids", []))
    pending = {
        str(item.get("media_id") or "")
        for item in history.get("pending_uploads", [])
        if isinstance(item, dict)
    }

    candidates: list[tuple[dict[str, Any], Path]] = []
    for folder in folders:
        for media_path in folder.get("media", []):
            key = media_key(media_path)
            if key in used or key in pending or folder.get("id") in used:
                continue
            candidates.append((folder, media_path))
    return random.choice(candidates) if candidates else None



def detect_tiktok_challenge(page) -> str:
    """
    Detect TikTok CAPTCHA / slider / puzzle / verification UI.

    This function intentionally does NOT solve, drag, click through, or bypass
    the challenge. It only pauses automation so the user can complete it.
    """
    try:
        url = str(page.url or "").lower()
    except Exception:
        url = ""

    if any(token in url for token in ("/captcha", "captcha?", "/verify", "verify?", "/challenge")):
        return f"challenge URL: {url[:220]}"

    selectors = (
        "div[class*='captcha' i]",
        "[id*='captcha' i]",
        "iframe[src*='captcha' i]",
        "iframe[src*='verify' i]",
        "[class*='verify' i][role='dialog']",
    )
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible(timeout=250):
                try:
                    value = re.sub(
                        r"\s+",
                        " ",
                        loc.inner_text(timeout=500) or "",
                    ).strip()
                except Exception:
                    value = ""
                return f"{selector}: {value[:220]}".strip()
        except Exception:
            pass

    phrases = (
        "drag the slider",
        "complete the puzzle",
        "solve the puzzle",
        "verify to continue",
        "security verification",
        "complete verification",
        "please verify",
        "captcha",
    )
    try:
        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=900) or "",
        ).strip()
        low = body.lower()
        for phrase in phrases:
            pos = low.find(phrase)
            if pos >= 0:
                return body[max(0, pos - 100):pos + 280]
    except Exception:
        pass

    return ""


def pause_for_manual_tiktok_challenge(
    page,
    account_id: str,
    max_wait_seconds: int | None = None,
) -> bool:
    """
    Pause safely for a TikTok CAPTCHA/slider/puzzle and let the user solve it.

    This function never solves, drags, clicks through, or bypasses the challenge.
    After the user solves it, the CURRENT workflow resumes from where it was.
    Auto is restored only if it was enabled before the challenge appeared.

    Returns False after the challenge is clear so existing callers continue.
    Raises only if the challenge remains too long or headless mode prevents
    manual completion.
    """
    detail = detect_tiktok_challenge(page)
    if not detail:
        return False

    account = find_account(account_id)
    label = account["username"] if account else account_id
    settings = get_settings(account_id)
    was_auto_enabled = account_id in AUTO_ENABLED_ACCOUNTS

    # Temporarily stop scheduling new work while the current account worker owns
    # the browser and the user manually solves the puzzle.
    AUTO_ENABLED_ACCOUNTS.discard(account_id)
    NEXT_DUE.pop(account_id, None)
    update_account(
        account_id,
        auto_enabled=False,
        status="Manual puzzle required",
        last_error=(
            "TikTok puzzle/CAPTCHA detected. Complete it manually in the "
            "open Chromium window; the script will not bypass it."
        ),
    )
    add_activity(
        f"{label}: 🧩 TikTok puzzle/CAPTCHA detected. Current workflow paused; "
        "solve it manually in Chromium.",
        "warn",
    )
    add_activity(
        f"{label}: challenge detail: {detail[:240]}",
        "warn",
    )

    if settings.get("headless_automation", False):
        raise RuntimeError(
            "TikTok puzzle/CAPTCHA appeared during headless automation. "
            "Turn Headless Automation off and complete the challenge manually."
        )

    try:
        page.bring_to_front()
    except Exception:
        pass

    wait_seconds = int(
        max_wait_seconds
        if max_wait_seconds is not None
        else CHALLENGE_MANUAL_WAIT_SECONDS
    )
    deadline = time.time() + max(60, wait_seconds)
    last_log = 0.0

    # Passive wait: no clicks, drags or page navigation while the user is solving.
    while time.time() < deadline:
        page.wait_for_timeout(1200)
        current = detect_tiktok_challenge(page)

        if not current:
            if was_auto_enabled and account_id in CONNECTED_ACCOUNTS:
                AUTO_ENABLED_ACCOUNTS.add(account_id)
                NEXT_DUE[account_id] = time.monotonic() + 10.0
                status = "Connected / Auto On"
                auto_enabled = True
            else:
                status = "Connected / Auto Off"
                auto_enabled = False

            update_account(
                account_id,
                status=status,
                auto_enabled=auto_enabled,
                last_error=None,
            )
            add_activity(
                f"{label}: ✅ puzzle cleared manually; resuming the current workflow"
                + (" and restoring Auto" if was_auto_enabled else ""),
                "good",
            )
            return False

        now = time.time()
        if now - last_log >= 20:
            add_activity(
                f"{label}: waiting for manual puzzle completion "
                f"({max(0, int(deadline-now))}s remaining)",
                "info",
            )
            last_log = now

    raise RuntimeError(
        "TikTok puzzle/CAPTCHA is still present after the manual wait period. "
        "The current workflow stopped without attempting to bypass it."
    )



def _button_state_signature(locator) -> tuple[str, ...]:
    values = []
    try:
        values.append(re.sub(r"\s+", " ", locator.inner_text(timeout=600) or "").strip())
    except Exception:
        values.append("")
    for attr in ("aria-pressed", "aria-label", "title", "class"):
        try:
            values.append(str(locator.get_attribute(attr) or ""))
        except Exception:
            values.append("")
    try:
        svg_state = locator.evaluate(
            """el => {
                const svg = el.querySelector('svg');
                const path = el.querySelector('svg path');
                return JSON.stringify({
                    svgFill: svg?.getAttribute('fill') || '',
                    svgClass: svg?.getAttribute('class') || '',
                    pathFill: path?.getAttribute('fill') || '',
                    html: el.innerHTML.slice(0, 900)
                });
            }"""
        )
        values.append(str(svg_state or ""))
    except Exception:
        values.append("")
    return tuple(values)


def _find_visible_control(page, selectors):
    for selector in selectors:
        try:
            locs = page.locator(selector)
            for i in range(min(locs.count(), 8)):
                loc = locs.nth(i)
                if loc.is_visible(timeout=250):
                    return loc
        except Exception:
            pass
    return None


def _confirm_control_state_change(
    page,
    finder,
    before_signature: tuple[str, ...],
    timeout_seconds: int = 8,
) -> tuple[bool, str]:
    deadline = time.time() + max(2, timeout_seconds)
    while time.time() < deadline:
        current = finder()
        if current is not None:
            signature = _button_state_signature(current)
            joined = " ".join(signature).lower()
            if any(
                token in joined
                for token in (
                    "remove from favorites",
                    "saved",
                    "favorited",
                    "unfavorite",
                    "remove repost",
                    "undo repost",
                    "reposted",
                )
            ):
                return True, joined[:180]
            if signature != before_signature:
                return True, joined[:180]
        page.wait_for_timeout(500)
    return False, ""


def update_engagement_counter(account_id: str, key: str, increment: int = 1) -> None:
    data = load_analytics()
    account = find_account(account_id)
    row = data["accounts"].setdefault(
        account_id,
        default_account_analytics(account) if account else {},
    )
    row[key] = int(row.get(key) or 0) + int(increment)
    save_analytics(data)

def maybe_human_delay(fraction: float = 1.0) -> None:
    lo = max(0.0, ACTION_HUMAN_DELAY_MIN_SECONDS * float(fraction))
    hi = max(lo, ACTION_HUMAN_DELAY_MAX_SECONDS * float(fraction))
    if hi > 0:
        time.sleep(random.uniform(lo, hi))


def should_run_periodic_check(cache: dict[str, float], key: str, interval_seconds: int) -> bool:
    now = time.monotonic()
    last = cache.get(key, 0.0)
    if now - last < max(1, int(interval_seconds)):
        return False
    cache[key] = now
    return True


# =============================================================================
# TikTok actions
# =============================================================================

def detect_tiktok_action_restriction(page) -> str:
    phrases = (
        "you're following too fast",
        "you are following too fast",
        "following too fast",
        "too many attempts",
        "maximum number of attempts",
        "try again later",
        "slow down",
        "temporarily blocked",
        "temporarily restricted",
        "couldn't follow",
        "could not follow",
        "couldn't like",
        "could not like",
        "action blocked",
    )
    try:
        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=800) or "",
        ).strip()
    except Exception:
        return ""

    low = body.lower()
    for phrase in phrases:
        pos = low.find(phrase)
        if pos >= 0:
            return body[max(0, pos - 120):pos + 320]
    return ""


def enforce_tiktok_action_restriction(page, account_id: str, action: str) -> None:
    detail = detect_tiktok_action_restriction(page)
    if not detail:
        return

    account = find_account(account_id)
    label = account["username"] if account else account_id
    cooldown = datetime.now() + timedelta(minutes=30)
    ACCOUNT_COOLDOWNS[account_id] = cooldown
    AUTO_ENABLED_ACCOUNTS.discard(account_id)
    NEXT_DUE.pop(account_id, None)
    update_account(
        account_id,
        auto_enabled=False,
        status="Safety backoff",
        cooldown_until=cooldown.isoformat(timespec="seconds"),
        last_error=f"TikTok blocked/restricted {action}: {detail[:220]}",
    )
    add_activity(
        f"{label}: 🛡️ TikTok restriction detected during {action}; Auto OFF for 30 minutes",
        "warn",
    )
    raise RuntimeError(
        f"TikTok restriction detected during {action}: {detail[:260]}"
    )


def wait_for_manual_follow_burst_slot(page, account_id: str) -> None:
    """
    Manual Follow Target uses its own short per-attempt cadence.

    It intentionally does not wait on the long generic rolling write window,
    because the manual button already has a hard seven-attempt cap. Platform
    challenge/restriction signals still stop the burst immediately.
    """
    while True:
        pause_for_manual_tiktok_challenge(page, account_id)
        enforce_tiktok_action_restriction(page, account_id, "follow")

        now = time.monotonic()
        last = MANUAL_FOLLOW_LAST_ATTEMPT.get(account_id, 0.0)
        remaining = MANUAL_FOLLOW_GAP_SECONDS - (now - last)
        if remaining <= 0:
            MANUAL_FOLLOW_LAST_ATTEMPT[account_id] = now
            return

        time.sleep(min(0.5, max(0.05, remaining)))

def register_action_verification(
    account_id: str,
    action: str,
    confirmed: bool,
    detail: str = "",
) -> None:
    """
    Track UI actions that TikTok appears to accept but never confirms.

    Three consecutive EXPLICITLY failed write verifications disable Auto and
    apply a 30-minute backoff. Ambiguous follow verification is filtered before
    this function is called.
    """
    key = (account_id, action)
    account = find_account(account_id)

    if confirmed:
        ACTION_VERIFY_FAILURES.pop(key, None)
        return

    failures = ACTION_VERIFY_FAILURES.get(key, 0) + 1
    ACTION_VERIFY_FAILURES[key] = failures

    add_activity(
        f"{account['username'] if account else account_id}: "
        f"{action} was not confirmed by TikTok "
        f"({failures}/3){': ' + detail if detail else ''}",
        "warn",
    )

    if failures >= 3:
        cooldown = datetime.now() + timedelta(minutes=30)
        ACCOUNT_COOLDOWNS[account_id] = cooldown
        AUTO_ENABLED_ACCOUNTS.discard(account_id)
        NEXT_DUE.pop(account_id, None)
        update_account(
            account_id,
            auto_enabled=False,
            status="Safety backoff",
            cooldown_until=cooldown.isoformat(timespec="seconds"),
            last_error=(
                f"TikTok did not confirm three consecutive {action} actions. "
                "Automation paused instead of retrying through a possible restriction."
            ),
        )
        add_activity(
            f"{account['username'] if account else account_id}: "
            f"🛡️ Auto disabled for 30 minutes after repeated unconfirmed {action} actions",
            "warn",
        )


def activate_once_dom(locator, page, account_label: str, action_label: str) -> None:
    """
    Activate a TikTok UI control exactly once with a native DOM click.

    We deliberately do not fire multiple fallback clicks for Follow/Like/etc.
    because a delayed UI update could otherwise turn a successful Follow into
    an immediate Unfollow.
    """
    try:
        locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    try:
        locator.evaluate("(el) => el.click()")
    except Exception as exc:
        raise RuntimeError(
            f"{action_label} control could not be activated: {exc}"
        ) from exc

    page.wait_for_timeout(900)


def locator_signature(locator) -> tuple[str, str, str, str]:
    def attr(name: str) -> str:
        try:
            return str(locator.get_attribute(name) or "")
        except Exception:
            return ""

    try:
        text_value = re.sub(
            r"\s+",
            " ",
            locator.inner_text(timeout=700) or "",
        ).strip()
    except Exception:
        text_value = ""

    return (
        text_value,
        attr("aria-pressed"),
        attr("aria-label"),
        attr("class"),
    )


def wait_for_like_confirmation(
    page,
    like_locator,
    before_signature: tuple[str, str, str, str],
    timeout_seconds: int = 8,
) -> bool:
    deadline = time.time() + max(2, int(timeout_seconds))
    while time.time() < deadline:
        try:
            current = locator_signature(like_locator)
            if current[1].lower() == "true":
                return True
            if current != before_signature:
                # TikTok often changes class/icon/count rather than aria-pressed.
                return True
        except Exception:
            # A remounted like control also indicates the video action UI changed.
            return True
        page.wait_for_timeout(500)
    return False


def wait_for_follow_confirmation(
    page,
    follow_locator,
    parent_locator,
    timeout_seconds: int = 10,
) -> tuple[bool | None, str]:
    """
    First-stage follow confirmation from the currently open popup row.

    Returns:
      True  -> strong confirmation (Following / Requested / Friends)
      False -> row explicitly still says exact Follow after the wait
      None  -> popup rerender/state is ambiguous; caller should do a profile check

    "Message" and "Follow back" are intentionally NOT treated as proof that the
    current account follows the candidate.
    """
    deadline = time.time() + max(3, int(timeout_seconds))
    strong_positive = re.compile(
        r"^(Following|Requested|Friends)$",
        re.I,
    )

    last_texts = []
    saw_exact_follow = False

    while time.time() < deadline:
        texts = []

        try:
            if follow_locator.count():
                value = re.sub(
                    r"\s+",
                    " ",
                    follow_locator.inner_text(timeout=600) or "",
                ).strip()
                if value:
                    texts.append(value)
                    if strong_positive.search(value):
                        return True, value
                    if re.fullmatch(r"Follow", value, re.I):
                        saw_exact_follow = True
        except Exception:
            pass

        try:
            if parent_locator is not None and parent_locator.count():
                buttons = parent_locator.locator(
                    "button[data-e2e='follow-button']"
                )
                for i in range(min(buttons.count(), 4)):
                    try:
                        value = re.sub(
                            r"\s+",
                            " ",
                            buttons.nth(i).inner_text(timeout=600) or "",
                        ).strip()
                        if value:
                            texts.append(value)
                            if strong_positive.search(value):
                                return True, value
                            if re.fullmatch(r"Follow", value, re.I):
                                saw_exact_follow = True
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            if parent_locator is not None and parent_locator.count():
                row_text = re.sub(
                    r"\s+",
                    " ",
                    parent_locator.inner_text(timeout=700) or "",
                ).strip()
                if row_text:
                    texts.append(row_text)
                    if re.search(
                        r"\b(Following|Requested|Friends)\b",
                        row_text,
                        re.I,
                    ):
                        return True, row_text[:180]
        except Exception:
            pass

        if texts:
            last_texts = texts[-4:]

        page.wait_for_timeout(600)

    detail = " | ".join(last_texts)[:180]
    if saw_exact_follow:
        return False, detail or "Follow"
    return None, detail


def verify_follow_relationship_on_profile(
    context,
    candidate_username: str,
    timeout_seconds: int = 14,
) -> tuple[bool | None, str]:
    """
    Second-stage verification on the candidate's actual profile.

    True  = Following / Requested / Friends is observed.
    False = exact Follow remains visible after repeated profile checks.
    None  = profile state could not be determined reliably.
    """
    username = str(candidate_username or "").strip().lstrip("@")
    if not username or username.startswith("candidate_"):
        return None, "candidate username unavailable"

    probe = None
    deadline = time.time() + max(6, int(timeout_seconds))
    saw_exact_follow = False
    last_state = ""

    try:
        probe = context.new_page()
        goto(probe, f"https://www.tiktok.com/@{username}", 1800)

        while time.time() < deadline:
            # Challenge on the probe page is not bypassed and makes verification ambiguous.
            challenge = detect_tiktok_challenge(probe)
            if challenge:
                return None, f"profile verification challenge: {challenge[:120]}"

            controls = []
            for selector in (
                "button[data-e2e='follow-button']",
                "button[data-e2e='follow-btn']",
            ):
                try:
                    locs = probe.locator(selector)
                    for i in range(min(locs.count(), 5)):
                        if locs.nth(i).is_visible(timeout=200):
                            controls.append(locs.nth(i))
                except Exception:
                    pass

            if not controls:
                try:
                    locs = probe.get_by_role(
                        "button",
                        name=re.compile(
                            r"^(Follow|Following|Requested|Friends|Follow back)$",
                            re.I,
                        ),
                    )
                    for i in range(min(locs.count(), 5)):
                        if locs.nth(i).is_visible(timeout=200):
                            controls.append(locs.nth(i))
                except Exception:
                    pass

            for control in controls:
                try:
                    state = re.sub(
                        r"\s+",
                        " ",
                        control.inner_text(timeout=600) or "",
                    ).strip()
                except Exception:
                    state = ""

                if not state:
                    continue

                last_state = state

                if re.fullmatch(r"(Following|Requested|Friends)", state, re.I):
                    return True, state

                if re.fullmatch(r"Follow", state, re.I):
                    saw_exact_follow = True

            probe.wait_for_timeout(1200)

            # Refresh once midway so a delayed server-side follow can surface.
            if time.time() + 5 < deadline:
                try:
                    probe.reload(
                        wait_until="domcontentloaded",
                        timeout=PAGE_TIMEOUT_MS,
                    )
                    probe.wait_for_timeout(900)
                except Exception:
                    pass

        if saw_exact_follow:
            return False, last_state or "Follow"
        return None, last_state or "no relationship control found"

    except Exception as exc:
        return None, f"profile verification error: {type(exc).__name__}: {str(exc)[:120]}"

    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass




def visible_text_present(page, value: str, timeout_seconds: int = 8) -> bool:
    needle = re.sub(r"\s+", " ", str(value or "")).strip()
    if not needle:
        return False

    # A short distinctive prefix works better when TikTok truncates rendered text.
    probe = needle[:80]
    deadline = time.time() + max(2, int(timeout_seconds))
    while time.time() < deadline:
        try:
            loc = page.get_by_text(probe, exact=False)
            if loc.count():
                for i in range(min(loc.count(), 6)):
                    try:
                        if loc.nth(i).is_visible(timeout=250):
                            return True
                    except Exception:
                        pass
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


def profile_video_urls(context, username: str) -> tuple[bool, set[str]]:
    """
    Snapshot actual /video/ URLs visible on the account profile.
    This is the authoritative post verification source used by v18.
    """
    probe = None
    try:
        probe = context.new_page()
        goto(probe, f"https://www.tiktok.com/@{username.lstrip('@')}", 2500)

        hrefs = probe.locator("a[href*='/video/']").evaluate_all(
            """els => els.map(e => e.href || e.getAttribute('href') || '')
                         .filter(Boolean)"""
        )
        urls = {
            str(href).split("?", 1)[0]
            for href in hrefs
            if "/video/" in str(href)
        }
        return True, urls
    except Exception:
        return False, set()
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


def profile_video_urls_on_page(probe, username: str, reload_page: bool = False) -> tuple[bool, set[str]]:
    try:
        target = f"https://www.tiktok.com/@{username.lstrip('@')}"
        if reload_page:
            try:
                probe.reload(wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            except Exception:
                goto(probe, target, 1200)
        elif not str(probe.url or "").startswith(target):
            goto(probe, target, 1500)

        hrefs = probe.locator("a[href*='/video/']").evaluate_all(
            """els => els.map(e => e.href || e.getAttribute('href') || '')
                         .filter(Boolean)"""
        )
        urls = {
            str(href).split("?", 1)[0]
            for href in hrefs
            if "/video/" in str(href)
        }
        return True, urls
    except Exception:
        return False, set()

def wait_for_new_profile_video(
    context,
    username: str,
    before_urls: set[str],
    timeout_seconds: int = 150,
) -> tuple[bool, str]:
    deadline = time.time() + max(20, int(timeout_seconds))

    while time.time() < deadline:
        ok, current = profile_video_urls(context, username)
        if ok:
            new_urls = current - set(before_urls)
            if new_urls:
                # Prefer the numerically newest TikTok video id if possible.
                ordered = sorted(new_urls, reverse=True)
                return True, ordered[0]
        time.sleep(4)

    return False, ""


def reconcile_pending_uploads(
    page,
    account_id: str,
    history: dict[str, Any],
) -> None:
    pending = [
        item for item in history.get("pending_uploads", [])
        if isinstance(item, dict)
    ]
    if not pending:
        return

    account = find_account(account_id)
    if not account:
        return

    ok, current_urls = profile_video_urls(
        page.context,
        account["username"],
    )
    if not ok:
        return

    now = time.time()
    keep = []

    for item in pending:
        before_urls = set(item.get("before_urls") or [])
        media_id = str(item.get("media_id") or "")
        created_at = float(item.get("created_at") or now)

        if current_urls - before_urls:
            if media_id and media_id not in history.get("posted_ids", []):
                history.setdefault("posted_ids", []).append(media_id)
            add_activity(
                f"{account['username']}: ✅ a previously pending upload is now visible on the profile",
                "good",
            )
            continue

        # Protect against duplicate retry for a day; then release it if TikTok
        # never surfaced a new profile video.
        if now - created_at < 24 * 3600:
            keep.append(item)
        else:
            add_activity(
                f"{account['username']}: pending upload expired after 24h without profile confirmation; media is eligible again",
                "warn",
            )

    history["pending_uploads"] = keep

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


def wait_for_tiktok_overlay_to_clear(
    page,
    account_label: str,
    max_wait_seconds: int = 180,
) -> None:
    """
    Wait for / dismiss non-essential TikTok Studio overlays before interacting.

    For the "Turn on automatic content checks?" prompt, prefer a non-enabling
    dismissal such as Not now / Skip / Maybe later / Close. We do not silently
    turn a TikTok account setting on.
    """
    deadline = time.time() + max_wait_seconds
    last_state = ""

    while time.time() < deadline:
        modal = None
        modal_frame = None
        modal_text = ""

        for frame in page.frames:
            try:
                candidates = frame.locator(
                    "[role='dialog'], .TUXModal-overlay, "
                    "[data-floating-ui-portal], [class*='modal' i]"
                )
                for i in range(min(candidates.count(), 8)):
                    candidate = candidates.nth(i)
                    if not candidate.is_visible(timeout=250):
                        continue
                    modal = candidate
                    modal_frame = frame
                    try:
                        modal_text = (
                            candidate.inner_text(timeout=600) or ""
                        ).strip()
                    except Exception:
                        modal_text = ""
                    break
                if modal is not None:
                    break
            except Exception:
                pass

        if modal is None:
            return

        compact = re.sub(r"\s+", " ", modal_text or "").strip()
        state = compact[:220]
        if state != last_state:
            add_activity(
                f"{account_label}: TikTok modal/overlay detected: "
                f"{state or '[no text]'}",
                "info",
            )
            last_state = state

        low = compact.lower()

        # Non-enabling / non-destructive choices first.
        preferred_names = []
        if "automatic content checks" in low or "turn on automatic content checks" in low:
            preferred_names.extend(
                [
                    "Not now",
                    "Maybe later",
                    "Skip",
                    "No thanks",
                    "Cancel",
                    "Close",
                    "Later",
                ]
            )

        preferred_names.extend(
            [
                "Got it",
                "OK",
                "Okay",
                "Done",
                "Close",
                "Cancel",
                "Skip",
                "Not now",
                "Maybe later",
                "Continue",
            ]
        )

        dismissed = False

        # Try exact labeled buttons inside the dialog/frame.
        for name in preferred_names:
            try:
                btn = modal_frame.get_by_role(
                    "button",
                    name=re.compile(rf"^{re.escape(name)}$", re.I),
                ).last
                if (
                    btn.count()
                    and btn.is_visible(timeout=250)
                    and btn.is_enabled(timeout=250)
                ):
                    btn.click(timeout=2500)
                    add_activity(
                        f"{account_label}: dismissed TikTok modal with '{name}'",
                        "info",
                    )
                    page.wait_for_timeout(700)
                    dismissed = True
                    break
            except Exception:
                pass

        if dismissed:
            continue

        # Try a close/X button by accessible label/title.
        for selector in (
            "button[aria-label*='close' i]",
            "[role='button'][aria-label*='close' i]",
            "button[title*='close' i]",
            "[data-e2e*='close' i]",
        ):
            try:
                btn = modal.locator(selector).first
                if (
                    btn.count()
                    and btn.is_visible(timeout=250)
                    and btn.is_enabled(timeout=250)
                ):
                    btn.click(timeout=2500)
                    add_activity(
                        f"{account_label}: closed TikTok modal",
                        "info",
                    )
                    page.wait_for_timeout(700)
                    dismissed = True
                    break
            except Exception:
                pass

        if dismissed:
            continue

        # Escape is a safe final attempt for dismissible overlays.
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
            still_open = False
            try:
                still_open = modal.is_visible(timeout=300)
            except Exception:
                still_open = False
            if not still_open:
                add_activity(
                    f"{account_label}: dismissed TikTok modal with Escape",
                    "info",
                )
                continue
        except Exception:
            pass

        page.wait_for_timeout(1000)

    raise RuntimeError(
        f"TikTok modal/overlay did not clear within {max_wait_seconds}s: "
        f"{last_state or 'unknown modal'}"
    )



def advance_tiktok_upload_if_needed(page, account_label: str, max_wait_seconds: int = 90):
    """Move from TikTok's media-upload step to the caption/post step when needed.

    TikTok Studio has changed its upload flow several times. Some sessions expose
    the caption editor immediately after media upload; others first show a Next /
    Continue action. This helper only clicks an exact, visible, enabled navigation
    button when no caption editor is currently detectable.
    """
    deadline = time.time() + max_wait_seconds
    clicked = False
    last_state = ""
    while time.time() < deadline:
        # If an editor already exists, do not touch navigation.
        try:
            for frame in page.frames:
                if frame.locator("[contenteditable='true'], textarea, [role='textbox']").count():
                    return
        except Exception:
            pass

        body_text = ""
        try:
            body_text = page.locator("body").inner_text(timeout=1500)
        except Exception:
            pass
        compact = re.sub(r"\\s+", " ", body_text or "")[:500]
        if compact != last_state:
            last_state = compact
            low = compact.lower()
            if any(x in low for x in ("uploading", "processing", "preparing", "checking")):
                add_activity(f"{account_label}: TikTok is still processing media; waiting before advancing", "info")

        # Only exact navigation labels. Avoid broad text selectors that could
        # click an unrelated TikTok control.
        for frame in page.frames:
            for label in ("Next", "Continue"):
                try:
                    loc = frame.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I)).last
                    if loc.count() and loc.is_visible(timeout=400) and loc.is_enabled(timeout=400):
                        box = loc.bounding_box()
                        if box and box.get("width", 0) > 30 and box.get("height", 0) > 20:
                            loc.click(timeout=5000)
                            add_activity(f"{account_label}: advanced TikTok upload with {label}", "info")
                            clicked = True
                            page.wait_for_timeout(1500)
                            return
                except Exception:
                    pass
        page.wait_for_timeout(1000)
    if clicked:
        return


def write_tiktok_editor_debug_snapshot(page, account_label: str):
    """Save a local screenshot/HTML snapshot when the editor cannot be located."""
    try:
        debug_dir = Path.home() / ".local" / "share" / "tiktok_dashboard" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", account_label)
        shot = debug_dir / f"editor_{safe}_{stamp}.png"
        html = debug_dir / f"editor_{safe}_{stamp}.html"
        page.screenshot(path=str(shot), full_page=True, timeout=10000)
        html.write_text(page.content(), encoding="utf-8", errors="ignore")
        add_activity(f"{account_label}: saved TikTok editor debug snapshot: {shot}", "warn")
    except Exception as exc:
        add_activity(f"{account_label}: could not save editor debug snapshot: {exc}", "warn")


def _read_caption_editor_value(editor) -> str:
    for reader in (
        lambda: editor.input_value(timeout=1200),
        lambda: editor.inner_text(timeout=1200),
        lambda: editor.text_content(timeout=1200),
    ):
        try:
            value = str(reader() or "").strip()
            if value:
                return value
        except Exception:
            pass
    return ""


def enter_tiktok_caption(
    page,
    editor,
    full_caption: str,
    account_label: str,
) -> str:
    """
    Enter text into TikTok Studio's DraftJS/Lexical/contenteditable editor.

    The key difference from the old implementation is that this does NOT require
    a successful pointer click. TikTok frequently leaves a transparent/modal
    layer over an otherwise-ready role=combobox editor.
    """
    expected = full_caption.strip()
    if not expected:
        raise RuntimeError("Generated caption is empty.")

    # Give transient overlays one more chance to clear.
    try:
        wait_for_tiktok_overlay_to_clear(page, account_label, 20)
    except Exception as overlay_exc:
        add_activity(
            f"{account_label}: caption editor still has an overlay; "
            "trying focus-based entry instead of mouse click",
            "warn",
        )

    errors = []

    # Strategy 1: locator.focus + keyboard. focus() avoids pointer hit testing.
    try:
        editor.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass

    try:
        editor.focus(timeout=5000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")

        try:
            editor.fill(expected, timeout=8000)
        except Exception:
            # insert_text is much faster and more reliable than one key at a time.
            page.keyboard.insert_text(expected)

        page.wait_for_timeout(500)
        entered = _read_caption_editor_value(editor)
        if expected[:20].lower() in entered.lower():
            return entered
        errors.append("focus/keyboard entry did not verify")
    except Exception as exc:
        errors.append(f"focus/keyboard: {type(exc).__name__}: {exc}")

    # Strategy 2: direct DOM focus + input dispatch. Useful for DraftJS comboboxes.
    try:
        editor.evaluate(
            """(el, value) => {
                el.focus();

                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(el);
                sel.removeAllRanges();
                sel.addRange(range);

                // Prefer browser editing APIs so React/DraftJS sees input-like changes.
                try {
                    document.execCommand('delete', false, null);
                } catch (e) {}

                try {
                    document.execCommand('insertText', false, value);
                } catch (e) {
                    el.textContent = value;
                }

                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    inputType: 'insertText',
                    data: value
                }));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            expected,
        )
        page.wait_for_timeout(700)
        entered = _read_caption_editor_value(editor)
        if expected[:20].lower() in entered.lower():
            return entered
        errors.append("DOM input dispatch did not verify")
    except Exception as exc:
        errors.append(f"DOM input: {type(exc).__name__}: {exc}")

    # Strategy 3: force-focus with JS, then real keyboard typing. Slowest fallback.
    try:
        editor.evaluate("(el) => el.focus()")
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(expected)
        page.wait_for_timeout(500)
        entered = _read_caption_editor_value(editor)
        if expected[:20].lower() in entered.lower():
            return entered
        errors.append("forced-focus keyboard entry did not verify")
    except Exception as exc:
        errors.append(f"forced-focus: {type(exc).__name__}: {exc}")

    raise RuntimeError(
        "Caption editor was found but text entry could not be verified. "
        + " | ".join(errors[-3:])
    )

def find_ready_caption_editor(page, max_wait_seconds: int = 240):
    """Find TikTok Studio's caption editor, including the legacy upload iframe."""
    selectors = (
        # Current TikTok Studio commonly exposes DraftJS as a combobox.
        "div[role='combobox'][contenteditable='true']",
        "div[class*='public-DraftEditor-content'][contenteditable='true']",
        "div[class*='public-DraftEditor-content']",
        "[data-lexical-editor='true']",
        "[contenteditable='plaintext-only']",
        "div[contenteditable='true']",
        "textarea",
        "[role='textbox']",
        "[aria-label*='caption' i]",
        "[aria-label*='description' i]",
        "[placeholder*='caption' i]",
        "[placeholder*='description' i]",
        "[data-e2e*='caption' i]",
        "[data-e2e*='post-content' i]",
    )
    deadline = time.time() + max_wait_seconds
    last_diag = 0.0

    def candidate_frames():
        """Yield normal frames plus an explicit TikTok upload iframe frame."""
        seen = set()
        for fr in page.frames:
            key = id(fr)
            if key not in seen:
                seen.add(key)
                yield fr
        try:
            iframe_loc = page.locator("iframe[data-tt='Upload_index_iframe']").first
            if iframe_loc.count():
                fr = iframe_loc.content_frame
                if fr:
                    key = id(fr)
                    if key not in seen:
                        yield fr
        except Exception:
            pass

    while time.time() < deadline:
        remaining = max(2, min(8, int(deadline - time.time())))
        try:
            wait_for_tiktok_overlay_to_clear(page, "upload", remaining)
        except Exception:
            pass

        # TikTok Studio has historically rendered the uploader inside
        # iframe[data-tt="Upload_index_iframe"]. Check that explicitly first,
        # then fall back to every frame Playwright currently exposes.
        frames = list(candidate_frames())
        for frame in frames:
            for selector in selectors:
                try:
                    locs = frame.locator(selector)
                    count = min(locs.count(), 12)
                    for i in range(count):
                        loc = locs.nth(i)
                        if not loc.is_visible(timeout=250):
                            continue
                        box = loc.bounding_box()
                        if not box or box.get("width", 0) <= 20 or box.get("height", 0) <= 10:
                            continue
                        try:
                            if not loc.is_enabled(timeout=250):
                                continue
                        except Exception:
                            pass
                        try:
                            if loc.is_editable(timeout=250):
                                return loc
                        except Exception:
                            pass
                        # TikTok's React editor can briefly report non-editable
                        # while its state is settling. Caption-labelled controls
                        # are safe fallback candidates.
                        low = selector.lower()
                        if any(k in low for k in ("caption", "description", "post-content")):
                            return loc
                except Exception:
                    pass

        now = time.time()
        if now - last_diag >= 8:
            last_diag = now
            try:
                summary = []
                for frame in frames:
                    for sel in (
                        "div[class*='public-DraftEditor-content']",
                        "[contenteditable='true']",
                        "[data-lexical-editor='true']",
                        "textarea",
                        "[role='textbox']",
                    ):
                        try:
                            n = frame.locator(sel).count()
                            if n:
                                summary.append(f"{sel}={n}")
                        except Exception:
                            pass
                iframe_count = 0
                try:
                    iframe_count = page.locator("iframe[data-tt='Upload_index_iframe']").count()
                except Exception:
                    pass
                add_activity(
                    f"TikTok editor scan: frames={len(frames)}, upload_iframe={iframe_count}, "
                    f"candidates={', '.join(summary) or 'none'}",
                    "info",
                )
            except Exception:
                pass
        page.wait_for_timeout(750)
    return None


def find_tiktok_upload_input(page, max_wait_seconds: int = 60):
    """
    Find TikTok Studio's upload file input, including iframe variants.
    File inputs are usually hidden, so visibility is NOT required.
    """
    deadline = time.time() + max(5, int(max_wait_seconds))
    while time.time() < deadline:
        frames = list(page.frames)

        # Explicit historical TikTok uploader iframe.
        try:
            iframe = page.locator("iframe[data-tt='Upload_index_iframe']").first
            if iframe.count():
                frame = iframe.content_frame
                if frame and frame not in frames:
                    frames.append(frame)
        except Exception:
            pass

        for frame in frames:
            for selector in (
                "input[type='file'][accept*='video']",
                "input[type='file'][accept*='image']",
                "input[type='file']",
            ):
                try:
                    loc = frame.locator(selector).first
                    if loc.count():
                        return loc
                except Exception:
                    pass

        page.wait_for_timeout(750)

    return None


def detect_tiktok_studio_error(page) -> str:
    """
    Return a visible TikTok Studio error/warning message when one is present.
    This is diagnostic only; it does not bypass TikTok checks.
    """
    phrases = (
        "something went wrong",
        "failed to upload",
        "upload failed",
        "couldn't upload",
        "could not upload",
        "unsupported file",
        "unsupported format",
        "video format",
        "video is too short",
        "video is too long",
        "file is too large",
        "try again later",
        "network error",
        "processing failed",
        "unable to process",
    )

    candidates = []
    for frame in page.frames:
        for selector in (
            "[role='alert']",
            "[role='dialog']",
            "[class*='error' i]",
            "[class*='Error']",
            "[data-e2e*='error' i]",
        ):
            try:
                locs = frame.locator(selector)
                for i in range(min(locs.count(), 8)):
                    loc = locs.nth(i)
                    if not loc.is_visible(timeout=200):
                        continue
                    value = re.sub(
                        r"\s+",
                        " ",
                        loc.inner_text(timeout=500) or "",
                    ).strip()
                    if value:
                        candidates.append(value)
            except Exception:
                pass

    # Fallback body scan for TikTok banners that are not marked as alerts.
    try:
        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=1000) or "",
        )
        lower = body.lower()
        for phrase in phrases:
            pos = lower.find(phrase)
            if pos >= 0:
                start = max(0, pos - 120)
                end = min(len(body), pos + 300)
                candidates.append(body[start:end].strip())
                break
    except Exception:
        pass

    for value in candidates:
        low = value.lower()
        if any(phrase in low for phrase in phrases):
            return value[:500]
    return ""


def wait_for_tiktok_media_attached(
    page,
    account_label: str,
    max_wait_seconds: int = 90,
) -> None:
    """
    Wait until TikTok acknowledges the selected media enough to expose the
    next/editor stage, or raise the real Studio error.
    """
    deadline = time.time() + max(10, int(max_wait_seconds))
    last_log = 0.0

    while time.time() < deadline:
        error = detect_tiktok_studio_error(page)
        if error:
            write_tiktok_editor_debug_snapshot(page, account_label)
            raise RuntimeError(f"TikTok Studio rejected the media: {error}")

        # Caption editor / Next / Continue are all evidence that the selected
        # file reached TikTok's upload workflow.
        for frame in page.frames:
            try:
                if frame.locator(
                    "div[class*='public-DraftEditor-content'], "
                    "[data-lexical-editor='true'], "
                    "[contenteditable='plaintext-only'], "
                    "[aria-label*='caption' i], "
                    "[aria-label*='description' i]"
                ).count():
                    return
            except Exception:
                pass

            for label in ("Next", "Continue"):
                try:
                    btn = frame.get_by_role(
                        "button",
                        name=re.compile(rf"^{label}$", re.I),
                    ).last
                    if (
                        btn.count()
                        and btn.is_visible(timeout=250)
                        and btn.is_enabled(timeout=250)
                    ):
                        return
                except Exception:
                    pass

        now = time.time()
        if now - last_log >= 10:
            last_log = now
            add_activity(
                f"{account_label}: waiting for TikTok Studio to accept/process the selected media",
                "info",
            )

        page.wait_for_timeout(800)

    error = detect_tiktok_studio_error(page)
    if error:
        raise RuntimeError(f"TikTok Studio rejected the media: {error}")
    raise RuntimeError(
        "TikTok Studio did not acknowledge the selected media within "
        f"{max_wait_seconds} seconds."
    )

def _submission_signal(page, post_locator, pre_url: str) -> tuple[bool, str]:
    """
    Detect only immediate UI evidence that the Post control reacted.

    This is NOT final publication confirmation. v18 requires a new /video/ URL
    on the account profile before recording a successful post.
    """
    try:
        current_url = page.url
    except Exception:
        current_url = ""

    if current_url and current_url != pre_url:
        return True, f"url changed to {current_url}"

    try:
        if not post_locator.count():
            return True, "post button detached"
        if not post_locator.is_visible(timeout=300):
            return True, "post button hidden"
        aria_disabled = str(
            post_locator.get_attribute("aria-disabled") or ""
        ).lower()
        if aria_disabled == "true":
            return True, "post button became disabled"
    except Exception:
        return True, "post button remounted"

    return False, ""



def activate_tiktok_post_button(
    page,
    post_locator,
    account_label: str,
    pre_url: str,
) -> str:
    """
    Activate TikTok's Post/Publish control without relying on pointer hit testing.

    Strategy order:
      1) focus + Enter
      2) direct DOM click
      3) Playwright force click

    After every attempt, look for a submission signal before trying another
    method so an already-started upload is not accidentally double-submitted.
    """
    try:
        wait_for_tiktok_overlay_to_clear(page, account_label, 20)
    except Exception:
        add_activity(
            f"{account_label}: overlay did not fully clear before Post; "
            "using non-pointer activation fallbacks",
            "warn",
        )

    try:
        disabled = post_locator.get_attribute("disabled")
        aria_disabled = post_locator.get_attribute("aria-disabled")
        if disabled is not None or str(aria_disabled).lower() == "true":
            raise RuntimeError(
                f"Post button is still disabled "
                f"(disabled={disabled!r}, aria-disabled={aria_disabled!r})"
            )
    except RuntimeError:
        raise
    except Exception:
        pass

    try:
        post_locator.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass

    attempts = []

    # 1) Focus + Enter avoids overlay pointer interception.
    try:
        post_locator.focus(timeout=5000)
        page.keyboard.press("Enter")
        attempts.append("focus+Enter")
        page.wait_for_timeout(2500)
        started, why = _submission_signal(page, post_locator, pre_url)
        if started:
            add_activity(
                f"{account_label}: Post activated with keyboard ({why})",
                "good",
            )
            return "focus+Enter"
    except Exception as exc:
        attempts.append(f"focus+Enter failed: {type(exc).__name__}: {exc}")

    # 2) Native element click bypasses Playwright pointer-actionability checks.
    try:
        post_locator.evaluate("(el) => el.click()")
        attempts.append("DOM click")
        page.wait_for_timeout(2500)
        started, why = _submission_signal(page, post_locator, pre_url)
        if started:
            add_activity(
                f"{account_label}: Post activated with DOM click ({why})",
                "good",
            )
            return "DOM click"
    except Exception as exc:
        attempts.append(f"DOM click failed: {type(exc).__name__}: {exc}")

    # 3) Last resort: Playwright force click. Use only after overlays were handled.
    try:
        post_locator.click(timeout=5000, force=True)
        attempts.append("force click")
        page.wait_for_timeout(2500)
        started, why = _submission_signal(page, post_locator, pre_url)
        if started:
            add_activity(
                f"{account_label}: Post activated with force click ({why})",
                "good",
            )
            return "force click"
    except Exception as exc:
        attempts.append(f"force click failed: {type(exc).__name__}: {exc}")

    write_tiktok_editor_debug_snapshot(page, account_label)
    raise RuntimeError(
        "Post button was found/enabled but no activation method produced a "
        "submission signal. " + " | ".join(attempts[-4:])
    )


def wait_for_tiktok_post_confirmation(
    page,
    account_label: str,
    pre_url: str,
    before_profile_urls: set[str],
    timeout_seconds: int = 300,
) -> tuple[bool, str]:
    """
    Confirm a post only when a genuinely new /video/ URL appears on the real
    account profile. A single verifier tab is reused to reduce browser churn.
    """
    account_username = account_label.lstrip("@")
    deadline = time.time() + max(30, int(timeout_seconds))
    last_log = 0.0
    last_refresh = 0.0
    probe = None

    try:
        probe = page.context.new_page()
        goto(
            probe,
            f"https://www.tiktok.com/@{account_username}",
            1600,
        )

        while time.time() < deadline:
            studio_error = detect_tiktok_studio_error(page)
            if studio_error:
                write_tiktok_editor_debug_snapshot(page, account_label)
                raise RuntimeError(
                    f"TikTok Studio error after Post: {studio_error}"
                )

            now = time.time()
            reload_probe = now - last_refresh >= 5.0
            ok, current = profile_video_urls_on_page(
                probe,
                account_username,
                reload_page=reload_probe,
            )
            if reload_probe:
                last_refresh = now

            if ok:
                new_urls = current - set(before_profile_urls)
                if new_urls:
                    final_url = sorted(new_urls, reverse=True)[0]
                    add_activity(
                        f"{account_label}: ✅ new profile video confirmed: {final_url}",
                        "good",
                    )
                    return True, final_url

            if now - last_log >= 15:
                remaining = max(0, int(deadline - now))
                add_activity(
                    f"{account_label}: Post submitted; waiting for a new profile "
                    f"video to appear ({remaining}s verification window remaining)",
                    "info",
                )
                last_log = now

            page.wait_for_timeout(1000)

        return False, ""

    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass




def execute_upload(
    page,
    account_id: str,
    history: dict[str, Any],
    manual: bool = False,
) -> int:
    """Perform one complete automatic upload from selection through Post.

    Manual upload bypasses the background feature switch, while automated
    uploads still require enable_posts. The media picker chooses a random
    individual file rather than consuming an entire folder at once.
    """
    account = find_account(account_id)
    if not account:
        return 0
    settings = get_settings(account_id)
    if not manual and not settings.get("enable_posts", False):
        add_activity(
            f"{account['username']}: upload task skipped because Posts is disabled",
            "warn",
        )
        return 0

    # Check upload availability before file selection / frame extraction / Ollama.
    # This prevents Auto from spending several minutes on vision when the upload
    # cooldown or rolling write budget already says the post cannot run.
    early_ok, early_reason = can_write_now(account_id, "upload")
    if not early_ok:
        add_activity(
            f"{account['username']}: upload skipped before AI analysis: {early_reason}",
            "info",
        )
        return 0

    reconcile_pending_uploads(page, account_id, history)

    folders = discover_media_folders()
    chosen = choose_random_unused_media(account_id, folders)
    if not chosen:
        add_activity(
            f"{account['username']}: media pool has no unused supported files",
            "warn",
        )
        return 0

    selected_folder, source_media = chosen
    media_id = media_key(source_media)
    add_activity(
        f"{account['username']}: selected media {source_media.name} "
        f"from {selected_folder['path']}",
        "info",
    )

    upload_path = (
        source_media
        if source_media.suffix.lower() in SUPPORTED_VIDEOS
        else image_to_video(source_media)
    )
    add_activity(
        f"{account['username']}: preparing upload page before AI analysis",
        "info",
    )

    # Navigate first. Previously the worker opened Chromium at about:blank and
    # then performed potentially slow video-frame/Ollama work. That made the
    # browser look frozen even though Python was busy elsewhere. Keeping the
    # TikTok uploader visible makes the workflow observable and also gives the
    # site time to initialize while local AI work runs.
    goto(page, TIKTOK_UPLOAD, 3000)
    add_activity(
        f"{account['username']}: TikTok upload page loaded at {page.url}",
        "info",
    )
    if explicit_login_button_visible(page) or "/login" in page.url.lower():
        raise RuntimeError("TikTok login/session expired.")

    if pause_for_manual_tiktok_challenge(page, account_id):
        return 0

    # v14 selected a local media path but never attached it to TikTok Studio's
    # actual file input. That left the editor visible with no valid upload.
    file_input = find_tiktok_upload_input(page, 60)
    if file_input is None:
        write_tiktok_editor_debug_snapshot(page, account["username"])
        raise RuntimeError(
            "TikTok Studio upload input was not found. "
            "The Studio layout may have changed."
        )

    add_activity(
        f"{account['username']}: attaching {upload_path.name} to TikTok Studio",
        "info",
    )
    try:
        file_input.set_input_files(str(upload_path))
    except Exception as exc:
        write_tiktok_editor_debug_snapshot(page, account["username"])
        raise RuntimeError(
            f"TikTok Studio could not attach the media file: {exc}"
        ) from exc

    # Confirm the browser received a filename when the DOM exposes it.
    try:
        selected_value = str(file_input.input_value(timeout=1500) or "")
        if selected_value:
            add_activity(
                f"{account['username']}: browser file input accepted {Path(selected_value).name}",
                "good",
            )
    except Exception:
        pass

    wait_for_tiktok_media_attached(
        page,
        account["username"],
        120,
    )

    sidecar = sidecar_context(selected_folder["path"])
    add_activity(
        f"{account['username']}: starting media analysis with "
        f"{vision_model() or 'no vision model'}",
        "info",
    )
    analysis = analyze_media_sequence(
        source_media,
        settings,
        sidecar=sidecar,
    )
    if not analysis["ok"]:
        add_activity(
            f"{account['username']}: skipped {source_media.name}: {analysis['reason']}",
            "warn",
        )
        return 0

    if analysis.get("visual_available"):
        add_activity(
            f"{account['username']}: 👁️ analyzed {source_media.name} using "
            f"{analysis['model'] or 'no vision model'}; frames={analysis['frames']}",
            "info",
        )
    else:
        add_activity(
            f"{account['username']}: ⚠️ vision unavailable for {source_media.name}; "
            f"continuing with sidecar/fallback context",
            "warn",
        )

    add_activity(
        f"{account['username']}: generating caption/tags with {OLLAMA_MODEL} "
        "(waiting for complete model response)",
        "info",
    )
    generated = generate_caption_and_tags(account_id, analysis)
    if not generated:
        add_activity(
            f"{account['username']}: caption generation failed for {source_media.name}; upload skipped",
            "warn",
        )
        return 0

    caption, tags = generated
    full_caption = f"{caption}\n{' '.join(tags)}".strip()
    add_activity(
        f"{account['username']}: caption ready for {source_media.name}: {caption[:100]}",
        "info",
    )

    # Manual actions should run immediately unless the account is genuinely
    # inside the rolling write limit. Background automation remains paced.
    ok_slot, slot_reason = can_write_now(account_id, "upload")
    if not ok_slot:
        add_activity(
            f"{account['username']}: upload deferred by pacing: {slot_reason}",
            "warn",
        )
        return 0

    add_activity(
        f"{account['username']}: returning to prepared TikTok upload for {source_media.name}",
        "info",
    )

    studio_error = detect_tiktok_studio_error(page)
    if studio_error:
        write_tiktok_editor_debug_snapshot(page, account["username"])
        raise RuntimeError(f"TikTok Studio error before editor: {studio_error}")

    # TikTok can display a processing modal over the editor, and some current
    # Studio sessions require an explicit Next/Continue transition after upload.
    wait_for_tiktok_overlay_to_clear(page, account["username"], 240)
    advance_tiktok_upload_if_needed(page, account["username"], 90)
    add_activity(f"{account['username']}: waiting for TikTok caption editor to become interactable", "info")
    editor = find_ready_caption_editor(page, 180)
    if editor is None:
        write_tiktok_editor_debug_snapshot(page, account["username"])
        raise RuntimeError("TikTok upload screen never exposed an interactable caption editor after the file was selected.")

    try:
        entered = enter_tiktok_caption(
            page,
            editor,
            full_caption,
            account["username"],
        )
        if caption.strip()[:20].lower() not in entered.lower():
            raise RuntimeError(
                "Caption editor did not contain the generated caption after entry."
            )
    except Exception as exc:
        write_tiktok_editor_debug_snapshot(page, account["username"])
        raise RuntimeError(
            f"Could not enter generated caption: {exc}"
        ) from exc

    add_activity(f"{account['username']}: caption + hashtags entered; waiting for Post", "info")

    wait_for_tiktok_overlay_to_clear(page, account["username"], 120)
    add_activity(f"{account['username']}: caption verified; waiting for Post button to become enabled", "info")

    post = None
    post_patterns = (
        re.compile(r"^Post$", re.I),
        re.compile(r"^Publish$", re.I),
    )
    deadline = time.time() + 180
    while time.time() < deadline:
        studio_error = detect_tiktok_studio_error(page)
        if studio_error:
            write_tiktok_editor_debug_snapshot(page, account["username"])
            raise RuntimeError(
                f"TikTok Studio error while waiting for Post: {studio_error}"
            )

        for frame in page.frames:
            for pattern in post_patterns:
                try:
                    loc = frame.get_by_role("button", name=pattern).last
                    if loc.count() and loc.is_visible(timeout=500):
                        post = loc
                        break
                except Exception:
                    pass
            if post is None:
                for text in ("Post", "Publish"):
                    try:
                        loc = frame.locator(f"button:has-text('{text}')").last
                        if loc.count() and loc.is_visible(timeout=500):
                            post = loc
                            break
                    except Exception:
                        pass
            if post is not None:
                break
        if post is not None:
            try:
                if post.is_enabled(timeout=500):
                    break
            except Exception:
                pass
        post = None
        page.wait_for_timeout(1000)

    if post is None:
        raise RuntimeError("TikTok Post/Publish button never became visible and enabled.")

    wait_for_tiktok_overlay_to_clear(page, account["username"], 30)
    page.wait_for_timeout(700)
    pre_url = page.url

    profile_snapshot_ok, before_profile_urls = profile_video_urls(
        page.context,
        account["username"],
    )
    if not profile_snapshot_ok:
        raise RuntimeError(
            "Could not snapshot the account profile before posting, so v18 "
            "refuses to claim a successful post without verifiable before/after state."
        )

    activation_method = activate_tiktok_post_button(
        page,
        post,
        account["username"],
        pre_url,
    )
    add_activity(
        f"{account['username']}: Post submission started via {activation_method}; confirming upload",
        "info",
    )

    confirmed, final_url = wait_for_tiktok_post_confirmation(
        page,
        account["username"],
        pre_url,
        before_profile_urls,
        timeout_seconds=300,
    )

    if not confirmed:
        write_tiktok_editor_debug_snapshot(page, account["username"])
        history.setdefault("pending_uploads", []).append({
            "media_id": media_id,
            "before_urls": sorted(before_profile_urls),
            "created_at": time.time(),
            "source": str(source_media),
            "caption": full_caption[:500],
        })
        history["pending_uploads"] = history["pending_uploads"][-30:]
        register_action_verification(
            account_id,
            "post",
            False,
            "Post was activated but no new /video/ URL appeared on the account profile.",
        )
        raise RuntimeError(
            "Post activation was sent, but no new video appeared on the actual "
            "TikTok profile. The media is marked pending and will not be retried "
            "immediately, preventing duplicate posts."
        )

    register_action_verification(account_id, "post", True)
    record_write(account_id, "upload")

    # Store the individual file key so the remaining media in the same folder
    # stays eligible for future runs. Keep old folder IDs untouched for history compatibility.
    history.setdefault("posted_ids", []).append(media_id)
    record_recent_post(
        account_id,
        source_media,
        full_caption,
        final_url if "/video/" in final_url else "",
    )
    add_activity(
        f"{account['username']}: ✅ uploaded {source_media.name} with generated caption/hashtags",
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
    if not settings.get("enable_dms", False):
        return 0

    sent = 0
    per_pass_limit = max(1, min(MAX_DM_REPLIES_PER_PASS_DEFAULT, int(settings["max_writes_per_workflow"])))
    history.setdefault("dm_thread_last_reply_at", {})
    goto(page, "https://www.tiktok.com/messages", 2200)
    if pause_for_manual_tiktok_challenge(page, account_id):
        return 0

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
        add_activity(
            f"{account['username']}: DMs enabled, but no recognizable TikTok message list was found",
            "warn",
        )
        return 0

    for index in range(min(4, threads.count())):
        if not get_settings(account_id).get("enable_dms", False):
            break
        if sent >= min(per_pass_limit, int(settings["max_writes_per_workflow"])):
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
        last_thread_reply_at = float(history.get("dm_thread_last_reply_at", {}).get(thread_key, 0.0) or 0.0)
        if last_thread_reply_at and time.time() - last_thread_reply_at < DM_THREAD_REPLY_COOLDOWN_SECONDS:
            continue
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

        maybe_human_delay(1.0)
        editor = page.locator("div[contenteditable='true']").last
        if not editor.count():
            continue
        before_count = bubbles.count()
        editor.fill(reply)
        page.keyboard.press("Enter")
        page.wait_for_timeout(900)

        confirmed = False
        deadline = time.time() + 8
        while time.time() < deadline:
            refreshed = page.locator(
                "[data-e2e='chat-message'], [class*='ChatBubble']"
            )
            try:
                if refreshed.count() > before_count:
                    recent_texts = []
                    for i in range(max(0, refreshed.count() - 4), refreshed.count()):
                        try:
                            recent_texts.append(
                                refreshed.nth(i).inner_text(timeout=600).strip()
                            )
                        except Exception:
                            pass
                    if any(
                        normalize_reply(reply) == normalize_reply(value)
                        for value in recent_texts
                    ):
                        confirmed = True
                        break
            except Exception:
                pass
            page.wait_for_timeout(500)

        if not confirmed:
            register_action_verification(
                account_id,
                "dm",
                False,
                "Reply text never appeared as a new message bubble.",
            )
            continue

        register_action_verification(account_id, "dm", True)
        record_write(account_id, "dm")

        history["replied_dms"].append(incoming_key)
        history["replied_dms"] = history["replied_dms"][-5000:]
        history["sent_dm_texts"].append(reply)
        history["sent_dm_texts"] = history["sent_dm_texts"][-200:]
        prior.append(reply)
        history["dm_reply_memory"][thread_key] = prior[-20:]
        history["dm_thread_last_reply_at"][thread_key] = time.time()

        sent += 1
        add_activity(
            f"{account['username']}: ✅ DM reply confirmed: {reply[:110]}",
            "good",
        )

    return sent


def handle_comments(
    page,
    account_id: str,
    history: dict[str, Any],
    manual: bool = False,
) -> int:
    account = find_account(account_id)
    settings = get_settings(account_id)
    if not manual and not settings.get("enable_comments", False):
        return 0

    username = account["username"].lstrip("@")
    goto(page, f"https://www.tiktok.com/@{username}", 2200)
    if pause_for_manual_tiktok_challenge(page, account_id):
        return 0
    first_video = page.locator(
        "[data-e2e='user-post-item'], a[href*='/video/']"
    ).first
    if not first_video.count():
        add_activity(
            f"{account['username']}: comments requested, but no visible account video was found",
            "warn",
        )
        return 0
    first_video.click(timeout=3000)
    page.wait_for_timeout(1600)

    comments = page.locator("[data-e2e='comment-item']")
    sent = 0
    per_pass_limit = max(
        1,
        min(
            MAX_COMMENT_REPLIES_PER_PASS_DEFAULT,
            int(settings["max_writes_per_workflow"]),
        ),
    )

    for index in range(min(5, comments.count())):
        if not manual and not get_settings(account_id).get("enable_comments", False):
            break
        if sent >= per_pass_limit:
            break

        container = comments.nth(index)
        try:
            comment_text = container.locator(
                "[data-e2e='comment-text']"
            ).first.inner_text(timeout=1800).strip()
            user = container.locator(
                "[data-e2e='comment-username']"
            ).first.inner_text(timeout=1800).strip()
        except Exception:
            continue

        key = stable_key("comment", user, comment_text)
        if not comment_text or key in history["replied_comments"]:
            continue

        reply = generate_reply(
            account_id,
            "comment",
            comment_text,
            prior=[],
        )
        if not reply:
            continue

        if not wait_for_write_slot(account_id, "comment"):
            break

        maybe_human_delay(0.6)
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
            activate_once_dom(
                post,
                page,
                account["username"],
                "comment Post",
            )
        else:
            page.keyboard.press("Enter")

        confirmed = visible_text_present(page, reply, 8)
        if not confirmed:
            register_action_verification(
                account_id,
                "comment",
                False,
                f"Reply to @{user} did not appear in the visible comment thread.",
            )
            continue

        register_action_verification(account_id, "comment", True)
        record_write(account_id, "comment")
        history["replied_comments"].append(key)
        history["replied_comments"] = history["replied_comments"][-5000:]
        sent += 1
        add_activity(
            f"{account['username']}: ✅ comment reply confirmed to @{user}: {reply[:110]}",
            "good",
        )

    return sent



def find_like_control(page):
    return _find_visible_control(
        page,
        (
            "button[data-e2e='browse-like']",
            "button:has(span[data-e2e='browse-like-icon'])",
            "button:has(span[data-e2e='like-icon'])",
        ),
    )


def find_favorite_control(page):
    return _find_visible_control(
        page,
        (
            "button:has(span[data-e2e='favorites-icon'])",
            "button:has(span[data-e2e='undefined-icon'])",
            "button[aria-label*='favorite' i]",
            "button[aria-label*='save' i]",
        ),
    )


def find_share_control(page):
    return _find_visible_control(
        page,
        (
            "button:has(span[data-e2e='share-icon'])",
            "button[data-e2e='share-icon']",
            "[role='button']:has(span[data-e2e='share-icon'])",
        ),
    )


def find_direct_repost_control(page):
    return _find_visible_control(
        page,
        (
            "a[data-e2e='video-share-repost']",
            "button[data-e2e='video-share-repost']",
        ),
    )


def current_tiktok_clip_identity(page) -> str:
    pieces = []

    try:
        pieces.append(str(page.url or ""))
    except Exception:
        pass

    try:
        videos = page.locator("video")
        for i in range(min(videos.count(), 8)):
            video = videos.nth(i)
            try:
                if not video.is_visible(timeout=150):
                    continue
            except Exception:
                continue

            try:
                current_src = video.evaluate(
                    "el => el.currentSrc || el.src || el.getAttribute('src') || ''"
                )
            except Exception:
                current_src = ""
            try:
                poster = video.get_attribute("poster") or ""
            except Exception:
                poster = ""

            if current_src or poster:
                pieces.extend([str(current_src), str(poster)])
                break
    except Exception:
        pass

    try:
        links = page.locator("a[href*='/video/']")
        for i in range(min(links.count(), 12)):
            link = links.nth(i)
            try:
                if not link.is_visible(timeout=100):
                    continue
                href = link.get_attribute("href") or ""
                if href:
                    pieces.append(href)
                    break
            except Exception:
                pass
    except Exception:
        pass

    return stable_key("clip-identity", *pieces) if pieces else stable_key(
        "clip-identity",
        str(time.time_ns()),
    )

def advance_to_next_tiktok_clip(page, account_id: str, account_label: str) -> bool:
    pause_for_manual_tiktok_challenge(page, account_id)
    enforce_tiktok_action_restriction(page, account_id, "engagement")

    old_identity = current_tiktok_clip_identity(page)

    next_button = _find_visible_control(
        page,
        (
            "button[data-e2e='arrow-right']",
            "button[aria-label*='next' i]",
        ),
    )

    try:
        if next_button is not None:
            activate_once_dom(
                next_button,
                page,
                account_label,
                "Next video",
            )
        else:
            page.keyboard.press("ArrowDown")
    except Exception:
        return False

    deadline = time.time() + 8
    while time.time() < deadline:
        pause_for_manual_tiktok_challenge(page, account_id)
        if current_tiktok_clip_identity(page) != old_identity:
            add_activity(
                f"{account_label}: advanced to next TikTok clip",
                "info",
            )
            return True
        page.wait_for_timeout(450)

    try:
        page.keyboard.press("ArrowDown")
        deadline = time.time() + 4
        while time.time() < deadline:
            pause_for_manual_tiktok_challenge(page, account_id)
            if current_tiktok_clip_identity(page) != old_identity:
                add_activity(
                    f"{account_label}: advanced to next clip with ArrowDown fallback",
                    "info",
                )
                return True
            page.wait_for_timeout(400)
    except Exception:
        pass

    return False


def engage_hashtag(
    page,
    account_id: str,
    history: dict[str, Any],
    manual: bool = False,
) -> int:
    account = find_account(account_id)
    settings = get_settings(account_id)

    if not manual and not settings.get("enable_engage", False):
        return 0

    tags = settings["target_hashtags"]
    if not tags:
        add_activity(
            f"{account['username']}: no target hashtags configured",
            "warn",
        )
        return 0

    tag = re.sub(r"[^A-Za-z0-9_]", "", random.choice(tags).lstrip("#"))
    if not tag:
        add_activity(
            f"{account['username']}: no valid hashtag after sanitizing target",
            "warn",
        )
        return 0

    goto(page, f"https://www.tiktok.com/tag/{tag}", 2600)
    pause_for_manual_tiktok_challenge(page, account_id)
    enforce_tiktok_action_restriction(page, account_id, "engagement")

    first = page.locator(
        "[data-e2e='challenge-item'], "
        "[data-e2e='search-card-video'], "
        "a[href*='/video/']"
    ).first

    if not first.count():
        add_activity(
            f"{account['username']}: #{tag} loaded, but no recognizable video card was found",
            "warn",
        )
        return 0

    try:
        first.click(timeout=3000)
    except Exception:
        first.evaluate("(el) => el.click()")
    page.wait_for_timeout(1400)

    pause_for_manual_tiktok_challenge(page, account_id)

    likes_confirmed = 0
    saves_confirmed = 0
    reposts_confirmed = 0
    clips_seen = 0
    clips_target = ENGAGE_CLIPS_PER_PASS

    while clips_seen < clips_target:
        if not manual and not get_settings(account_id).get("enable_engage", False):
            break

        pause_for_manual_tiktok_challenge(page, account_id)
        enforce_tiktok_action_restriction(page, account_id, "engagement")

        try:
            video_url = str(page.url or "")
        except Exception:
            video_url = ""

        if "/video/" not in video_url:
            add_activity(
                f"{account['username']}: video viewer did not expose a /video/ URL; "
                "attempting current visible controls anyway",
                "info",
            )

        clip_identity = current_tiktok_clip_identity(page)
        key = stable_key("video", clip_identity)
        clips_seen += 1

        add_activity(
            f"{account['username']}: engaging #{tag} clip "
            f"{clips_seen}/{clips_target}: {video_url[:120]}",
            "info",
        )

        controls_found = 0

        # LIKE
        if key not in history["liked_videos"]:
            like = find_like_control(page)
            if like is not None:
                controls_found += 1
            if like is None:
                add_activity(
                    f"{account['username']}: Like control not found on clip {clips_seen}",
                    "warn",
                )
            else:
                if not wait_for_write_slot(account_id, "like", max_wait=90):
                    add_activity(
                        f"{account['username']}: Like deferred by pacing; continuing clip navigation",
                        "warn",
                    )
                else:
                    before = _button_state_signature(like)
                    activate_once_dom(like, page, account["username"], "Like")
                    page.wait_for_timeout(500)
                    enforce_tiktok_action_restriction(page, account_id, "like")

                    confirmed = wait_for_like_confirmation(
                        page,
                        like,
                        before,
                        8,
                    )
                    if confirmed:
                        register_action_verification(account_id, "like", True)
                        record_write(account_id, "like")
                        history["liked_videos"].append(key)
                        likes_confirmed += 1
                        add_activity(
                            f"{account['username']}: ✅ like confirmed on #{tag} clip",
                            "good",
                        )
                    else:
                        register_action_verification(
                            account_id,
                            "like",
                            False,
                            "Like UI did not change.",
                        )

        pause_for_manual_tiktok_challenge(page, account_id)

        # SAVE / FAVORITE
        if key not in history["saved_videos"]:
            favorite = find_favorite_control(page)
            if favorite is not None:
                controls_found += 1
            if favorite is None:
                add_activity(
                    f"{account['username']}: Save/Favorite control not found on clip {clips_seen}",
                    "warn",
                )
            else:
                if not wait_for_write_slot(account_id, "save", max_wait=90):
                    add_activity(
                        f"{account['username']}: Save deferred by pacing",
                        "warn",
                    )
                else:
                    before = _button_state_signature(favorite)
                    activate_once_dom(
                        favorite,
                        page,
                        account["username"],
                        "Save/Favorite",
                    )
                    page.wait_for_timeout(500)
                    enforce_tiktok_action_restriction(page, account_id, "save")

                    confirmed, _ = _confirm_control_state_change(
                        page,
                        find_favorite_control,
                        before,
                        8,
                    )
                    if confirmed:
                        record_write(account_id, "save")
                        history["saved_videos"].append(key)
                        saves_confirmed += 1
                        update_engagement_counter(account_id, "saves_sent", 1)
                        add_activity(
                            f"{account['username']}: ✅ save/favorite confirmed on #{tag} clip",
                            "good",
                        )
                    else:
                        register_action_verification(
                            account_id,
                            "save",
                            False,
                            "Favorite/Save UI did not change.",
                        )

        pause_for_manual_tiktok_challenge(page, account_id)

        # REPOST
        if key not in history["reposted_videos"]:
            confirmed_repost = False
            direct = find_direct_repost_control(page)

            if direct is not None:
                controls_found += 1
                if wait_for_write_slot(account_id, "repost", max_wait=90):
                    before = _button_state_signature(direct)
                    activate_once_dom(
                        direct,
                        page,
                        account["username"],
                        "Repost",
                    )
                    page.wait_for_timeout(650)
                    enforce_tiktok_action_restriction(page, account_id, "repost")
                    confirmed_repost, _ = _confirm_control_state_change(
                        page,
                        find_direct_repost_control,
                        before,
                        8,
                    )
            else:
                share = find_share_control(page)
                if share is not None:
                    controls_found += 1
                if share is not None and wait_for_write_slot(
                    account_id,
                    "repost",
                    max_wait=90,
                ):
                    activate_once_dom(
                        share,
                        page,
                        account["username"],
                        "Share",
                    )
                    page.wait_for_timeout(450)

                    option = _find_visible_control(
                        page,
                        (
                            "[data-e2e='share-repost']",
                            "[role='menuitem']:has-text('Repost')",
                            "div[data-e2e='share-repost']",
                        ),
                    )

                    if option is not None:
                        activate_once_dom(
                            option,
                            page,
                            account["username"],
                            "Repost option",
                        )
                        page.wait_for_timeout(750)
                        enforce_tiktok_action_restriction(
                            page,
                            account_id,
                            "repost",
                        )

                        # Confirm by reopening Share and looking for Remove/Undo.
                        share2 = find_share_control(page)
                        if share2 is not None:
                            activate_once_dom(
                                share2,
                                page,
                                account["username"],
                                "Share verification",
                            )
                            page.wait_for_timeout(400)

                            state_control = _find_visible_control(
                                page,
                                (
                                    "[role='menuitem']:has-text('Remove repost')",
                                    "[role='menuitem']:has-text('Undo repost')",
                                    "[data-e2e='share-repost']",
                                ),
                            )
                            if state_control is not None:
                                state_text = " ".join(
                                    _button_state_signature(state_control)
                                ).lower()
                                confirmed_repost = (
                                    "remove repost" in state_text
                                    or "undo repost" in state_text
                                    or "reposted" in state_text
                                )

                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass

            if confirmed_repost:
                record_write(account_id, "repost")
                history["reposted_videos"].append(key)
                reposts_confirmed += 1
                update_engagement_counter(account_id, "reposts_sent", 1)
                add_activity(
                    f"{account['username']}: ✅ repost confirmed on #{tag} clip",
                    "good",
                )
            else:
                add_activity(
                    f"{account['username']}: repost was not confirmed on clip {clips_seen}",
                    "info",
                )

        save_history(account_id, history)

        if controls_found == 0:
            write_tiktok_editor_debug_snapshot(
                page,
                f"{account['username']}_engage_no_controls",
            )
            add_activity(
                f"{account['username']}: no Like/Save/Repost controls were found on "
                f"clip {clips_seen}; saved debug snapshot",
                "warn",
            )

        if clips_seen < clips_target:
            advanced = advance_to_next_tiktok_clip(
                page,
                account_id,
                account["username"],
            )
            if not advanced:
                write_tiktok_editor_debug_snapshot(
                    page,
                    f"{account['username']}_engage_no_scroll",
                )
                add_activity(
                    f"{account['username']}: could not advance to the next hashtag clip; "
                    "saved debug snapshot and ended this engagement pass",
                    "warn",
                )
                break
            page.wait_for_timeout(random.randint(1200, 2200))

    add_activity(
        f"{account['username']}: hashtag engagement finished; "
        f"clips={clips_seen}, likes={likes_confirmed}, "
        f"saves={saves_confirmed}, reposts={reposts_confirmed}",
        "good" if clips_seen else "warn",
    )
    return likes_confirmed





def follow_target_network(
    page,
    account_id: str,
    history: dict[str, Any],
    manual: bool = False,
) -> int:
    account = find_account(account_id)
    settings = get_settings(account_id)

    if not manual and not settings.get("enable_follow", False):
        return 0

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

    add_activity(
        f"{account['username']}: opening @{target} {source} list",
        "info",
    )
    goto(page, f"https://www.tiktok.com/@{target}", 2500)
    pause_for_manual_tiktok_challenge(page, account_id)

    selectors = (
        ("[data-e2e='followers-count']", "strong[data-e2e='followers-count']")
        if source == "followers"
        else ("[data-e2e='following-count']", "strong[data-e2e='following-count']")
    )

    trigger = None
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count():
                trigger = loc
                break
        except Exception:
            pass

    if trigger is None:
        label = "Followers" if source == "followers" else "Following"
        try:
            loc = page.get_by_text(
                re.compile(rf"^\s*{label}\s*$", re.I)
            ).first
            if loc.count():
                trigger = loc
        except Exception:
            pass

    if trigger is None:
        add_activity(
            f"{account['username']}: @{target} loaded, but no recognizable "
            f"{source} control was found",
            "warn",
        )
        write_tiktok_editor_debug_snapshot(
            page,
            f"{account['username']}_follow_trigger",
        )
        return 0

    try:
        trigger.evaluate("(el) => el.click()")
    except Exception:
        try:
            ancestor = trigger.locator(
                "xpath=ancestor::*[@role='button' or self::a or self::button][1]"
            )
            ancestor.evaluate("(el) => el.click()")
        except Exception as exc:
            raise RuntimeError(
                f"Could not open @{target}'s {source} list: {exc}"
            ) from exc

    page.wait_for_timeout(1200)
    pause_for_manual_tiktok_challenge(page, account_id)

    popup = page.locator("[data-e2e='follow-info-popup']").last
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            if popup.count() and popup.is_visible(timeout=300):
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
        popup = page.locator("[data-e2e='follow-info-popup']").last

    if popup.count():
        root = popup
        add_activity(
            f"{account['username']}: @{target} {source} popup opened",
            "good",
        )
    else:
        fallback = page.locator("[role='dialog']").last
        root = fallback if fallback.count() else page
        add_activity(
            f"{account['username']}: follow-info-popup hook not found; using fallback container",
            "warn",
        )

    def exact_follow_buttons():
        try:
            primary = root.locator(
                "button[data-e2e='follow-button']"
            ).filter(
                has_text=re.compile(r"^\s*Follow\s*$", re.I)
            )
            if primary.count():
                return primary
        except Exception:
            pass
        try:
            return root.get_by_role(
                "button",
                name=re.compile(r"^\s*Follow\s*$", re.I),
            )
        except Exception:
            return root.locator("button").filter(
                has_text=re.compile(r"^\s*Follow\s*$", re.I)
            )

    followed = 0
    follow_attempts = 0
    batch_attempted_keys: set[str] = set()
    max_follow = MANUAL_FOLLOW_BATCH if manual else int(settings["follow_limit"])
    examined = 0
    empty_load_attempts = 0

    add_activity(
        f"{account['username']}: follow batch target={max_follow} "
        f"({'manual button' if manual else 'Auto'}); "
        f"{'rapid hard-cap burst' if manual else 'normal pacing'}",
        "info",
    )

    while follow_attempts < max_follow and examined < 160:
        if not manual:
            if account_id not in AUTO_ENABLED_ACCOUNTS:
                add_activity(
                    f"{account['username']}: active Auto follow batch stopped because Auto is OFF",
                    "warn",
                )
                break
            cooldown = ACCOUNT_COOLDOWNS.get(account_id)
            if cooldown and datetime.now() < cooldown:
                add_activity(
                    f"{account['username']}: active Auto follow batch stopped by safety cooldown",
                    "warn",
                )
                break
            if not get_settings(account_id).get("enable_follow", False):
                break

        pause_for_manual_tiktok_challenge(page, account_id)
        enforce_tiktok_action_restriction(page, account_id, "follow")

        buttons = exact_follow_buttons()
        count = buttons.count()

        if count == 0:
            empty_load_attempts += 1
            if empty_load_attempts > 12:
                write_tiktok_editor_debug_snapshot(
                    page,
                    f"{account['username']}_follow_no_buttons",
                )
                add_activity(
                    f"{account['username']}: no more Follow rows after lazy loading; "
                    "saved debug snapshot",
                    "warn",
                )
                break

            try:
                root.evaluate(
                    "(el) => { el.scrollTop = el.scrollHeight; el.scrollBy(0, 900); }"
                )
            except Exception:
                try:
                    root.locator("li").last.scroll_into_view_if_needed(timeout=1200)
                except Exception:
                    pass
            page.wait_for_timeout(550 if manual else 1200)
            continue

        empty_load_attempts = 0
        selected = None

        # Scan all currently loaded exact-Follow rows and choose an unattempted
        # user. This prevents repeatedly selecting the first stale row.
        for button_index in range(min(count, 40)):
            candidate_button = buttons.nth(button_index)

            candidate_row = None
            try:
                maybe_row = candidate_button.locator("xpath=ancestor::li[1]")
                if maybe_row.count():
                    candidate_row = maybe_row
            except Exception:
                pass
            if candidate_row is None:
                candidate_row = candidate_button.locator("xpath=..")

            candidate_name = ""
            try:
                links = candidate_row.locator("a[href*='/@']")
                for i in range(min(links.count(), 6)):
                    href = links.nth(i).get_attribute("href") or ""
                    match = re.search(r"/@([^/?#]+)", href)
                    if match:
                        candidate_name = match.group(1)
                        break
            except Exception:
                pass

            if not candidate_name:
                try:
                    row_text = re.sub(
                        r"\s+",
                        " ",
                        candidate_row.inner_text(timeout=600) or "",
                    ).strip()[:180]
                except Exception:
                    row_text = ""
                candidate_name = (
                    f"row_{stable_key(target, source, row_text)}"
                    if row_text
                    else f"row_index_{button_index}_{examined}"
                )

            candidate_key = stable_key(
                "follow",
                target,
                source,
                candidate_name.lower(),
            )

            if (
                candidate_key in history["followed_users"]
                or candidate_key in batch_attempted_keys
            ):
                continue

            selected = (
                candidate_button,
                candidate_row,
                candidate_name,
                candidate_key,
            )
            break

        if selected is None:
            empty_load_attempts += 1
            if empty_load_attempts > 12:
                break
            try:
                root.evaluate(
                    "(el) => { el.scrollTop = el.scrollHeight; el.scrollBy(0, 900); }"
                )
            except Exception:
                try:
                    root.locator("li").last.scroll_into_view_if_needed(timeout=1200)
                except Exception:
                    pass
            page.wait_for_timeout(500 if manual else 1100)
            continue

        button, row, candidate_name, key = selected
        examined += 1

        if manual:
            wait_for_manual_follow_burst_slot(
                page,
                account_id,
            )
        else:
            if not wait_for_write_slot(
                account_id,
                "follow",
                max_wait=120,
            ):
                add_activity(
                    f"{account['username']}: Auto follow pass paused by pacing at "
                    f"{follow_attempts}/{max_follow} attempts, {followed} confirmed",
                    "warn",
                )
                break

        batch_attempted_keys.add(key)
        follow_attempts += 1

        add_activity(
            f"{account['username']}: sending Follow request "
            f"{follow_attempts}/{max_follow} for @{candidate_name}",
            "info",
        )

        activate_once_dom(
            button,
            page,
            account["username"],
            "Follow",
        )
        page.wait_for_timeout(300 if manual else 650)
        enforce_tiktok_action_restriction(
            page,
            account_id,
            "follow",
        )

        confirmed, state_text = wait_for_follow_confirmation(
            page,
            button,
            row,
            2 if manual else 8,
        )

        if confirmed is not True:
            if manual:
                # Hard cap is attempts, so keep the burst moving. Ambiguous UI
                # never causes more than seven real Follow activations.
                add_activity(
                    f"{account['username']}: Follow request {follow_attempts}/{max_follow} "
                    f"for @{candidate_name} "
                    f"{'still shows Follow' if confirmed is False else 'is ambiguous'}; "
                    "not counted as confirmed",
                    "warn" if confirmed is False else "info",
                )
                continue

            profile_confirmed, profile_state = verify_follow_relationship_on_profile(
                page.context,
                candidate_name,
                14,
            )

            if profile_confirmed is True:
                confirmed = True
                state_text = f"profile:{profile_state}"
                add_activity(
                    f"{account['username']}: popup was stale, but profile verification "
                    f"confirmed @{candidate_name} as {profile_state}",
                    "good",
                )
            elif profile_confirmed is False and confirmed is False:
                register_action_verification(
                    account_id,
                    "follow",
                    False,
                    f"@{candidate_name}: popup={state_text or 'Follow'}; "
                    f"profile={profile_state or 'Follow'}",
                )
                add_activity(
                    f"{account['username']}: follow on @{candidate_name} was explicitly "
                    "not confirmed by popup and profile",
                    "warn",
                )

                if (
                    account_id not in AUTO_ENABLED_ACCOUNTS
                    or (
                        ACCOUNT_COOLDOWNS.get(account_id)
                        and datetime.now() < ACCOUNT_COOLDOWNS[account_id]
                    )
                ):
                    break
                continue
            else:
                add_activity(
                    f"{account['username']}: Auto follow state for @{candidate_name} is "
                    "ambiguous; not counted and no safety strike",
                    "warn",
                )
                continue

        register_action_verification(account_id, "follow", True)

        # Manual bursts use their own hard-cap cadence and do not poison the
        # general rolling write budget used by Auto/engagement/posting.
        if not manual:
            record_write(account_id, "follow")

        history["followed_users"].append(key)
        followed += 1

        add_activity(
            f"{account['username']}: ✅ follow confirmed for @{candidate_name} "
            f"(request {follow_attempts}/{max_follow}, confirmed {followed})"
            + (f" [{state_text[:80]}]" if state_text else ""),
            "good",
        )

        page.wait_for_timeout(180 if manual else 600)

    save_history(account_id, history)

    add_activity(
        f"{account['username']}: follow batch finished after "
        f"{follow_attempts}/{max_follow} Follow request(s); "
        f"{followed} immediately confirmed",
        "good" if follow_attempts else "warn",
    )
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
    if not account or account_id in REMOVED_RUNTIME_ACCOUNTS:
        return

    if account_id not in CONNECTED_ACCOUNTS:
        add_activity(
            f"{account['username']}: workflow ignored because account is not connected",
            "warn",
        )
        return

    lock = account_lock(account_id)

    # Manual button presses queue behind an active browser workflow instead of
    # disappearing after a five-second lock timeout.
    if forced_task and lock.locked():
        add_activity(
            f"{account['username']}: manual {forced_task} queued behind the "
            "currently running account workflow",
            "info",
        )

    if forced_task:
        acquired = lock.acquire(timeout=7200)
    else:
        acquired = lock.acquire(blocking=False)

    if not acquired:
        if forced_task:
            add_activity(
                f"{account['username']}: queued manual {forced_task} expired before the account became free",
                "warn",
            )
        return

    add_activity(
        f"{account['username']}: starting "
        f"{'manual ' + forced_task if forced_task else 'automatic'} workflow",
        "info",
    )

    history = load_history(account_id)
    context = None

    def run_feature(label: str, func) -> int:
        try:
            result = int(func() or 0)
            add_activity(
                f"{account['username']}: {label} pass finished with {result} action(s)",
                "good" if result else "info",
            )
            return result
        except Exception as exc:
            message = str(exc)
            add_activity(
                f"{account['username']}: {label} failed: "
                f"{type(exc).__name__}: {message[:320]}",
                "error",
            )
            if any(token in message.lower() for token in (
                "login/session expired",
                "asking for login",
                "profile identity mismatch",
                "login needed",
            )):
                raise
            return 0

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

        settings = get_settings(account_id)
        update_account(
            account_id,
            status="Running",
            last_error=None,
            cooldown_until=None,
        )

        with sync_playwright() as p:
            context = launch_profile(
                p,
                account_id,
                headless=bool(settings["headless_automation"]),
            )
            page = choose_active_page(context)

            add_activity(
                f"{account['username']}: Chromium session opened for workflow at {page.url}",
                "info",
            )

            dm_count = comment_count = post_count = like_count = follow_count = 0

            # Manual buttons are standalone actions. They do not depend on the
            # corresponding Auto feature checkbox.
            if forced_task == "upload":
                post_count = run_feature(
                    "manual post",
                    lambda: execute_upload(
                        page,
                        account_id,
                        history,
                        manual=True,
                    ),
                )
            elif forced_task == "follow":
                try:
                    follow_count = int(
                        follow_target_network(
                            page,
                            account_id,
                            history,
                            manual=True,
                        ) or 0
                    )
                    add_activity(
                        f"{account['username']}: manual follow batch complete; "
                        f"{follow_count} relationship change(s) immediately confirmed. "
                        "The hard cap remains seven Follow requests.",
                        "good" if follow_count else "info",
                    )
                except Exception as exc:
                    add_activity(
                        f"{account['username']}: manual follow failed: "
                        f"{type(exc).__name__}: {str(exc)[:320]}",
                        "error",
                    )
            elif forced_task == "engage":
                like_count = run_feature(
                    "manual engagement",
                    lambda: engage_hashtag(
                        page,
                        account_id,
                        history,
                        manual=True,
                    ),
                )
            elif forced_task == "comments":
                comment_count = run_feature(
                    "manual comments",
                    lambda: handle_comments(
                        page,
                        account_id,
                        history,
                        manual=True,
                    ),
                )

            else:
                # Periodic inbound features.
                settings = get_settings(account_id)

                if (
                    settings.get("enable_dms", False)
                    and should_run_periodic_check(
                        LAST_DM_CHECK,
                        account_id,
                        DM_CHECK_INTERVAL_SECONDS,
                    )
                ):
                    dm_count += run_feature(
                        "DM listener",
                        lambda: handle_dms(page, account_id, history),
                    )

                if (
                    settings.get("enable_comments", False)
                    and should_run_periodic_check(
                        LAST_COMMENT_CHECK,
                        account_id,
                        COMMENT_CHECK_INTERVAL_SECONDS,
                    )
                ):
                    comment_count += run_feature(
                        "comment listener",
                        lambda: handle_comments(
                            page,
                            account_id,
                            history,
                            manual=False,
                        ),
                    )

                # Run ALL enabled outward features in one Auto cycle instead of
                # picking one random/rotating feature and ending the pass.
                settings = get_settings(account_id)
                enabled_names = [
                    label for key, label in (
                        ("enable_posts", "Posts"),
                        ("enable_follow", "Follow"),
                        ("enable_engage", "Engage"),
                    )
                    if settings.get(key, False)
                ]
                add_activity(
                    f"{account['username']}: Auto outward features this pass: "
                    f"{', '.join(enabled_names) if enabled_names else 'none'}",
                    "info",
                )

                # Run quicker features first. Posting can spend several minutes
                # in local vision/text models, so it runs last rather than blocking
                # Follow/Engage for the whole Auto cycle.
                if (
                    account_id in AUTO_ENABLED_ACCOUNTS
                    and settings.get("enable_follow", False)
                ):
                    follow_count += run_feature(
                        "follow",
                        lambda: follow_target_network(
                            page,
                            account_id,
                            history,
                            manual=False,
                        ),
                    )

                settings = get_settings(account_id)
                if (
                    account_id in AUTO_ENABLED_ACCOUNTS
                    and settings.get("enable_engage", False)
                ):
                    like_count += run_feature(
                        "engagement",
                        lambda: engage_hashtag(
                            page,
                            account_id,
                            history,
                            manual=False,
                        ),
                    )

                settings = get_settings(account_id)
                if (
                    account_id in AUTO_ENABLED_ACCOUNTS
                    and settings.get("enable_posts", False)
                ):
                    post_count += run_feature(
                        "post",
                        lambda: execute_upload(
                            page,
                            account_id,
                            history,
                            manual=False,
                        ),
                    )

            save_history(account_id, history)

            current = load_analytics()["accounts"].get(account_id, {})
            update_account(
                account_id,
                status=(
                    "Connected / Auto On"
                    if account_id in AUTO_ENABLED_ACCOUNTS
                    else "Connected / Auto Off"
                ),
                connected=True,
                posts_sent=int(current.get("posts_sent") or 0) + post_count,
                likes_sent=int(current.get("likes_sent") or 0) + like_count,
                follows_sent=int(current.get("follows_sent") or 0) + follow_count,
                dm_replies=int(current.get("dm_replies") or 0) + dm_count,
                comment_replies=(
                    int(current.get("comment_replies") or 0)
                    + comment_count
                ),
                last_checked=now_iso(),
            )

            try:
                collect_metrics(page, account_id)
            except Exception as exc:
                add_activity(
                    f"{account['username']}: metrics refresh skipped: {str(exc)[:120]}",
                    "info",
                )

            try:
                context.close()
            except Exception:
                pass
            context = None

    except Exception as exc:
        message = str(exc)

        if any(token in message.lower() for token in (
            "login/session expired",
            "asking for login",
            "profile identity mismatch",
            "login needed",
        )):
            CONNECTED_ACCOUNTS.discard(account_id)
            AUTO_ENABLED_ACCOUNTS.discard(account_id)
            update_account(
                account_id,
                status="Login needed",
                connected=False,
                auto_enabled=False,
                last_error=message,
            )
            add_activity(
                f"{account['username']}: saved session is no longer usable: {message}",
                "warn",
            )
        else:
            cooldown = datetime.now() + timedelta(seconds=30)
            ACCOUNT_COOLDOWNS[account_id] = cooldown
            update_account(
                account_id,
                status="Error cooldown",
                last_error=message,
                cooldown_until=cooldown.isoformat(timespec="seconds"),
            )
            add_activity(
                f"{account['username']}: workflow-level error: "
                f"{type(exc).__name__}: {message[:220]}",
                "error",
            )
            traceback.print_exc()

    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        lock.release()





def start_manual_action(account_id: str, task: str) -> None:
    # Manual DM mode intentionally removed.
    allowed = {"upload", "follow", "engage", "comments"}
    if task not in allowed:
        raise ValueError("Unsupported manual task.")
    if account_id not in CONNECTED_ACCOUNTS:
        raise RuntimeError("Quick Login or Save Login first.")
    def worker() -> None:
        account = find_account(account_id)
        try:
            add_activity(
                f"{account['username'] if account else account_id}: manual {task} requested",
                "info",
            )
            run_account_once(account_id, task)
        except Exception as exc:
            add_activity(
                f"{account['username'] if account else account_id}: manual {task} crashed: {exc}",
                "error",
            )
            traceback.print_exc()

    thread = threading.Thread(
        target=worker,
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
        settings = get_settings(account_id)
        NEXT_DUE[account_id] = time.monotonic() + random.randint(2, 5)

        enabled_features = [
            label for key, label in (
                ("enable_posts", "Posts"),
                ("enable_follow", "Follow"),
                ("enable_engage", "Engage"),
                ("enable_dms", "DMs"),
                ("enable_comments", "Comments"),
            )
            if settings.get(key, False)
        ]

        update_account(
            account_id,
            auto_enabled=True,
            status="Connected / Auto On",
        )
        add_activity(
            f"{account['username']}: automation enabled; first pass in 2-5s; "
            f"features={', '.join(enabled_features) if enabled_features else 'none'}",
            "good",
        )
    else:
        AUTO_ENABLED_ACCOUNTS.discard(account_id)
        NEXT_DUE.pop(account_id, None)
        update_account(
            account_id,
            auto_enabled=False,
            status=(
                "Connected / Auto Off"
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
        "TikTok scheduler started; up to 3 enabled accounts can work concurrently while each account stays single-worker",
        "good",
    )

    while not AUTOMATION_STOP.wait(1.0):
        now = time.monotonic()

        for account_id in list(AUTO_ENABLED_ACCOUNTS):
            if account_id in REMOVED_RUNTIME_ACCOUNTS or not find_account(account_id):
                AUTO_ENABLED_ACCOUNTS.discard(account_id)
                NEXT_DUE.pop(account_id, None)
                continue

            if account_id not in CONNECTED_ACCOUNTS:
                continue

            if now < NEXT_DUE.get(account_id, 0.0):
                continue

            # A long vision/upload/manual batch owns this account's persistent
            # profile. Don't spawn throwaway workers while it is still active.
            if account_lock(account_id).locked():
                NEXT_DUE[account_id] = now + 5.0
                continue

            settings = get_settings(account_id)
            delay = random.randint(
                int(settings["workflow_min_seconds"]),
                int(settings["workflow_max_seconds"]),
            )

            NEXT_DUE[account_id] = now + delay
            account = find_account(account_id)

            if account:
                add_activity(
                    f"{account['username']}: starting automatic workflow; "
                    f"next eligibility in ~{delay}s after the account is free",
                    "info",
                )

            threading.Thread(
                target=run_account_once,
                args=(account_id, None),
                daemon=True,
                name=f"tiktok-auto-{account_id}",
            ).start()




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
<title>TikTok Control Head v25</title>
<style>
:root{color-scheme:dark;--bg:#0b0d12;--panel:#131722;--panel2:#0d1018;--border:#293043;--text:#edf1f7;--muted:#8e99ad;--ok:#6ee7b7;--warn:#facc6b;--bad:#fb7185}
*{box-sizing:border-box}body{margin:0;padding:18px;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:14px}h1{font-size:21px;margin:0}.small{font-size:11px;color:var(--muted)}
.toolbar,.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:15px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:11px;padding:14px}.cardhead{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
h2{margin:0;font-size:17px}.badge{font-size:10px;padding:4px 7px;border-radius:999px;background:#252b3d}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:7px;margin:10px 0}.stat{background:var(--panel2);padding:8px;border-radius:7px;text-align:center}.stat b{display:block}.stat span{font-size:10px;color:var(--muted)}
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
    <h1>🎛️ TikTok Control Head v25</h1>
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
  Automation never starts just because the Python process started. An account can stay configured and even connected with AUTO OFF while other accounts run.
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
const CONTROL_HEAD_REFRESH_SECONDS=10;
let editing=false,refreshBusy=false;

function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function toast(msg,bad=false){const t=document.getElementById("toast");t.textContent=msg;t.style.display="block";t.style.color=bad?"var(--bad)":"var(--text)";clearTimeout(window.__tt);window.__tt=setTimeout(()=>t.style.display="none",5000);}
function splitList(v){return String(v||"").split(/[\n,]+/).map(x=>x.trim()).filter(Boolean);}
async function api(action,payload={}){const r=await fetch("/api/control",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,...payload})});const d=await r.json().catch(()=>({ok:false,error:"Invalid response"}));if(!r.ok||!d.ok)throw new Error(d.error||`HTTP ${r.status}`);return d;}

document.addEventListener("focusin",e=>{if(e.target.matches("input,textarea,select"))editing=true});
document.addEventListener("focusout",e=>{const card=e.target.closest?.(".card[data-account-id]");setTimeout(()=>{editing=!!document.querySelector("input:focus,textarea:focus,select:focus");if(card&&!editing)autoSaveSettings(card.dataset.accountId,true)},0)});
document.addEventListener("change",e=>{const card=e.target.closest?.(".card[data-account-id]");if(card)autoSaveSettings(card.dataset.accountId,true)});

function accountCard(id,a){
  const s=a.settings||{};
  const connected=!!a.connected,auto=!!a.auto_enabled,loginOpen=!!a.login_browser_open;
  const status=connected?"CONNECTED":"DISCONNECTED";
  const tags=(s.target_hashtags||[]).join(", ");
  const targets=(s.target_accounts||[]).join(", ");
  return `<div class="card" data-account-id="${esc(id)}">
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
      <div class="stat"><b>${a.posts_sent||0}</b><span>Confirmed Posts</span></div>
      <div class="stat"><b>${a.follows_sent||0}</b><span>Confirmed Follows</span></div>
      <div class="stat"><b>${a.likes_sent||0}</b><span>Confirmed Likes</span></div>
      <div class="stat"><b>${a.saves_sent||0}</b><span>Confirmed Saves</span></div>
      <div class="stat"><b>${a.reposts_sent||0}</b><span>Confirmed Reposts</span></div>
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
        <button onclick="task('${id}','comments')" ${!connected?"disabled":""}>Reply Comments</button>
      </div>
    </div>

    <div class="section">
      <b>Behavior Features & Settings</b>
      <div class="fieldgrid">
        <div><label>Account username</label><input id="name_${id}" value="${esc(a.username)}"></div>
        <div style="grid-column:1/-1">
          <label>Enabled features</label>
          <div class="row">
            <label><input id="posts_${id}" type="checkbox" style="width:auto" ${s.enable_posts?"checked":""}> Posts</label>
            <label><input id="follow_${id}" type="checkbox" style="width:auto" ${s.enable_follow?"checked":""}> Follow</label>
            <label><input id="engage_${id}" type="checkbox" style="width:auto" ${s.enable_engage?"checked":""}> Hashtag / Engage</label>
            <label><input id="dms_${id}" type="checkbox" style="width:auto" ${s.enable_dms?"checked":""}> DMs</label>
            <label><input id="comments_${id}" type="checkbox" style="width:auto" ${s.enable_comments?"checked":""}> Comment Replies</label>
          </div>
          <div class="small">Checkboxes control Auto. Manual Post/Follow/Engage/Comments buttons work independently. Manual Follow sends at most 7 Follow requests per click (hard cap) at a short dedicated cadence. Auto uses the normal pacing controls. A TikTok puzzle pauses the current browser workflow for manual completion, then resumes it. Hashtag Engage attempts Like + Save/Favorite + Repost, then advances to the next clip.</div>
        </div>
        <div><label>Follow source</label><select id="fsource_${id}">${["both","followers","following"].map(m=>`<option value="${m}" ${s.follow_source===m?"selected":""}>${m}</option>`).join("")}</select></div>
        <div><label>Follow limit / pass</label><input id="followlim_${id}" type="number" min="1" max="21" value="${s.follow_limit||2}"></div>
        <div><label>Caption char limit</label><input id="caplim_${id}" type="number" min="80" max="2000" value="${s.caption_char_limit||350}"></div>
        <div><label>Reply char limit</label><input id="replim_${id}" type="number" min="20" max="1000" value="${s.reply_char_limit||220}"></div>
        <div><label>Video frames to watch</label><input id="frames_${id}" type="number" min="3" max="9" value="3"></div>
        <div><label>Writes / window</label><input id="maxwrites_${id}" type="number" min="1" max="21" value="${s.max_writes_per_window||6}"></div>
        <div><label>Window seconds</label><input id="window_${id}" type="number" min="60" max="7200" value="${s.write_window_seconds||900}"></div>
        <div><label>Min write gap seconds</label><input id="gap_${id}" type="number" min="3" max="600" value="${s.write_min_gap_seconds||30}"></div>
        <div><label>Upload cooldown seconds</label><input id="uploadgap_${id}" type="number" min="300" max="21600" value="${s.upload_cooldown_seconds||1800}"></div>
        <div><label>Workflow min seconds</label><input id="wmin_${id}" type="number" min="15" max="7200" value="${s.workflow_min_seconds||30}"></div>
        <div><label>Workflow max seconds</label><input id="wmax_${id}" type="number" min="15" max="14400" value="${s.workflow_max_seconds||90}"></div>
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
        <label><input id="headless_${id}" type="checkbox" style="width:auto" ${s.headless_automation?"checked":""}> Headless automation</label>
      </div>
      <div class="row">
        <button onclick="saveName('${id}')">Save Username</button>
        <button class="danger" onclick="removeTikTokAccount('${id}', ${JSON.stringify(a.username)})">Remove Account</button>
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
  for(const [id,a] of Object.entries(accounts)){
    const s=a.settings||{};
    settingsSignatures[id]=JSON.stringify({
      enable_posts:!!s.enable_posts,enable_follow:!!s.enable_follow,
      enable_engage:!!s.enable_engage,enable_dms:!!s.enable_dms,
      enable_comments:!!s.enable_comments,follow_source:s.follow_source,
      follow_limit:Number(s.follow_limit),caption_char_limit:Number(s.caption_char_limit),
      reply_char_limit:Number(s.reply_char_limit),video_frames:Number(s.video_frames),
      max_writes_per_window:Number(s.max_writes_per_window),write_window_seconds:Number(s.write_window_seconds),
      write_min_gap_seconds:Number(s.write_min_gap_seconds),upload_cooldown_seconds:Number(s.upload_cooldown_seconds),
      workflow_min_seconds:Number(s.workflow_min_seconds),workflow_max_seconds:Number(s.workflow_max_seconds),
      target_accounts:s.target_accounts||[],target_hashtags:s.target_hashtags||[],
      persona_prompt:s.persona_prompt||"",caption_prompt:s.caption_prompt||"",
      dm_prompt:s.dm_prompt||"",comment_prompt:s.comment_prompt||"",
      require_video_vision:!!s.require_video_vision,headless_automation:!!s.headless_automation
    });
  }
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
  try{if(!editing) await autoSaveVisibleSettings(); const r=await fetch("/api/metrics",{cache:"no-store"});const d=await r.json();render(d);document.getElementById("refreshState").textContent="Updated "+new Date().toLocaleTimeString();}
  catch(e){document.getElementById("refreshState").textContent="Refresh error";}
  finally{refreshBusy=false;}
}

async function act(id,action){try{await api(action,{account_id:id});await refreshNow(true);toast(`${action} requested.`);}catch(e){toast(e.message,true)}}
async function task(id,taskName){try{await api("task",{account_id:id,task:taskName});toast(`Started ${taskName}.`);}catch(e){toast(e.message,true)}}
async function clearLogin(id){if(!confirm("Clear saved TikTok login cookies/storage for this account?"))return;try{await api("clear_login",{account_id:id});toast("Clear-login started.");}catch(e){toast(e.message,true)}}
async function openFolder(folder){try{await api("open_folder",{folder});toast("Opened source folder.");}catch(e){toast(e.message,true)}}
async function saveName(id){const username=document.getElementById(`name_${id}`).value;try{await api("save_name",{account_id:id,username});editing=false;await refreshNow(true);toast("Username saved.");}catch(e){toast(e.message,true)}}

function collectSettings(id){
  return {
    enable_posts:document.getElementById(`posts_${id}`).checked,
    enable_follow:document.getElementById(`follow_${id}`).checked,
    enable_engage:document.getElementById(`engage_${id}`).checked,
    enable_dms:document.getElementById(`dms_${id}`).checked,
    enable_comments:document.getElementById(`comments_${id}`).checked,
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
    headless_automation:document.getElementById(`headless_${id}`).checked,
  };
}

const settingsSignatures={};

async function autoSaveSettings(id,quiet=true){
  const card=document.querySelector(`.card[data-account-id="${CSS.escape(id)}"]`);
  if(!card) return;
  const settings=collectSettings(id);
  const sig=JSON.stringify(settings);
  if(settingsSignatures[id]===sig) return;
  try{
    await api("save_settings",{account_id:id,settings});
    settingsSignatures[id]=sig;
    if(!quiet) toast("Behavior updated.");
  }catch(e){
    if(!quiet) toast(e.message,true);
  }
}

async function autoSaveVisibleSettings(){
  const ids=[...document.querySelectorAll(".card[data-account-id]")].map(x=>x.dataset.accountId);
  await Promise.all(ids.map(id=>autoSaveSettings(id,true)));
}


async function removeTikTokAccount(id,username){
  if(!confirm(`Remove ${username} from the TikTok Control Head?\n\nIts persistent browser profile/history/settings will be preserved locally.`)) return;
  try{
    await api("remove_account",{account_id:id});
    await refreshNow(true);
    toast(`Removed ${username}.`);
  }catch(e){toast(e.message,true)}
}

async function addAccountPrompt(){
  const u=prompt("TikTok username to add (maximum 3 configured accounts):");
  if(!u)return;
  try{await api("add_account",{username:u});await refreshNow(true);}
  catch(e){toast(e.message,true)}
}

document.getElementById("autoRefresh").addEventListener("change",()=>refreshNow(true));
setInterval(()=>refreshNow(false),CONTROL_HEAD_REFRESH_SECONDS*1000);
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

            if action == "remove_account":
                response_json(self, remove_account(account_id))
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
    print("TikTok Control Head v25")
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
