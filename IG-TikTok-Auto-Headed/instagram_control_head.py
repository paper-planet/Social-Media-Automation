import json
import os
import random
import re
import time
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
import threading
import sys
import socket
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from http.server import SimpleHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from instagrapi import Client
from instagrapi.exceptions import FeedbackRequired, LoginRequired, ClientError, UserNotFound, PrivateError
import pyotp
import ollama
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

# --- SYSTEM SETTINGS ---
_CLI_FLAGS = {str(arg).strip().lower() for arg in sys.argv[1:]}
MANUAL_LOGIN_MODE = (
    os.environ.get("IG_MANUAL_LOGIN", "0").strip().lower() in {"1", "true", "yes", "on"}
    or bool(_CLI_FLAGS & {"--manual-login", "manual-login", "manual", "ig_manual_login=1", "ig_manual_login=true"})
)
MANUAL_LOGIN_ACCOUNT = os.environ.get("IG_MANUAL_ACCOUNT", "").strip().lstrip("@")
RUN_ACCOUNT = os.environ.get("IG_ACCOUNT", "").strip().lstrip("@")
for _arg in sys.argv[1:]:
    _low = str(_arg).strip()
    if _low.lower().startswith("ig_manual_account="):
        MANUAL_LOGIN_ACCOUNT = _low.split("=", 1)[1].strip().lstrip("@")
    elif _low.lower().startswith("--account="):
        _selected_account = _low.split("=", 1)[1].strip().lstrip("@")
        MANUAL_LOGIN_ACCOUNT = _selected_account
        RUN_ACCOUNT = _selected_account
_DEFAULT_IG_ROOT = Path.home() / ".local" / "share" / "instagram_bot" / "data"
_DOWNLOAD_OVERRIDE = os.environ.get("IG_DATA_ROOT", "").strip()
DOWNLOAD_ROOT = Path(_DOWNLOAD_OVERRIDE).expanduser() if _DOWNLOAD_OVERRIDE else _DEFAULT_IG_ROOT
ANALYTICS_FILE = DOWNLOAD_ROOT / "instagram_analytics.json"
_DEFAULT_MEDIA_ROOT = Path(os.environ.get("IG_MEDIA_ROOT", str(Path.home() / "social-media-pool"))).expanduser()
IG_HOST = "127.0.0.1"
IG_PORT = int(os.environ.get("IG_PORT", "8081"))

IG_ACCOUNTS_FILE = Path(
    os.environ.get(
        "IG_ACCOUNTS_FILE",
        str(Path.home() / ".social-control-suite" / "instagram" / "accounts.json"),
    )
).expanduser()


def _safe_account_slug(username):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(username or "").strip().lstrip("@"))
    return value.strip("._-") or "instagram_account"


def _load_external_fan_roster():
    """
    Load usernames from a private accounts file outside the repository.

    Accepted formats:
      {"accounts": ["name1", "name2"]}
    or:
      {"accounts": [{"username":"name1","enabled":true}, ...]}

    Passwords are intentionally NOT loaded from this file. Use the localhost
    Control Head login form; successful sessions are saved under IG_DATA_ROOT.
    """
    try:
        if not IG_ACCOUNTS_FILE.exists():
            return {}
        raw = json.loads(IG_ACCOUNTS_FILE.read_text(encoding="utf-8"))
        items = raw.get("accounts", []) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return {}

        roster = {}
        for item in items:
            if isinstance(item, str):
                username = item
                enabled = True
                target_hashtags = []
                target_accounts = []
            elif isinstance(item, dict):
                username = item.get("username", "")
                enabled = bool(item.get("enabled", True))
                target_hashtags = item.get("target_hashtags", [])
                target_accounts = item.get("target_accounts", [])
            else:
                continue

            username = str(username or "").strip().lstrip("@")
            if not username:
                continue

            slug = _safe_account_slug(username)
            roster[username] = {
                "enabled": enabled,
                "password_env": "",
                "totp_env": "",
                "session_file": f"session_{slug}.json",
                "history_file": f"history_{slug}.json",
                "target_hashtags": list(target_hashtags) if isinstance(target_hashtags, list) else [],
                "competitor_accounts": list(target_accounts) if isinstance(target_accounts, list) else [],
            }
        return dict(list(roster.items())[:3])
    except Exception as exc:
        print(f"⚠️ Could not load Instagram accounts file {IG_ACCOUNTS_FILE}: {exc}")
        return {}


FAN_ROSTER = _load_external_fan_roster()


MAX_ACTIONS_PER_PROFILE_RUN = 30  
ACCOUNT_COOLDOWNS, NEXT_TASK_OVERRIDE = {}, {}
AUTH_EVENT = {}  # username -> saved_session / manual_saved / manual_failed / credential_login / throttled / auth_backoff
CLIENT_CACHE = {}  # username -> authenticated instagrapi Client for this process
BROWSER_NATIVE_ACCOUNTS = set()  # valid Instagram Web sessions used when private API is blocked
BROWSER_HANDLE_BY_CONTEXT = {}
LIVE_CDP_CONTEXT_IDS = set()
LIVE_BROWSER_PROCESSES = {}
BROWSER_LOGIN_IN_PROGRESS = set()
MEDIA_FOLDER_CACHE = {"root":"", "mtime_ns":-1, "cached_at":0.0, "folders":[]}
MEDIA_FOLDER_CACHE_SECONDS = max(10, int(os.environ.get("IG_MEDIA_FOLDER_CACHE_SECONDS", "60")))
AUTH_RETRY_AFTER = {}  # username -> datetime; prevents rapid login loops

# One-account instances can keep DMs responsive without making posting/actions frequent.
DM_POLL_SECONDS = max(300, int(os.environ.get("IG_DM_POLL_SECONDS", "1800")))
WORKFLOW_SLEEP_MIN = max(DM_POLL_SECONDS, int(os.environ.get("IG_WORKFLOW_SLEEP_MIN", "420")))
WORKFLOW_SLEEP_MAX = max(WORKFLOW_SLEEP_MIN, int(os.environ.get("IG_WORKFLOW_SLEEP_MAX", "1200")))

AUTO_ACTIVE_REST_MIN_SECONDS = max(
    120,
    int(os.environ.get("IG_AUTO_ACTIVE_REST_MIN_SECONDS", "180")),
)
AUTO_ACTIVE_REST_MAX_SECONDS = max(
    AUTO_ACTIVE_REST_MIN_SECONDS,
    int(os.environ.get("IG_AUTO_ACTIVE_REST_MAX_SECONDS", "480")),
)
AUTO_LONG_REST_MIN_SECONDS = max(
    900,
    int(os.environ.get("IG_AUTO_LONG_REST_MIN_SECONDS", "2700")),
)
AUTO_LONG_REST_MAX_SECONDS = max(
    AUTO_LONG_REST_MIN_SECONDS,
    int(os.environ.get("IG_AUTO_LONG_REST_MAX_SECONDS", "10800")),
)
AUTO_PASSES_BEFORE_LONG_MIN = max(
    2,
    int(os.environ.get("IG_AUTO_PASSES_BEFORE_LONG_MIN", "3")),
)
AUTO_PASSES_BEFORE_LONG_MAX = max(
    AUTO_PASSES_BEFORE_LONG_MIN,
    int(os.environ.get("IG_AUTO_PASSES_BEFORE_LONG_MAX", "6")),
)
AUTO_FOLLOW_BATCH_MIN = max(
    1,
    min(10, int(os.environ.get("IG_AUTO_FOLLOW_BATCH_MIN", "1"))),
)
AUTO_FOLLOW_BATCH_MAX = max(
    AUTO_FOLLOW_BATCH_MIN,
    min(10, int(os.environ.get("IG_AUTO_FOLLOW_BATCH_MAX", "10"))),
)

DAILY_FOLLOW_HARD_CAP = 50


AUTO_SUCCESS_PASSES = {}
AUTO_LONG_BREAK_TARGET = {}

BURST_UNTIL = {}
LAST_BROWSER_DM_CHECK = {}
NEXT_BROWSER_DM_CHECK = {}
LAST_BROWSER_COMMENT_CHECK = {}
NEXT_BROWSER_COMMENT_CHECK = {}

ACTIVE_SESSION_MIN_SECONDS = max(
    60,
    int(os.environ.get("IG_ACTIVE_SESSION_MIN_SECONDS", "120")),
)
ACTIVE_SESSION_MAX_SECONDS = max(
    ACTIVE_SESSION_MIN_SECONDS,
    int(os.environ.get("IG_ACTIVE_SESSION_MAX_SECONDS", "300")),
)
BURST_SESSION_MIN_SECONDS = max(
    60,
    int(os.environ.get("IG_BURST_SESSION_MIN_SECONDS", "60")),
)
BURST_SESSION_MAX_SECONDS = max(
    BURST_SESSION_MIN_SECONDS,
    int(os.environ.get("IG_BURST_SESSION_MAX_SECONDS", "120")),
)
BURST_BROWSE_GAP_MIN_SECONDS = max(
    2,
    int(os.environ.get("IG_BURST_BROWSE_GAP_MIN_SECONDS", "2")),
)
BURST_BROWSE_GAP_MAX_SECONDS = max(
    BURST_BROWSE_GAP_MIN_SECONDS,
    int(os.environ.get("IG_BURST_BROWSE_GAP_MAX_SECONDS", "5")),
)
ACTIVE_BROWSE_GAP_MIN_SECONDS = max(
    3,
    int(os.environ.get("IG_ACTIVE_BROWSE_GAP_MIN_SECONDS", "5")),
)
ACTIVE_BROWSE_GAP_MAX_SECONDS = max(
    ACTIVE_BROWSE_GAP_MIN_SECONDS,
    int(os.environ.get("IG_ACTIVE_BROWSE_GAP_MAX_SECONDS", "12")),
)
OVERNIGHT_REST_MIN_SECONDS = max(
    180,
    int(os.environ.get("IG_OVERNIGHT_REST_MIN_SECONDS", "300")),
)
OVERNIGHT_REST_MAX_SECONDS = max(
    OVERNIGHT_REST_MIN_SECONDS,
    int(os.environ.get("IG_OVERNIGHT_REST_MAX_SECONDS", "720")),
)
OVERNIGHT_LONG_REST_MIN_SECONDS = max(
    1800,
    int(os.environ.get("IG_OVERNIGHT_LONG_REST_MIN_SECONDS", "3600")),
)
OVERNIGHT_LONG_REST_MAX_SECONDS = max(
    OVERNIGHT_LONG_REST_MIN_SECONDS,
    int(os.environ.get("IG_OVERNIGHT_LONG_REST_MAX_SECONDS", "14400")),
)
BROWSER_DM_CHECK_MIN_SECONDS = max(
    900,
    int(os.environ.get("IG_BROWSER_DM_CHECK_MIN_SECONDS", "2700")),
)
BROWSER_DM_CHECK_MAX_SECONDS = max(
    BROWSER_DM_CHECK_MIN_SECONDS,
    int(os.environ.get("IG_BROWSER_DM_CHECK_MAX_SECONDS", "7200")),
)
BROWSER_COMMENT_CHECK_MIN_SECONDS = max(
    1200,
    int(os.environ.get("IG_BROWSER_COMMENT_CHECK_MIN_SECONDS", "3600")),
)
BROWSER_COMMENT_CHECK_MAX_SECONDS = max(
    BROWSER_COMMENT_CHECK_MIN_SECONDS,
    int(os.environ.get("IG_BROWSER_COMMENT_CHECK_MAX_SECONDS", "10800")),
)
OLLAMA_MODEL = os.environ.get("IG_OLLAMA_MODEL", "llama3.1").strip() or "llama3.1"


BROWSER_AI_NAV_ENABLED = os.environ.get(
    "IG_BROWSER_AI_NAV_ENABLED", "1"
).strip().lower() not in {"0", "false", "no", "off"}
BROWSER_AI_NAV_MODEL = (
    os.environ.get("IG_BROWSER_AI_NAV_MODEL", OLLAMA_MODEL).strip() or OLLAMA_MODEL
)
BROWSER_AI_NAV_TIMEOUT_SECONDS = max(
    3, min(45, int(os.environ.get("IG_BROWSER_AI_NAV_TIMEOUT_SECONDS", "12")))
)
BROWSER_AI_NAV_MAX_STEPS = max(
    1, min(5, int(os.environ.get("IG_BROWSER_AI_NAV_MAX_STEPS", "3")))
)

OLLAMA_VISION_MODEL = os.environ.get("IG_OLLAMA_VISION_MODEL", "").strip()
VISION_MODEL_CACHE = {"checked": False, "name": ""}

VISIBILITY_VERIFY_ATTEMPTS = max(1, int(os.environ.get("IG_VISIBILITY_VERIFY_ATTEMPTS", "4")))
VISIBILITY_VERIFY_DELAY = max(5, int(os.environ.get("IG_VISIBILITY_VERIFY_DELAY", "15")))
DM_AUTO_APPROVE_REQUESTS = os.environ.get("IG_DM_AUTO_APPROVE_REQUESTS", "1").strip().lower() in {"1", "true", "yes", "on"}

DM_MAX_MESSAGE_AGE_HOURS = max(1, int(os.environ.get("IG_DM_MAX_MESSAGE_AGE_HOURS", "24")))

# --- CONSERVATIVE WRITE PACING ---
# These govern WRITE actions only (likes, follows, comments, DMs, uploads).
# Read/polling actions such as DM checks do not consume the write budget.
WRITE_MIN_GAP_SECONDS = max(10, int(os.environ.get("IG_WRITE_MIN_GAP_SECONDS", "40")))
GLOBAL_WRITE_MIN_GAP_SECONDS = max(8, int(os.environ.get("IG_GLOBAL_WRITE_MIN_GAP_SECONDS", "18")))
WRITE_WINDOW_SECONDS = max(60, int(os.environ.get("IG_WRITE_WINDOW_SECONDS", "1200")))
MAX_WRITES_PER_WINDOW = max(1, int(os.environ.get("IG_MAX_WRITES_PER_WINDOW", "6")))
MAX_WRITES_PER_WORKFLOW = max(1, int(os.environ.get("IG_MAX_WRITES_PER_WORKFLOW", "3")))

BROWSER_FOLLOW_MIN_GAP_SECONDS = max(
    4,
    min(
        30,
        int(os.environ.get("IG_BROWSER_FOLLOW_MIN_GAP_SECONDS", "8")),
    ),
)
BROWSER_FOLLOW_VERIFY_SECONDS = max(
    2,
    min(
        15,
        int(os.environ.get("IG_BROWSER_FOLLOW_VERIFY_SECONDS", "5")),
    ),
)
BROWSER_LAST_FOLLOW_ACTION = {}
UPLOAD_COOLDOWN_SECONDS = max(300, int(os.environ.get("IG_UPLOAD_COOLDOWN_SECONDS", "2400")))
DM_REPLY_COOLDOWN_SECONDS = max(30, int(os.environ.get("IG_DM_REPLY_COOLDOWN_SECONDS", "120")))
DM_MIN_INCOMING_AGE_SECONDS = max(0, int(os.environ.get("IG_DM_MIN_INCOMING_AGE_SECONDS", "90")))
DM_THREAD_REPLY_COOLDOWN_SECONDS = max(60, int(os.environ.get("IG_DM_THREAD_REPLY_COOLDOWN_SECONDS", "1800")))
MAX_DM_REPLIES_PER_PASS = max(1, int(os.environ.get("IG_MAX_DM_REPLIES_PER_PASS", "1")))
MAX_COMMENT_REPLIES_PER_PASS = max(1, int(os.environ.get("IG_MAX_COMMENT_REPLIES_PER_PASS", "1")))
ACTION_HUMAN_DELAY_MIN_SECONDS = max(0, int(os.environ.get("IG_ACTION_HUMAN_DELAY_MIN_SECONDS", "8")))
ACTION_HUMAN_DELAY_MAX_SECONDS = max(ACTION_HUMAN_DELAY_MIN_SECONDS, int(os.environ.get("IG_ACTION_HUMAN_DELAY_MAX_SECONDS", "18")))
CONTROL_HEAD_REFRESH_SECONDS = max(5, int(os.environ.get("IG_CONTROL_HEAD_REFRESH_SECONDS", "10")))

# Keep Browser Login open after session detection so Instagram post-login
# prompts (e.g. "Save login info") can be completed manually.
BROWSER_LOGIN_FINISH_SECONDS = max(
    15,
    min(
        600,
        int(os.environ.get("IG_BROWSER_LOGIN_FINISH_SECONDS", "90")),
    ),
)


BROWSER_POST_AI_TIMEOUT_SECONDS = max(
    90,
    min(
        600,
        int(os.environ.get("IG_BROWSER_POST_AI_TIMEOUT_SECONDS", "300")),
    ),
)
BROWSER_POST_VISION_FRAMES = max(
    3,
    min(
        5,
        int(os.environ.get("IG_BROWSER_POST_VISION_FRAMES", "3")),
    ),
)

WORKFLOW_RETRY_IDLE_SECONDS = max(15, int(os.environ.get("IG_WORKFLOW_RETRY_IDLE_SECONDS", "45")))

ACCOUNT_WRITE_TIMES = {}
ACCOUNT_LAST_WRITE = {}
ACCOUNT_LAST_UPLOAD = {}
ACCOUNT_LAST_DM_REPLY = {}
GLOBAL_LAST_WRITE = 0.0
WRITE_LOCK = threading.RLock()
DM_TEST_MODE = bool(_CLI_FLAGS & {"--dm-test", "dm-test"})
DM_ONLY_MODE = bool(_CLI_FLAGS & {"--dm-only", "dm-only"})

LAST_DM_POLL = {}

# --- CONTROL HEAD RUNTIME STATE ---
# Populated in main(). Accounts begin disconnected on every process start.
CONTROL_ROSTER = {}
CONTROL_DISCONNECTED_ACCOUNTS = set()
CONTROL_PAUSED_ACCOUNTS = set()  # connected accounts with background automation OFF
CONTROL_FORCE_RUN = set()
CONTROL_LOCK = threading.RLock()

ACCOUNT_SAFETY_LOCK = threading.RLock()


def _account_safety_path():
    return DOWNLOAD_ROOT / "account_safety_state.json"


def _load_account_safety_db():
    try:
        path = _account_safety_path()
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
    except Exception:
        pass
    return {}


def _save_account_safety_db(value):
    path = _account_safety_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def get_account_safety_state(username):
    username = str(username or "").strip().lstrip("@")
    with ACCOUNT_SAFETY_LOCK:
        row = _load_account_safety_db().get(username, {})
        row = row if isinstance(row, dict) else {}

    until_ts = float(row.get("until_ts") or 0.0)
    return {
        "active": until_ts > time.time(),
        "level": str(row.get("level") or ""),
        "reason": str(row.get("reason") or ""),
        "detected_at": str(row.get("detected_at") or ""),
        "until_ts": until_ts,
        "until": datetime.fromtimestamp(until_ts).strftime("%Y-%m-%d %H:%M") if until_ts else "",
    }


def apply_account_safety_backoff(username, reason, level="restricted", hours=None):
    """
    Honor Instagram restriction/throttle feedback by stopping writes on the
    affected account. Saved feature checkboxes are preserved; the safety state
    temporarily overrides them.
    """
    username = str(username or "").strip().lstrip("@")
    level = str(level or "restricted").lower()

    if hours is None:
        hours = {
            "restricted": 12,
            "throttle": 2,
            "verification": 4,
        }.get(level, 2)

    reason = re.sub(r"\s+", " ", str(reason or "Instagram restriction signal")).strip()[:500]
    until_ts = time.time() + float(hours) * 3600.0

    with ACCOUNT_SAFETY_LOCK:
        db = _load_account_safety_db()
        previous = db.get(username, {})
        if isinstance(previous, dict):
            until_ts = max(until_ts, float(previous.get("until_ts") or 0.0))
        db[username] = {
            "level": level,
            "reason": reason,
            "detected_at": datetime.now().isoformat(timespec="seconds"),
            "until_ts": until_ts,
        }
        _save_account_safety_db(db)

    until_dt = datetime.fromtimestamp(until_ts)
    ACCOUNT_COOLDOWNS[username] = until_dt
    CONTROL_PAUSED_ACCOUNTS.add(username)
    CONTROL_FORCE_RUN.discard(username)
    NEXT_TASK_OVERRIDE.pop(username, None)

    update_account_metric(username, "cooldown_until", value=until_dt.strftime("%Y-%m-%d %H:%M"))
    update_account_metric(username, "status", status="Safety Backoff")
    update_account_metric(
        username,
        "add_history",
        value=(
            f"🛡️ Safety backoff ({level}) until {until_dt.strftime('%Y-%m-%d %H:%M')}: "
            f"{reason[:180]}"
        ),
    )
    print(
        f"🛡️ @{username}: Safety Backoff ({level}) until "
        f"{until_dt.strftime('%Y-%m-%d %H:%M')} — {reason}"
    )
    return get_account_safety_state(username)


def classify_platform_signal(value):
    message = re.sub(r"\s+", " ", str(value or "")).strip()
    lower = message.lower()

    restricted_terms = (
        "we restrict certain activity",
        "we restrict how often",
        "action blocked",
        "account functionality",
        "functionality reduced",
        "functionality is being reduced",
        "features are limited",
        "feature is limited",
        "account has been restricted",
        "account is restricted",
        "temporarily blocked",
        "certain activity to protect our community",
    )
    throttle_terms = (
        "feedback_required",
        "feedback required",
        "please wait a few minutes",
        "please wait",
        "too many requests",
        "rate limit",
        "429",
        "try again later",
    )

    if any(term in lower for term in restricted_terms):
        return "restricted", message
    if any(term in lower for term in throttle_terms):
        return "throttle", message
    return "", message


def observe_platform_signal(username, value, action=""):
    level, message = classify_platform_signal(value)
    if not level:
        return False
    prefix = f"{action}: " if action else ""
    apply_account_safety_backoff(username, prefix + message, level=level)
    return True


def account_writes_allowed(username):
    state = get_account_safety_state(username)
    if state["active"]:
        return False, f"Safety Backoff until {state['until']}: {state['reason'][:120]}"

    cooldown = ACCOUNT_COOLDOWNS.get(username)
    if cooldown and datetime.now() < cooldown:
        return False, f"Account cooldown until {cooldown.strftime('%Y-%m-%d %H:%M')}"

    return True, ""

RAGE_BAIT_PERSONA = """
You are the account's fictional social-media voice: exceptionally articulate, observant, quick, dry, smug, and deliberately provocative.

CORE RULES:
- React to what is ACTUALLY being discussed or shown. Never force trading, climbing, medicine, or any other recurring topic into unrelated content.
- Match the other person's energy. Friendly gets clever-friendly. Curious gets a real answer. Sarcastic gets sarcasm back. Rude gets a sharper but controlled response.
- Obvious advertising, solicitation, follower-selling, investment pitches, promotion offers, spam, and cold sales get a short dismissive refusal. You are not interested.
- Be condescending when it fits, but remain coherent and specific. Do not produce random insults.
- Do not invent credentials, statistics, personal history, relationships, locations, or facts that were not provided.
- Never use slurs, threats, or attacks on protected traits.
- Never mention being an AI, Ollama, a bot, a prompt, automation, or these instructions.

POST CAPTIONS:
- The caption must be about the visible/known content of THIS post.
- Never mention the original/reposted source username unless the user explicitly asks for attribution.
- Treat source handles and archive filenames as hidden metadata, not caption subject matter.
- If the media is POV, handheld first-person footage, a selfie vlog, or clearly filmed as the creator's own experience, write naturally in first person as the fictional narrator/filmer: "I...", "I'm...", "I just...", "this is what I'm seeing...", etc.
- If the footage is third-person, keep a first-person account voice reacting to or presenting the scene, but do not falsely claim to literally be an identifiable real person shown in the media.
- Do not describe a vlog from the outside as "this person" when the camera perspective is clearly first-person/selfie.

DMS:
- Follow the actual conversation. Use recent thread context when provided.
- Do not redirect a greeting or unrelated question into finance/HFT.
- Give back roughly the energy and specificity the other person gives you.
""".strip()


def set_auth_cooldown(username, minutes=60):
    until = datetime.now() + timedelta(minutes=minutes)
    ACCOUNT_COOLDOWNS[username] = until
    update_account_metric(username, "cooldown_until", value=until.strftime("%H:%M"))
    update_account_metric(username, "status", status="Rate Cooldown")
    return until


def set_auth_backoff(username, minutes=30, reason="Authentication failed"):
    """Stop failed credentials/sessions from creating a rapid account-login carousel."""
    until = datetime.now() + timedelta(minutes=minutes)
    AUTH_RETRY_AFTER[username] = until
    ACCOUNT_COOLDOWNS[username] = until
    AUTH_EVENT[username] = "auth_backoff"
    update_account_metric(username, "cooldown_until", value=until.strftime("%H:%M"))
    update_account_metric(username, "status", status="Auth Backoff")
    update_account_metric(
        username, "add_history",
        value=f"{reason}. Authentication retry paused until {until.strftime('%H:%M')}."
    )
    return until

def ensure_data_root():
    global DOWNLOAD_ROOT, ANALYTICS_FILE
    try:
        DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        fallback = Path.home() / ".local" / "share" / "instagram_bot" / "data"
        print(f"⚠️ {DOWNLOAD_ROOT} is not writable; using {fallback} instead.")
        DOWNLOAD_ROOT = fallback
        ANALYTICS_FILE = fallback / "instagram_analytics.json"
        fallback.mkdir(parents=True, exist_ok=True)


def _global_control_settings_path():
    return DOWNLOAD_ROOT / "control_head_global_settings.json"


def load_global_control_settings():
    try:
        path = _global_control_settings_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_global_control_settings(data):
    ensure_data_root()
    path = _global_control_settings_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def get_media_root() -> Path:
    data = load_global_control_settings()
    raw = str(data.get("media_root", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _DEFAULT_MEDIA_ROOT


def media_root_payload():
    root = get_media_root()
    try:
        resolved = root.resolve()
    except Exception:
        resolved = root

    exists = resolved.exists()
    is_dir = resolved.is_dir() if exists else False

    child_folders = 0
    try:
        if is_dir:
            child_folders = sum(
                1
                for item in resolved.iterdir()
                if item.is_dir()
            )
    except Exception:
        child_folders = 0

    return {
        "path": str(resolved),
        "exists": bool(exists),
        "is_dir": bool(is_dir),
        "child_folders": int(child_folders),
        "source": (
            "saved"
            if str(load_global_control_settings().get("media_root", "") or "").strip()
            else "default"
        ),
    }


def _control_set_media_root(path_value):
    raw = str(path_value or "").strip().strip('"')
    if not raw:
        raise ValueError("Choose or enter a media folder path.")

    root = Path(raw).expanduser()
    try:
        resolved = root.resolve()
    except Exception:
        resolved = root

    if not resolved.exists():
        raise FileNotFoundError(
            f"Media folder does not exist: {resolved}"
        )
    if not resolved.is_dir():
        raise ValueError(
            f"Media path is not a folder: {resolved}"
        )

    data = load_global_control_settings()
    data["media_root"] = str(resolved)
    save_global_control_settings(data)

    print(
        f"📁 Instagram media root changed from Control Head -> {resolved}",
        flush=True,
    )

    for username in CONTROL_ROSTER:
        update_account_metric(
            username,
            "add_history",
            value=f"📁 Media folder changed to: {resolved}",
        )

    return {
        "ok": True,
        "media": media_root_payload(),
    }


def _control_choose_media_root():
    """
    Open a native local folder picker on the computer running the bot.

    This is intentionally local-only: the dashboard listens on 127.0.0.1.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError(
            "Native folder picker is unavailable. Paste the folder path "
            "into Media Library instead."
        ) from exc

    current = get_media_root()
    initial = current if current.exists() else Path.home()

    root = None
    try:
        root = tk.Tk()
        root.withdraw()

        try:
            root.attributes("-topmost", True)
        except Exception:
            pass

        try:
            root.update()
        except Exception:
            pass

        chosen = filedialog.askdirectory(
            parent=root,
            initialdir=str(initial),
            title="Choose Instagram media folder",
            mustexist=True,
        )
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    if not chosen:
        return {
            "ok": True,
            "cancelled": True,
            "media": media_root_payload(),
        }

    result = _control_set_media_root(chosen)
    result["cancelled"] = False
    return result


def _control_open_media_root():
    root = get_media_root()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(
            f"Configured media folder does not exist: {root}"
        )

    resolved = root.resolve()

    if os.name == "nt":
        os.startfile(str(resolved))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(resolved)])
    else:
        subprocess.Popen(["xdg-open", str(resolved)])

    return {
        "ok": True,
        "folder": str(resolved),
    }

def _control_settings_path():
    return DOWNLOAD_ROOT / "control_head_settings.json"


def _default_control_settings(username, conf):
    return {
        "persona_prompt": RAGE_BAIT_PERSONA,
        "caption_prompt": (
            "Narrate what is actually happening in the media. For POV/selfie/vlog "
            "footage, speak naturally in first person as the fictional narrator/filmer. "
            "Be smart, dry, confident, and provocative only when it fits the scene."
        ),
        "dm_prompt": (
            "Follow the actual conversation, answer what the person said, match their "
            "energy, reject obvious advertising, and do not drag unrelated topics into trading."
        ),
        "comment_prompt": (
            "Reply to the actual comment with a concise, context-aware response. "
            "Match the commenter's energy without repeating stock phrases."
        ),
        "caption_char_limit": 420,
        "reply_char_limit": 280,
        "target_hashtags": list(conf.get("target_hashtags") or []),
        "target_accounts": list(conf.get("competitor_accounts") or []),
        "follow_source": "both",
        "follow_limit": 2,
        "auto_follow_batch_max": 10,
        "active_session_max_passes": 8,
        "engage_clips_per_pass": 12,
        "engage_scroll_steps": 8,
        "engage_like_percent": 75,
        "engage_repost_percent": 75,
        "engage_comment_percent": 10,
        "engage_follow_percent": 25,
        "pace_mode": "normal",
        "video_frames": 5,
        "require_video_vision": True,

        # Independent behavior switches. There is no hidden mode routing.
        "enable_posts": True,
        "enable_follow": True,
        "enable_engage": True,
        "enable_dms": True,
        "enable_comments": True,

        "max_writes_per_window": MAX_WRITES_PER_WINDOW,
        "write_window_seconds": WRITE_WINDOW_SECONDS,
        "write_min_gap_seconds": WRITE_MIN_GAP_SECONDS,
        "upload_cooldown_seconds": UPLOAD_COOLDOWN_SECONDS,
        "max_writes_per_workflow": MAX_WRITES_PER_WORKFLOW,
        "max_dm_replies_per_pass": MAX_DM_REPLIES_PER_PASS,
        "max_comment_replies_per_pass": MAX_COMMENT_REPLIES_PER_PASS,
    }



def _normalize_string_list(value, prefix_to_strip=""):
    if isinstance(value, str):
        parts = re.split(r"[\n,]+", value)
    elif isinstance(value, list):
        parts = value
    else:
        parts = []
    out=[]
    for item in parts:
        s=str(item or "").strip()
        if prefix_to_strip:
            s=s.lstrip(prefix_to_strip)
        if s and s not in out:
            out.append(s)
    return out[:100]


def _normalize_hashtag_list(value):
    """
    Convert Control Head hashtag input to request-safe tag names.

    Examples:
      #xauusd          -> xauusd
      "xauusd🔥🔥"     -> xauusd
      gold trading    -> goldtrading

    Only letters, digits and underscore are sent to Instagram's tag endpoint.
    """
    if isinstance(value, str):
        parts = re.split(r"[\n,]+", value)
    elif isinstance(value, list):
        parts = value
    else:
        parts = []

    out = []
    for item in parts:
        s = str(item or "").strip().strip("\"'")
        s = s.lstrip("#")
        s = re.sub(r"\s+", "", s)
        s = re.sub(r"[^A-Za-z0-9_]", "", s)
        if s and s not in out:
            out.append(s)
    return out[:100]


def _sanitize_control_settings(username, conf, raw):
    base = _default_control_settings(username, conf)
    raw = raw if isinstance(raw, dict) else {}

    follow_source = str(raw.get("follow_source", base["follow_source"])).strip().lower()
    if follow_source not in {"followers", "following", "both"}:
        follow_source = "both"

    pace_mode = str(raw.get("pace_mode", base.get("pace_mode", "normal"))).strip().lower()
    if pace_mode not in {"normal", "overnight"}:
        pace_mode = "normal"

    def as_int(key, lo, hi):
        try:
            v = int(raw.get(key, base[key]))
        except Exception:
            v = int(base[key])
        return max(lo, min(hi, v))

    # Migrate old mode-based settings once, but do not return/store a mode.
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

    def feature(name, legacy_index, old_alias=None):
        if name in raw:
            return bool(raw[name])
        if old_alias and old_alias in raw:
            return bool(raw[old_alias])
        if legacy_mode:
            return bool(legacy[legacy_index])
        return bool(base[name])

    return {
        "persona_prompt": str(raw.get("persona_prompt", base["persona_prompt"]))[:6000],
        "caption_prompt": str(raw.get("caption_prompt", base["caption_prompt"]))[:4000],
        "dm_prompt": str(raw.get("dm_prompt", base["dm_prompt"]))[:4000],
        "comment_prompt": str(raw.get("comment_prompt", base["comment_prompt"]))[:4000],
        "caption_char_limit": as_int("caption_char_limit", 80, 2200),
        "reply_char_limit": as_int("reply_char_limit", 20, 1000),
        "target_hashtags": _normalize_hashtag_list(
            raw.get("target_hashtags", base["target_hashtags"])
        ),
        "target_accounts": _normalize_string_list(
            raw.get("target_accounts", base["target_accounts"]), "@"
        ),
        "follow_source": follow_source,
        "follow_limit": as_int("follow_limit", 1, 20),
        "auto_follow_batch_max": as_int("auto_follow_batch_max", 1, 10),
        "active_session_max_passes": as_int("active_session_max_passes", 1, 20),
        "engage_clips_per_pass": as_int("engage_clips_per_pass", 1, 20),
        "engage_scroll_steps": as_int("engage_scroll_steps", 1, 20),
        "engage_like_percent": as_int("engage_like_percent", 0, 100),
        "engage_repost_percent": as_int("engage_repost_percent", 0, 100),
        "engage_comment_percent": as_int("engage_comment_percent", 0, 100),
        "engage_follow_percent": as_int("engage_follow_percent", 0, 100),
        "pace_mode": pace_mode,
        "video_frames": as_int("video_frames", 3, 9),
        "require_video_vision": bool(raw.get("require_video_vision", base["require_video_vision"])),

        "enable_posts": feature("enable_posts", 0),
        "enable_follow": feature("enable_follow", 1),
        "enable_engage": feature("enable_engage", 2),
        "enable_dms": feature("enable_dms", 3, "enable_dm_replies"),
        "enable_comments": feature("enable_comments", 4, "enable_comment_replies"),

        "max_writes_per_window": as_int("max_writes_per_window", 1, 30),
        "write_window_seconds": as_int("write_window_seconds", 60, 7200),
        "write_min_gap_seconds": as_int("write_min_gap_seconds", 8, 600),
        "upload_cooldown_seconds": as_int("upload_cooldown_seconds", 300, 21600),
        "max_writes_per_workflow": as_int("max_writes_per_workflow", 1, 10),
        "max_dm_replies_per_pass": as_int("max_dm_replies_per_pass", 1, 5),
        "max_comment_replies_per_pass": as_int("max_comment_replies_per_pass", 1, 5),
    }



def load_control_settings():
    try:
        p=_control_settings_path()
        if p.exists():
            data=json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_control_settings(data):
    ensure_data_root()
    p=_control_settings_path()
    tmp=p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding="utf-8")
    tmp.replace(p)


def get_account_control_settings(username, conf=None):
    if conf is None:
        conf=CONTROL_ROSTER.get(username) or FAN_ROSTER.get(username) or {}
    all_settings=load_control_settings()
    return _sanitize_control_settings(
        username, conf, all_settings.get(username, {})
    )


def update_account_control_settings(username, patch):
    conf = CONTROL_ROSTER.get(username) or FAN_ROSTER.get(username)
    if not conf:
        raise ValueError(f"Unknown account @{username}")
    if not isinstance(patch, dict):
        raise ValueError("settings payload must be an object")

    data = load_control_settings()
    before = _sanitize_control_settings(username, conf, data.get(username, {}))
    merged = dict(data.get(username, {}))
    merged.update(patch)
    clean = _sanitize_control_settings(username, conf, merged)

    # Auto-refresh may submit settings repeatedly. Only touch disk/log when changed.
    if clean != before:
        data[username] = clean
        save_control_settings(data)
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
        update_account_metric(
            username,
            "add_history",
            value="⚙️ Behavior auto-saved: " + (", ".join(enabled) if enabled else "all features off"),
        )
    return clean



def _clip_chars(text, limit):
    text=str(text or "").strip()
    if len(text)<=limit:
        return text
    cut=text[:max(1,limit)].rstrip()
    for mark in (". ","! ","? "):
        pos=cut.rfind(mark)
        if pos >= max(20, int(limit*0.55)):
            return cut[:pos+1].strip()
    return cut.rstrip(" ,;:-") + "…"


def _effective_pacing(username):
    s=get_account_control_settings(username)
    return {
        "max_window": int(s["max_writes_per_window"]),
        "window": int(s["write_window_seconds"]),
        "gap": int(s["write_min_gap_seconds"]),
        "upload": int(s["upload_cooldown_seconds"]),
        "workflow": int(s["max_writes_per_workflow"]),
    }


def _new_account_metrics():
    return {
        "status": "Idle",
        "total_posts": 0,
        "total_follows": 0,
        "total_likes": 0,
        "cooldown_until": "None",
        "history_log": [],
        "growth_timeline": [
            {
                "date": (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"),
                "followers": 1000 + random.randint(-50, 200),
            }
            for i in reversed(range(7))
        ],
    }


def load_analytics():
    """Load metrics and make the dashboard match FAN_ROSTER exactly.

    Removing/commenting an account out of FAN_ROSTER removes its stale dashboard
    card too. Adding a new roster account automatically creates a metrics row.
    """
    ensure_data_root()
    data = {}
    if ANALYTICS_FILE.exists():
        try:
            with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
        except Exception:
            data = {}

    active_users = set(FAN_ROSTER.keys())
    data = {user: metrics for user, metrics in data.items() if user in active_users}
    for user in FAN_ROSTER:
        data.setdefault(user, _new_account_metrics())
    return data

def save_analytics(data):
    try:
        with open(ANALYTICS_FILE, "w") as f: json.dump(data, f, indent=4)
    except Exception as e: print(f"❌ Metrics sync block error: {e}")

def update_account_metric(username, key, value=None, increment=1, status=None):
    db = load_analytics()
    if username in db:
        if status:
            db[username]["status"] = status
        if key in ["total_posts", "total_follows", "total_likes"]:
            db[username][key] += increment
        elif key == "cooldown_until":
            db[username][key] = str(value)
        elif key == "add_history":
            stamp = datetime.now().strftime("%H:%M:%S")
            message = str(value)
            db[username]["history_log"].insert(0, f"[{stamp}] {message}")
            db[username]["history_log"] = db[username]["history_log"][:15]

            # Browser Mode previously only wrote progress to the dashboard,
            # which made long AI/upload steps look frozen in the console.
            try:
                print(f"[{stamp}] @{username}: {message}", flush=True)
            except Exception:
                pass

        elif key == "sync_followers":
            today = datetime.now().strftime("%Y-%m-%d")
            timeline = db[username]["growth_timeline"]
            if timeline and timeline[-1]["date"] == today:
                timeline[-1]["followers"] = value
            else:
                timeline.append({"date": today, "followers": value})
                if len(timeline) > 30:
                    timeline.pop(0)

    save_analytics(db)


def load_json(filepath, default_data):
    p = DOWNLOAD_ROOT / filepath if not Path(filepath).is_absolute() else Path(filepath)
    if p.exists():
        try:
            with open(p, "r") as f:
                data = json.load(f)
                for key in ["posted_ids", "replied_dms", "replied_comments", "liked_medias", "commented_medias", "followed_users", "blocked_or_missing", "competitor_pool", "followers_next_max_id", "following_next_max_id", "dm_reply_memory"]:
                    if key not in data:
                        if key.endswith("max_id"):
                            data[key] = None
                        elif key == "dm_reply_memory":
                            data[key] = {}
                        else:
                            data[key] = []
                return data
        except Exception: pass
    return default_data

def save_json(filepath, data):
    p = DOWNLOAD_ROOT / filepath if not Path(filepath).is_absolute() else Path(filepath)
    try:
        with open(p, "w") as f: json.dump(data, f, indent=4)
    except Exception as e: print(f"❌ Failed registry sync: {e}")

def generate_2fa(secret):
    if not secret: return None
    return pyotp.TOTP(secret.replace(" ", "")).now()

def analyze_context_topic(text):
    text = str(text or "").lower()

    if any(k in text for k in [
        "xau", "xauusd", "gold", "hft", "algorithmic trading",
        "day trade", "scalping", "leverage", "liquidation", "quant"
    ]):
        return "hft"

    if any(k in text for k in [
        "climb", "boulder", "v10", "v9", "v8", "v7", "v6", "v5",
        "route", "crimp", "dyno", "send", "moonboard", "kilter"
    ]):
        return "climbing"

    if any(k in text for k in [
        "medical", "doctor", "clinical", "rehab", "hospital",
        "patient", "treatment", "therapy", "medicine"
    ]):
        return "medical"

    return "general"


def _clean_ollama_output(raw):
    text = str(raw or "").strip().strip('"').strip()
    for prefix in ("Caption:", "Reply:", "Response:", "Assistant:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    return " ".join(text.split())


def _ollama_generate(
    task_prompt,
    min_words=8,
    max_words=70,
    attempts=4,
    system_prompt=None,
):
    system_prompt = str(system_prompt or RAGE_BAIT_PERSONA).strip()
    for attempt in range(1, attempts + 1):
        repair = ""
        if attempt > 1:
            repair = (
                "\nIMPORTANT REWRITE: the previous answer was rejected as incomplete "
                "or malformed. Start over and write one polished, grammatically complete "
                "thought with a decisive ending."
            )
        try:
            response = ollama.chat(
                model=OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task_prompt + repair},
                ],
                options={"temperature": 0.82, "top_p": 0.90},
            )
            candidate = _clean_ollama_output(
                response.get("message", {}).get("content", "")
            )
        except Exception:
            continue

        words = candidate.split()
        lower = candidate.lower()
        complete_end = bool(candidate) and candidate[-1] in ".!?…)]}"
        obvious_fragment = (
            not candidate
            or candidate.endswith((",", ":", ";", "-", "—"))
            or lower.endswith((" and", " but", " or", " because", " so", " if"))
        )
        contains_meta = any(
            token in lower
            for token in ("as an ai", "ollama", "language model", "here is the reply")
        )

        if (
            min_words <= len(words) <= max_words
            and complete_end
            and not obvious_fragment
            and not contains_meta
        ):
            return candidate
    return None





def _sanitize_post_context(raw_text, source_account=""):
    """
    Keep semantic clues but remove source-account attribution/handles so the
    generated caption does not start talking about the original poster.
    """
    value = str(raw_text or "")
    if source_account:
        value = re.sub(
            rf"@?{re.escape(source_account)}\b",
            "",
            value,
            flags=re.IGNORECASE,
        )

    # Remove generic social handles and archive metadata-looking fragments.
    value = re.sub(r"(?<!\w)@[A-Za-z0-9._]{2,}", "", value)
    value = re.sub(
        r"\b(?:source|original\s*(?:poster|account|creator)|reposted\s*from)\s*:\s*\S+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value).strip()
    return value[:3500]


def _available_ollama_models():
    names = []
    try:
        listing = ollama.list()
        models = (
            listing.get("models", [])
            if isinstance(listing, dict)
            else getattr(listing, "models", [])
        )
        for item in models or []:
            if isinstance(item, dict):
                name = item.get("model") or item.get("name") or ""
            else:
                name = getattr(item, "model", "") or getattr(item, "name", "")
            if name:
                names.append(str(name))
    except Exception:
        pass
    return names


def get_ollama_vision_model():
    """
    Prefer IG_OLLAMA_VISION_MODEL when set; otherwise auto-detect a likely
    locally installed multimodal Ollama model. No model is downloaded.
    """
    if OLLAMA_VISION_MODEL:
        return OLLAMA_VISION_MODEL

    if VISION_MODEL_CACHE["checked"]:
        return VISION_MODEL_CACHE["name"]

    VISION_MODEL_CACHE["checked"] = True
    candidates = _available_ollama_models()

    preferred_tokens = (
        "qwen3-vl", "qwen2.5vl", "qwen2-vl", "gemma3",
        "llava", "minicpm-v", "moondream", "bakllava"
    )

    for token in preferred_tokens:
        for name in candidates:
            if token in name.lower():
                VISION_MODEL_CACHE["name"] = name
                return name

    return ""



def _video_duration_seconds(video_path):
    candidates=[]
    ffprobe=shutil.which("ffprobe")
    if ffprobe:
        candidates.append(ffprobe)
    ffmpeg=shutil.which("ffmpeg")
    if ffmpeg:
        sibling=Path(ffmpeg).with_name("ffprobe.exe" if os.name=="nt" else "ffprobe")
        if sibling.exists():
            candidates.append(str(sibling))
    for exe in candidates:
        try:
            r=subprocess.run(
                [exe,"-v","error","-show_entries","format=duration",
                 "-of","default=noprint_wrappers=1:nokey=1",str(video_path)],
                check=True,capture_output=True,text=True,timeout=20
            )
            d=float(r.stdout.strip())
            if d>0:
                return d
        except Exception:
            pass
    try:
        from moviepy import VideoFileClip
        clip=VideoFileClip(str(video_path))
        try:
            d=float(clip.duration or 0)
        finally:
            clip.close()
        return d if d>0 else None
    except Exception:
        return None


def extract_video_story_frames(video_path, count=5):
    video_path=Path(video_path)
    count=max(3,min(9,int(count)))
    root=DOWNLOAD_ROOT/"video_story_frames"
    root.mkdir(parents=True,exist_ok=True)
    stamp=f"{video_path.stat().st_size}_{video_path.stat().st_mtime_ns}"
    safe="".join(c if c.isalnum() or c in "-_" else "_" for c in video_path.stem)[:70]
    duration=_video_duration_seconds(video_path)

    if duration and duration>1:
        positions=[duration*((i+1)/(count+1)) for i in range(count)]
    else:
        positions=[0.2 + i*1.0 for i in range(count)]

    ffmpeg_candidates=[]
    env_ffmpeg=os.environ.get("IMAGEIO_FFMPEG_EXE","").strip()
    if env_ffmpeg:
        ffmpeg_candidates.append(env_ffmpeg)
    pff=shutil.which("ffmpeg")
    if pff and pff not in ffmpeg_candidates:
        ffmpeg_candidates.append(pff)
    try:
        import imageio_ffmpeg
        b=imageio_ffmpeg.get_ffmpeg_exe()
        if b and b not in ffmpeg_candidates:
            ffmpeg_candidates.append(b)
    except Exception:
        pass

    frames=[]
    for idx,sec in enumerate(positions,1):
        out=root/f"{safe}_{stamp}_{count}_{idx}.jpg"
        if out.exists() and out.stat().st_size>1024:
            frames.append(out); continue
        made=False
        for exe in ffmpeg_candidates:
            try:
                subprocess.run(
                    [exe,"-hide_banner","-loglevel","error","-y",
                     "-ss",f"{sec:.3f}","-i",str(video_path),
                     "-frames:v","1",
                     "-vf","scale=960:-2:force_original_aspect_ratio=decrease",
                     "-q:v","3",str(out)],
                    check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=45
                )
                if out.exists() and out.stat().st_size>1024:
                    frames.append(out); made=True; break
            except Exception:
                pass
        if not made and out.exists():
            try: out.unlink()
            except Exception: pass
    return frames


def analyze_media_for_caption(
    media_files,
    sidecar_text="",
    source_account="",
    frame_count=5,
    require_video_vision=True,
):
    clean_text=_sanitize_post_context(sidecar_text,source_account)
    vision_model=get_ollama_vision_model()

    video=next((p for p in media_files if p.suffix.lower()==".mp4"),None)
    image=next((p for p in media_files if p.suffix.lower() in {".jpg",".jpeg",".png"}),None)

    media_kind="video" if video else ("image" if image else "unknown")
    visual_files=[]
    if video:
        try:
            visual_files=extract_video_story_frames(video,frame_count)
        except Exception:
            visual_files=[]
    elif image:
        visual_files=[image]

    if video and require_video_vision and (not vision_model or len(visual_files)<3):
        return {
            "context":"",
            "perspective":"UNKNOWN",
            "vision_model":vision_model,
            "media_kind":"video",
            "vision_required_failed":True,
            "frames_analyzed":len(visual_files),
        }

    visual_description=""
    perspective="UNKNOWN"

    if vision_model and visual_files:
        if media_kind=="video":
            prompt = """
These images are chronological frames sampled across ONE social-media video.
Actually watch the sequence by comparing all frames from earliest to latest.

Return:
PERSPECTIVE: POV_FIRST_PERSON | SELFIE_VLOG | THIRD_PERSON | UNKNOWN
SEQUENCE: 3-7 concise sentences explaining what happens over time, in order.
NARRATION_NOTES: a concise first-person-friendly description suitable for a post caption.

Rules:
- Base the answer only on visible evidence across the frames.
- Notice changes between frames, actions, movement, setting, objects, and readable text.
- If this is POV/selfie/vlog footage, make that explicit.
- Do not identify people by name or infer private traits.
- Do not mention usernames, filenames, source accounts, reposting, or archive metadata.
- Do not introduce trading or another topic unless it is visibly present.
""".strip()
        else:
            prompt = """
Analyze this social-media image for caption writing.
Return:
PERSPECTIVE: SELFIE_VLOG | THIRD_PERSON | UNKNOWN
SEQUENCE: 1-3 concise sentences describing what is visibly happening.
NARRATION_NOTES: a concise first-person-friendly description suitable for a caption.
Do not identify people by name, infer private traits, mention usernames/files, or invent topics.
""".strip()
        try:
            response=ollama.chat(
                model=vision_model,
                messages=[{
                    "role":"user",
                    "content":prompt,
                    "images":[str(p) for p in visual_files],
                }],
                options={"temperature":0.12,"top_p":0.8},
            )
            raw=str(
                response.get("message",{}).get("content","")
                if isinstance(response,dict)
                else getattr(getattr(response,"message",None),"content","")
            ).strip()
            m=re.search(
                r"PERSPECTIVE:\s*(POV_FIRST_PERSON|SELFIE_VLOG|THIRD_PERSON|UNKNOWN)",
                raw,re.I
            )
            if m:
                perspective=m.group(1).upper()
            visual_description=re.sub(
                r"(?<!\w)@[A-Za-z0-9._]{2,}","",raw
            ).strip()[:3500]
        except Exception:
            visual_description=""

    pieces=[]
    if visual_description:
        pieces.append("VIDEO/IMAGE VISUAL ANALYSIS:\n"+visual_description)
    if clean_text:
        pieces.append("SUPPORTING TEXT CONTEXT:\n"+clean_text)
    if not pieces:
        pieces.append(
            "No reliable semantic description is available. Do not invent a topic, "
            "identity, profession, location, or event."
        )

    return {
        "context":"\n\n".join(pieces),
        "perspective":perspective,
        "vision_model":vision_model,
        "media_kind":media_kind,
        "vision_required_failed":False,
        "frames_analyzed":len(visual_files),
    }



def _looks_like_advertising(text):
    lower = str(text or "").lower()
    ad_phrases = (
        "promote your", "promotion", "paid promo", "sponsored post",
        "brand ambassador", "collab offer", "collaboration opportunity",
        "grow your account", "grow your page", "more followers",
        "buy followers", "increase followers", "social media marketing",
        "marketing agency", "investment opportunity", "business opportunity",
        "forex signals", "crypto signals", "guaranteed returns",
        "discount code", "use my code", "shop now", "buy now",
        "check out my page", "check my profile", "dm us for",
        "we can help you", "our service", "special offer",
    )
    linkish = bool(re.search(r"https?://|www\.|(?:wa\.me|t\.me)/", lower))
    sales_language = any(p in lower for p in ad_phrases)

    # Avoid treating an ordinary friendly message containing one URL as spam.
    return sales_language or (
        linkish and any(
            k in lower
            for k in ("offer", "service", "promo", "marketing", "followers", "invest")
        )
    )


def _dm_conversation_excerpt(messages, own_user_id, max_messages=10):
    usable = []
    try:
        ordered = sorted(
            list(messages or []),
            key=lambda m: getattr(m, "timestamp", datetime.min),
        )
    except Exception:
        ordered = list(reversed(list(messages or [])))

    for msg in ordered[-max_messages:]:
        body = str(getattr(msg, "text", "") or "").strip()
        if not body:
            continue

        sent_by_viewer = getattr(msg, "is_sent_by_viewer", None)
        if sent_by_viewer is True:
            role = "ACCOUNT"
        elif sent_by_viewer is False:
            role = "OTHER PERSON"
        elif str(getattr(msg, "user_id", "")) == str(own_user_id):
            role = "ACCOUNT"
        else:
            role = "OTHER PERSON"

        usable.append(f"{role}: {body[:500]}")

    return "\n".join(usable[-max_messages:])


def generate_rage_bait_caption(
    media_context,
    perspective="unknown",
    account_username=None,
):
    s=get_account_control_settings(account_username) if account_username else {}
    semantic_context=str(media_context or "").strip()
    perspective=str(perspective or "unknown").upper()
    char_limit=int(s.get("caption_char_limit",420))
    extra=str(s.get("caption_prompt","")).strip()
    persona=str(s.get("persona_prompt",RAGE_BAIT_PERSONA)).strip()

    if perspective in {"POV_FIRST_PERSON","SELFIE_VLOG"}:
        perspective_rule=(
            "This is POV/selfie/vlog-style footage. Narrate the experience naturally "
            "in FIRST PERSON as the fictional filmer/account voice: I, I'm, my, we, "
            "what I'm seeing, what I'm doing. Describe the sequence as an experience, "
            "not as an outside observer."
        )
    elif perspective=="THIRD_PERSON":
        perspective_rule=(
            "Use first-person account voice reacting to/presenting the scene, but do "
            "not falsely claim to literally be an identifiable real person shown."
        )
    else:
        perspective_rule=(
            "Use first-person account voice without inventing who depicted people are."
        )

    prompt=f"""
Write ONE Instagram caption for this exact media.

MEDIA ANALYSIS:
{semantic_context}

PERSPECTIVE:
{perspective}
{perspective_rule}

ACCOUNT-SPECIFIC CAPTION INSTRUCTIONS:
{extra or "(none)"}

Requirements:
- Narrate what happens in the supplied visual sequence, especially for video.
- Never mention source/original usernames, repost metadata, archive folders, or filenames.
- Never inject HFT/trading or another stock topic unless the actual media analysis supports it.
- Smart, natural, specific, and coherent.
- Caption text only; hashtags are appended separately.
- Keep the caption under {char_limit} characters.
- Complete sentences.
""".strip()

    result=_ollama_generate(
        prompt,min_words=12,max_words=110,attempts=4,system_prompt=persona
    )
    return _clip_chars(result,char_limit) if result else None




def generate_interactive_reply(
    context_type,
    incoming_text,
    username,
    conversation_context="",
    account_username=None,
):
    incoming_text=str(incoming_text or "").strip()
    s=get_account_control_settings(account_username) if account_username else {}
    char_limit=int(s.get("reply_char_limit",280))
    persona=str(s.get("persona_prompt",RAGE_BAIT_PERSONA)).strip()
    extra = (
        str(s.get("dm_prompt",""))
        if context_type=="dm"
        else str(s.get("comment_prompt",""))
    )

    if context_type=="dm" and _looks_like_advertising(incoming_text):
        options=[
            "Not interested. Take the sales pitch somewhere else.",
            "No thanks. This inbox is for conversations, not cold sales scripts.",
            "I'm not interested in whatever you're selling. Try someone else.",
        ]
        return _clip_chars(random.choice(options),char_limit)

    prompt=f"""
Write ONE {context_type} reply to @{username}.

MOST RECENT MESSAGE:
{incoming_text!r}

RECENT CONVERSATION:
{conversation_context or "(none)"}

ACCOUNT-SPECIFIC REPLY INSTRUCTIONS:
{extra or "(none)"}

Rules:
- Respond to what they actually said and stay on that subject.
- Match their energy and effort.
- Do not redirect unrelated conversation into finance/HFT or another pet topic.
- If it is clearly advertising/solicitation, be dismissive and say you are not interested.
- Do not invent private facts.
- Keep it under {char_limit} characters.
- Complete sentence(s).
Output only the reply.
""".strip()

    result=_ollama_generate(
        prompt,min_words=3,max_words=100,attempts=4,system_prompt=persona
    )
    return _clip_chars(result,char_limit) if result else None




def _credential(conf, key):
    """Resolve credentials without forcing one configuration style.

    Priority:
      1. configured environment variable (recommended)
      2. direct/original-style FAN_ROSTER values for backwards compatibility

    Supported direct keys:
      password -> conf["password"]
      totp     -> conf["totp_secret"] or conf["totp"]
    """
    env_name = conf.get(f"{key}_env")
    if env_name:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value

    direct_keys = {
        "password": ("password",),
        "totp": ("totp_secret", "totp"),
    }.get(key, (key,))
    for direct_key in direct_keys:
        value = conf.get(direct_key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _refresh_instagram_app_profile(cl, username: str = ""):
    """
    Re-apply the app-version profile bundled with the installed instagrapi.

    Important: load_settings() can restore stale app_version/version_code/
    bloks_versioning_id values from an older session JSON. Calling set_app()
    afterwards keeps the saved device/session identity while updating the
    advertised Instagram app build to the library's current supported default.
    """
    label = f"@{username}" if username else "Instagram client"
    try:
        cl.set_app()
        app_version = str(
            getattr(cl, "app_version", "")
            or (getattr(cl, "device_settings", {}) or {}).get("app_version", "")
            or "current library default"
        )
        print(f"📱 {label}: Instagram app profile refreshed -> {app_version}")
        return cl
    except AttributeError:
        # Very old instagrapi installations did not expose set_app().
        raise RuntimeError(
            "Installed instagrapi is too old for current Instagram login. "
            "Update it with: python -m pip install -U instagrapi==2.18.19"
        )
    except Exception as exc:
        raise RuntimeError(
            f"Could not refresh Instagram app profile: "
            f"{type(exc).__name__}: {str(exc)[:180]}"
        ) from exc


def _is_needs_upgrade_error(exc) -> bool:
    message = str(exc or "").lower()
    return any(
        token in message
        for token in (
            "needs_upgrade",
            "app out of date",
            "version of instagram is out of date",
            "update instagram to the latest version",
            "unsupported_version",
            "web/unsupported_version",
        )
    )


def _needs_upgrade_message(username: str = "") -> str:
    label = f" for @{username}" if username else ""
    return (
        f"Instagram is rejecting the private-API username/password login{label} "
        "with error_type=needs_upgrade even though current instagrapi 2.18.19 "
        "is installed. This is an Instagram/private-API compatibility issue, "
        "not proof that your local package is outdated. Use Browser Login for "
        "this account instead of repeatedly retrying Password Login."
    )

def _manual_browser_login(username, conf, session_p):
    """Capture a browser login and try once to convert it to an instagrapi session.

    A browser cookie is not guaranteed to be accepted by Instagram's private/mobile
    API. If conversion is throttled or rejected, do not hammer the endpoint; record
    the result and let the scheduler move immediately to the next account.
    """
    AUTH_EVENT[username] = "manual_failed"
    if sync_playwright is None:
        update_account_metric(username, "add_history", value="Playwright is not installed; cannot run manual login.")
        return None

    update_account_metric(username, "add_history", value="Opening browser for manual Instagram login...")
    with sync_playwright() as p:
        profile_dir = DOWNLOAD_ROOT / f"browser_{username.replace('.', '_')}"
        context = p.chromium.launch_persistent_context(
            str(profile_dir), headless=False, viewport={"width": 1280, "height": 900}
        )
        page = context.pages[0] if context.pages else context.new_page()

        # A persistent browser profile may already be logged in. Start at Instagram
        # instead of forcing /accounts/login/ every time.
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        input(f"Log into @{username} in the Chromium window, confirm the home/profile page is loaded, then press Enter here: ")
        cookies = context.cookies("https://www.instagram.com/")
        context.close()

    sessionid = next((c.get("value") for c in cookies if c.get("name") == "sessionid"), None)
    if not sessionid:
        update_account_metric(username, "add_history", value="Manual login finished but Instagram did not provide a sessionid cookie.")
        update_account_metric(username, "status", status="Login Needed")
        return None

    # Keep a local record that the browser login itself succeeded. This does not
    # expose the cookie in console/dashboard output.
    browser_cookie_file = DOWNLOAD_ROOT / f"browser_session_{username.replace('.', '_')}.json"
    try:
        browser_cookie_file.write_text(json.dumps({"sessionid": sessionid}), encoding="utf-8")
    except Exception:
        pass

    cl = Client()
    _refresh_instagram_app_profile(cl, username)
    try:
        cl.login_by_sessionid(sessionid)
        cl.dump_settings(session_p)
        info = cl.user_info_v1(cl.user_id)
        update_account_metric(username, "sync_followers", value=info.follower_count)
        update_account_metric(username, "add_history", value="Manual browser session converted and saved successfully.")
        AUTH_EVENT[username] = "manual_saved"
        return cl
    except Exception as exc:
        message = str(exc)
        if "429" in message or "too many requests" in message.lower() or "please wait" in message.lower():
            AUTH_EVENT[username] = "throttled"
            set_auth_cooldown(username, minutes=60)
            update_account_metric(
                username, "add_history",
                value="Browser login succeeded, but Instagram throttled private-API session conversion. Pausing this account for 60 minutes.",
            )
        else:
            update_account_metric(username, "add_history", value=f"Browser login succeeded, but private-API session conversion failed: {message[:80]}")
            update_account_metric(username, "status", status="Login Needed")
        return None



def verify_authenticated_identity(cl, expected_username, session_p=None):
    """
    Refuse to use a saved/private-API session unless it is actually authenticated
    as the roster account we intend to operate.

    This prevents a copied/reused session JSON from silently running the wrong
    Instagram account.
    """
    expected = str(expected_username or "").strip().lstrip("@")
    try:
        if not getattr(cl, "user_id", None):
            cl.get_timeline_feed()

        info = cl.user_info_v1(cl.user_id)
        actual = str(getattr(info, "username", "") or "").strip().lstrip("@")
    except Exception as exc:
        update_account_metric(
            expected,
            "add_history",
            value=f"⚠️ Could not verify authenticated account identity: {type(exc).__name__}: {str(exc)[:80]}"
        )
        return False

    if actual.casefold() != expected.casefold():
        session_name = Path(session_p).name if session_p else "unknown session"
        message = (
            f"SESSION IDENTITY MISMATCH: roster expects @{expected}, "
            f"but {session_name} is logged in as @{actual}."
        )
        print(f"❌ {message}")
        update_account_metric(expected, "status", status="Session Mismatch")
        update_account_metric(expected, "add_history", value=f"❌ {message}")
        AUTH_EVENT[expected] = "identity_mismatch"
        CLIENT_CACHE.pop(expected, None)

        # Do not keep hammering a known-wrong saved session.
        until = datetime.now() + timedelta(minutes=30)
        AUTH_RETRY_AFTER[expected] = until
        ACCOUNT_COOLDOWNS[expected] = until
        update_account_metric(
            expected,
            "cooldown_until",
            value=until.strftime("%H:%M"),
        )
        return False

    print(f"✅ Session identity verified: @{expected}")
    update_account_metric(
        expected,
        "add_history",
        value=f"✅ Session identity verified as @{actual}."
    )
    return True


def get_authenticated_client(username, conf):
    """
    Return one authenticated client, but only after verifying that the Instagram
    session really belongs to `username`.

    v7.6 boot-safety rule:
      a disconnected account NEVER authenticates implicitly.
      Only a Control Head login action removes it from the disconnected set.
    """
    if username in CONTROL_DISCONNECTED_ACCOUNTS:
        AUTH_EVENT[username] = "disconnected"
        return None

    session_p = DOWNLOAD_ROOT / conf["session_file"]
    AUTH_EVENT[username] = ""

    cached = CLIENT_CACHE.get(username)
    if cached is not None:
        if verify_authenticated_identity(cached, username, session_p):
            AUTH_EVENT[username] = "cached_client"
            return cached
        CLIENT_CACHE.pop(username, None)
        return None

    retry_after = AUTH_RETRY_AFTER.get(username)
    if retry_after and datetime.now() < retry_after:
        AUTH_EVENT[username] = AUTH_EVENT.get(username) or "auth_backoff"
        return None
    if retry_after:
        AUTH_RETRY_AFTER.pop(username, None)

    if MANUAL_LOGIN_MODE:
        cl = _manual_browser_login(username, conf, session_p)
        if cl is not None:
            if not verify_authenticated_identity(cl, username, session_p):
                return None
            CLIENT_CACHE[username] = cl
        return cl

    password = _credential(conf, "password")
    totp_secret = _credential(conf, "totp")

    # If credentials exist, explicitly login as the roster username.
    if password:
        cl = Client()
        try:
            if session_p.exists():
                cl.load_settings(session_p)

            # load_settings() may have restored an obsolete Instagram build.
            _refresh_instagram_app_profile(cl, username)

            verification_code = (
                generate_2fa(totp_secret) if totp_secret else ""
            )
            cl.login(
                username,
                password,
                verification_code=verification_code,
            )

            if not verify_authenticated_identity(cl, username, session_p):
                return None

            cl.dump_settings(session_p)

            try:
                info = cl.user_info_v1(cl.user_id)
                update_account_metric(
                    username, "sync_followers",
                    value=info.follower_count
                )
            except Exception:
                pass

            update_account_metric(
                username,
                "add_history",
                value="Authenticated with credentials; identity verified and client cached."
            )
            AUTH_EVENT[username] = "credential_login"
            CLIENT_CACHE[username] = cl
            return cl

        except Exception as exc:
            message = str(exc)
            lower = message.lower()

            if _is_needs_upgrade_error(exc):
                AUTH_EVENT[username] = "needs_upgrade"
                set_auth_cooldown(username, minutes=360)
                update_account_metric(
                    username,
                    "status",
                    status="Private API Login Blocked",
                )
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "📱 " + _needs_upgrade_message(username)
                        + " Automatic credential retries paused for 6 hours."
                    ),
                )
                print(
                    "📱 "
                    + _needs_upgrade_message(username)
                    + " Automatic credential retries paused for 6 hours."
                )
                return None

            if (
                "429" in lower
                or "too many requests" in lower
                or "please wait" in lower
            ):
                AUTH_EVENT[username] = "throttled"
                set_auth_cooldown(username, minutes=60)
                update_account_metric(
                    username,
                    "add_history",
                    value=f"Instagram throttled authentication: {message[:80]}"
                )
                return None

            set_auth_backoff(
                username,
                minutes=30,
                reason=f"Credential/session login failed: {message[:80]}",
            )
            return None

    # Saved-session-only path: this is where a mismatched session used to be
    # silently accepted and could operate the wrong account.
    if session_p.exists():
        cl = Client()
        try:
            cl.load_settings(session_p)
            cl.get_timeline_feed()

            if not verify_authenticated_identity(cl, username, session_p):
                return None

            try:
                info = cl.user_info_v1(cl.user_id)
                update_account_metric(
                    username, "sync_followers",
                    value=info.follower_count
                )
            except Exception:
                pass

            AUTH_EVENT[username] = "saved_session"
            CLIENT_CACHE[username] = cl
            update_account_metric(
                username,
                "add_history",
                value="Reused saved Instagram session; identity verified and client cached."
            )
            return cl

        except Exception as exc:
            message = str(exc)
            lower = message.lower()

            if (
                "429" in lower
                or "too many requests" in lower
                or "please wait" in lower
            ):
                AUTH_EVENT[username] = "throttled"
                set_auth_cooldown(username, minutes=60)
                return None

            set_auth_backoff(
                username,
                minutes=30,
                reason=f"Saved session is unusable: {message[:80]}",
            )
            return None

    AUTH_EVENT[username] = "login_needed"
    set_auth_backoff(
        username,
        minutes=30,
        reason=(
            f"No usable saved session or password is available for @{username}. "
            "Run once with --manual-login --account="
            f"{username} if manual login is needed"
        ),
    )
    AUTH_EVENT[username] = "login_needed"
    update_account_metric(username, "status", status="Login Needed")
    return None


def discover_local_media_folders(force: bool = False):
    media_root = get_media_root()
    try:
        resolved = media_root.resolve()
    except Exception:
        resolved = media_root

    if not resolved.exists() or not resolved.is_dir():
        print(f"⚠️ Media folder does not exist: {resolved}", flush=True)
        MEDIA_FOLDER_CACHE.update({"root":str(resolved),"mtime_ns":-1,"cached_at":time.monotonic(),"folders":[]})
        return []

    try:
        mtime_ns = resolved.stat().st_mtime_ns
    except OSError:
        mtime_ns = -1
    now=time.monotonic()
    if (
        not force
        and MEDIA_FOLDER_CACHE.get("root") == str(resolved)
        and MEDIA_FOLDER_CACHE.get("mtime_ns") == mtime_ns
        and now - float(MEDIA_FOLDER_CACHE.get("cached_at",0.0) or 0.0) <= MEDIA_FOLDER_CACHE_SECONDS
    ):
        return list(MEDIA_FOLDER_CACHE.get("folders") or [])

    folders=[]
    try:
        with os.scandir(resolved) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                item=Path(entry.path)
                parts=item.name.split("_",2)
                source=parts[1] if len(parts)>1 and parts[1] else item.name
                folders.append({"id":item.name,"path":item,"source":source})
    except OSError as exc:
        print(f"⚠️ Media folder scan failed for {resolved}: {exc}", flush=True)
    MEDIA_FOLDER_CACHE.update({"root":str(resolved),"mtime_ns":mtime_ns,"cached_at":now,"folders":list(folders)})
    return folders






def _shared_pacing_path():
    return DOWNLOAD_ROOT / "shared_write_pacing.json"


def _shared_pacing_lock_path():
    return DOWNLOAD_ROOT / "shared_write_pacing.lock"


def _acquire_shared_pacing_lock(timeout=8.0):
    """
    Tiny cross-process lock using exclusive file creation.
    Works across separate Python instances without third-party packages.
    """
    lock_path=_shared_pacing_lock_path()
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            fd=os.open(str(lock_path),os.O_CREAT|os.O_EXCL|os.O_WRONLY)
            os.write(fd,f"{os.getpid()} {time.time()}".encode("ascii","ignore"))
            return fd
        except FileExistsError:
            try:
                age=time.time()-lock_path.stat().st_mtime
                if age>30:
                    lock_path.unlink(missing_ok=True)
                    continue
            except Exception:
                pass
            time.sleep(0.15)
    return None


def _release_shared_pacing_lock(fd):
    if fd is not None:
        try: os.close(fd)
        except Exception: pass
    try: _shared_pacing_lock_path().unlink(missing_ok=True)
    except Exception: pass


def _load_shared_pacing():
    try:
        p=_shared_pacing_path()
        if p.exists():
            data=json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data,dict):
                data.setdefault("global_last",0.0)
                data.setdefault("accounts",{})
                return data
    except Exception:
        pass
    return {"global_last":0.0,"accounts":{}}


def _save_shared_pacing(data):
    p=_shared_pacing_path()
    tmp=p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data,indent=2),encoding="utf-8")
    tmp.replace(p)


def _reserve_shared_write_slot(username, action_type):
    """
    Reserve a short cross-process write slot.

    Uploads receive a short in-progress lease. The long upload cooldown is
    committed only after Instagram returns a real media id.
    """
    allowed, reason = account_writes_allowed(username)
    if not allowed:
        return False, reason

    fd = _acquire_shared_pacing_lock()
    if fd is None:
        return False, "shared pacing lock busy"

    try:
        data = _load_shared_pacing()
        now = time.time()
        settings = get_account_control_settings(username)
        window = int(settings["write_window_seconds"])
        max_window = int(settings["max_writes_per_window"])
        account_gap = int(settings["write_min_gap_seconds"])
        upload_gap = int(settings["upload_cooldown_seconds"])

        accounts = data.setdefault("accounts", {})
        row = accounts.setdefault(username, {
            "writes": [],
            "last_write": 0.0,
            "last_upload": 0.0,
            "pending_upload_until": 0.0,
        })
        row.setdefault("pending_upload_until", 0.0)

        cutoff = now - window
        row["writes"] = [
            float(t) for t in row.get("writes", [])
            if isinstance(t, (int, float)) and float(t) >= cutoff
        ]

        if len(row["writes"]) >= max_window:
            wait = int(max(1, float(row["writes"][0]) + window - now))
            return False, f"shared rolling budget; retry in ~{wait}s"

        last_write = float(row.get("last_write") or 0.0)
        if now - last_write < account_gap:
            wait = int(max(1, account_gap - (now - last_write)))
            return False, f"shared account gap; retry in ~{wait}s"

        global_last = float(data.get("global_last") or 0.0)
        if now - global_last < GLOBAL_WRITE_MIN_GAP_SECONDS:
            wait = int(max(1, GLOBAL_WRITE_MIN_GAP_SECONDS - (now - global_last)))
            return False, f"shared multi-process gap; retry in ~{wait}s"

        if action_type == "upload":
            pending_until = float(row.get("pending_upload_until") or 0.0)
            if pending_until > now:
                wait = int(max(1, pending_until - now))
                return False, f"upload attempt already in progress; retry in ~{wait}s"

            last_upload = float(row.get("last_upload") or 0.0)
            if now - last_upload < upload_gap:
                wait = int(max(1, upload_gap - (now - last_upload)))
                return False, f"shared upload cooldown; retry in ~{wait}s"

        row["writes"].append(now)
        row["last_write"] = now
        data["global_last"] = now

        if action_type == "upload":
            # Four-minute lease. Failure clears this instead of consuming the
            # account's full upload cooldown.
            row["pending_upload_until"] = now + 240.0

        _save_shared_pacing(data)
        return True, ""
    finally:
        _release_shared_pacing_lock(fd)



def _finish_shared_write_reservation(username, action_type, success):
    if action_type != "upload":
        return

    fd = _acquire_shared_pacing_lock()
    if fd is None:
        return

    try:
        data = _load_shared_pacing()
        row = data.setdefault("accounts", {}).setdefault(username, {})
        row["pending_upload_until"] = 0.0
        if success:
            row["last_upload"] = time.time()
        _save_shared_pacing(data)
    finally:
        _release_shared_pacing_lock(fd)

def _prune_write_window(username):
    now=time.monotonic()
    pacing=_effective_pacing(username)
    cutoff=now-pacing["window"]
    times=ACCOUNT_WRITE_TIMES.setdefault(username,[])
    times[:]=[t for t in times if t>=cutoff]
    return times



def can_write_now(username, action_type="write"):
    now=time.monotonic()
    pacing=_effective_pacing(username)
    times=_prune_write_window(username)

    if len(times)>=pacing["max_window"]:
        wait=int(max(1,(times[0]+pacing["window"])-now))
        return False,f"rolling write budget reached; retry in ~{wait}s"

    last_account=ACCOUNT_LAST_WRITE.get(username,0.0)
    if now-last_account<pacing["gap"]:
        wait=int(max(1,pacing["gap"]-(now-last_account)))
        return False,f"account write gap; retry in ~{wait}s"

    global GLOBAL_LAST_WRITE
    if now-GLOBAL_LAST_WRITE<GLOBAL_WRITE_MIN_GAP_SECONDS:
        wait=int(max(1,GLOBAL_WRITE_MIN_GAP_SECONDS-(now-GLOBAL_LAST_WRITE)))
        return False,f"global multi-bot write gap; retry in ~{wait}s"

    if action_type=="upload":
        last_upload=ACCOUNT_LAST_UPLOAD.get(username,0.0)
        if now-last_upload<pacing["upload"]:
            wait=int(max(1,pacing["upload"]-(now-last_upload)))
            return False,f"upload cooldown; retry in ~{wait}s"

    if action_type=="dm":
        last_dm=ACCOUNT_LAST_DM_REPLY.get(username,0.0)
        if now-last_dm<DM_REPLY_COOLDOWN_SECONDS:
            wait=int(max(1,DM_REPLY_COOLDOWN_SECONDS-(now-last_dm)))
            return False,f"DM reply cooldown; retry in ~{wait}s"

    return True,""



def _shared_write_block_reason(username: str, action_type: str = "write") -> str:
    allowed, reason = account_writes_allowed(username)
    if not allowed:
        return reason

    fd = _acquire_shared_pacing_lock()
    if fd is None:
        return "shared pacing lock busy"

    try:
        data = _load_shared_pacing()
        now = time.time()
        settings = get_account_control_settings(username)
        window = int(settings["write_window_seconds"])
        max_window = int(settings["max_writes_per_window"])
        account_gap = int(settings["write_min_gap_seconds"])
        upload_gap = int(settings["upload_cooldown_seconds"])

        row = data.setdefault("accounts", {}).setdefault(
            username,
            {
                "writes": [],
                "last_write": 0.0,
                "last_upload": 0.0,
                "pending_upload_until": 0.0,
            },
        )

        cutoff = now - window
        writes = [
            float(t)
            for t in row.get("writes", [])
            if isinstance(t, (int, float)) and float(t) >= cutoff
        ]

        if len(writes) >= max_window:
            wait = int(max(1, writes[0] + window - now))
            return f"shared rolling budget; retry in ~{wait}s"

        last_write = float(row.get("last_write") or 0.0)
        if now - last_write < account_gap:
            wait = int(max(1, account_gap - (now - last_write)))
            return f"shared account gap; retry in ~{wait}s"

        global_last = float(data.get("global_last") or 0.0)
        if now - global_last < GLOBAL_WRITE_MIN_GAP_SECONDS:
            wait = int(max(1, GLOBAL_WRITE_MIN_GAP_SECONDS - (now - global_last)))
            return f"shared multi-process gap; retry in ~{wait}s"

        if action_type == "upload":
            pending_until = float(row.get("pending_upload_until") or 0.0)
            if pending_until > now:
                wait = int(max(1, pending_until - now))
                return f"upload attempt already in progress; retry in ~{wait}s"

            last_upload = float(row.get("last_upload") or 0.0)
            if now - last_upload < upload_gap:
                wait = int(max(1, upload_gap - (now - last_upload)))
                return f"shared upload cooldown; retry in ~{wait}s"

        return ""
    finally:
        _release_shared_pacing_lock(fd)


def _control_clear_upload_cooldown(username: str):
    username = str(username or "").strip().lstrip("@")
    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")

    fd = _acquire_shared_pacing_lock()
    if fd is None:
        raise RuntimeError("Shared pacing file is busy; try again.")

    try:
        data = _load_shared_pacing()
        row = data.setdefault("accounts", {}).setdefault(username, {})
        previous = float(row.get("last_upload") or 0.0)
        row["last_upload"] = 0.0
        row["pending_upload_until"] = 0.0
        _save_shared_pacing(data)
    finally:
        _release_shared_pacing_lock(fd)

    ACCOUNT_LAST_UPLOAD.pop(username, None)

    update_account_metric(
        username,
        "add_history",
        value=(
            "🧹 Upload cooldown cleared manually. Safety backoffs, generic "
            "write history, follows, likes, and login state were left intact."
        ),
    )

    return {"ok": True, "username": username, "previous_last_upload": previous}


def _quick_reopen_live_browser(username: str) -> tuple[bool, str]:
    if sync_playwright is None:
        return False, "Playwright is unavailable"

    try:
        with sync_playwright() as p:
            browser, context = _browser_launch_live_process(
                p,
                username,
                force_fresh_visible=False,
            )

            page = context.pages[0] if context.pages else context.new_page()

            try:
                page.bring_to_front()
            except Exception:
                pass

            try:
                if "instagram.com" not in str(page.url or "").lower():
                    page.goto(
                        "https://www.instagram.com/",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    page.wait_for_timeout(700)
            except Exception:
                pass

            try:
                _browser_safe_recover_page(
                    page,
                    context,
                    username,
                    reason="Quick Login browser recovery",
                    max_steps=3,
                )
            except Exception:
                pass

            deadline = time.time() + 15
            while time.time() < deadline:
                if _browser_page_authenticated(page, username):
                    try:
                        _browser_refresh_saved_sessionid(context, username)
                    except Exception:
                        pass
                    LIVE_CDP_CONTEXT_IDS.discard(id(context))
                    BROWSER_HANDLE_BY_CONTEXT.pop(id(context), None)
                    return True, "authenticated live Chromium reopened"

                problem = _browser_page_problem(page)
                if problem:
                    LIVE_CDP_CONTEXT_IDS.discard(id(context))
                    BROWSER_HANDLE_BY_CONTEXT.pop(id(context), None)
                    return False, f"manual attention required: {problem}"

                page.wait_for_timeout(500)

            LIVE_CDP_CONTEXT_IDS.discard(id(context))
            BROWSER_HANDLE_BY_CONTEXT.pop(id(context), None)
            return False, "Instagram did not reach the authenticated shell within 15s"

    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:180]}"

def wait_for_write_slot(
    username,
    action_type="write",
    max_wait=180,
    *,
    fail_fast=False,
):
    start = time.monotonic()
    last_reason = ""

    while True:
        allowed, reason = account_writes_allowed(username)
        if not allowed:
            update_account_metric(
                username,
                "add_history",
                value=f"🛡️ {action_type} blocked: {reason}",
            )
            return False

        ok, reason = _reserve_shared_write_slot(username, action_type)
        if ok:
            return True

        last_reason = reason

        if fail_fast:
            update_account_metric(
                username,
                "add_history",
                value=f"⏳ Deferred {action_type}: {last_reason or 'shared pacing budget'}",
            )
            return False

        if "Safety Backoff" in reason or "Account cooldown" in reason:
            break

        if time.monotonic() - start >= max_wait:
            break

        time.sleep(2.0)

    update_account_metric(
        username,
        "add_history",
        value=f"⏳ Deferred {action_type}: {last_reason or 'shared pacing budget'}",
    )
    return False





def record_write(username, action_type="write"):
    global GLOBAL_LAST_WRITE
    now = time.monotonic()

    with WRITE_LOCK:
        ACCOUNT_WRITE_TIMES.setdefault(username, []).append(now)
        ACCOUNT_LAST_WRITE[username] = now
        GLOBAL_LAST_WRITE = now
        if action_type == "upload":
            ACCOUNT_LAST_UPLOAD[username] = now
        elif action_type == "dm":
            ACCOUNT_LAST_DM_REPLY[username] = now

    _finish_shared_write_reservation(username, action_type, True)




def _normalize_reply_for_similarity(text):
    value = str(text or "").lower()
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"@\w[\w.]*", "", value)
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _reply_too_similar(candidate, previous_replies, threshold=0.76):
    norm = _normalize_reply_for_similarity(candidate)
    if not norm:
        return True

    for previous in previous_replies or []:
        prev = _normalize_reply_for_similarity(previous)
        if not prev:
            continue

        if norm == prev:
            return True

        ratio = difflib.SequenceMatcher(None, norm, prev).ratio()
        if ratio >= threshold:
            return True

        # Also catch repeated openings that feel robotic even if the rest differs.
        cand_words = norm.split()
        prev_words = prev.split()
        if len(cand_words) >= 6 and len(prev_words) >= 6:
            if cand_words[:5] == prev_words[:5]:
                return True

    return False


def generate_nonrepeating_dm_reply(
    incoming_text,
    sender,
    conversation_context,
    previous_replies,
    account_username=None,
):
    """
    Generate a DM reply that follows context and avoids repeating recent phrasing.
    """
    previous_replies = list(previous_replies or [])[-12:]
    avoid_block = "\n".join(
        f"- {reply}" for reply in previous_replies[-8:]
    ) or "(none)"

    for attempt in range(1, 5):
        diversity_note = f"""
RECENT REPLIES FROM THIS ACCOUNT TO THIS THREAD:
{avoid_block}

NON-REPETITION RULE:
Do not reuse the same opening, joke, punchline, sentence pattern, or conclusion
from those replies. Sound spontaneous. Change vocabulary and rhythm substantially.
Attempt {attempt}/4.
""".strip()

        reply = generate_interactive_reply(
            "dm",
            incoming_text,
            sender,
            conversation_context=(
                f"{conversation_context}\n\n{diversity_note}"
            ),
            account_username=account_username,
        )

        if not reply:
            continue

        if not _reply_too_similar(reply, previous_replies):
            return reply

    return None


def harvest_and_amplify_networks(cl, history, config, username, actions_performed):
    s = get_account_control_settings(username, config)
    if not s.get("enable_follow", False):
        return actions_performed

    targets = s["target_accounts"] or list(config.get("competitor_accounts") or [])
    if not targets:
        update_account_metric(username, "add_history", value="⚠️ Follow is enabled but no target accounts are configured.")
        return actions_performed

    follow_limit = min(int(s["follow_limit"]), int(s["max_writes_per_workflow"]))
    active_target = random.choice(targets)
    update_account_metric(username, "add_history", value=f"🎯 Follow target: @{active_target}")

    try:
        target_id = cl.user_id_from_username(active_target)
        source = s["follow_source"]
        sweep_type = random.choice(["followers", "following"]) if source == "both" else source
        max_id_key = f"{active_target}:{sweep_type}_next_max_id"
        current_max_id = history.get(max_id_key) or ""

        if sweep_type == "followers":
            users, next_max_id = cl.user_followers_v1_chunk(
                target_id, max_amount=max(10, follow_limit * 4), max_id=current_max_id
            )
        else:
            users, next_max_id = cl.user_following_v1_chunk(
                target_id, max_amount=max(10, follow_limit * 4), max_id=current_max_id
            )
        history[max_id_key] = next_max_id

        followed_this_pass = 0
        for u in users:
            # Re-read settings while running so an unchecked Follow box takes effect immediately.
            if not get_account_control_settings(username, config).get("enable_follow", False):
                break
            if followed_this_pass >= follow_limit:
                break

            u_pk, u_name = int(u.pk), u.username
            if u_pk in history["followed_users"] or u_pk in history["blocked_or_missing"]:
                continue
            try:
                friendship = cl.user_friendship_v1(u_pk)
                if not friendship.following and not friendship.outgoing_request:
                    if not wait_for_write_slot(username, "follow"):
                        break
                    if cl.user_follow(u_pk):
                        record_write(username, "follow")
                        history["followed_users"].append(u_pk)
                        followed_this_pass += 1
                        actions_performed += 1
                        update_account_metric(username, "total_follows", increment=1)
                        update_account_metric(
                            username, "add_history",
                            value=f"👤 Followed @{u_name} from @{active_target}'s {sweep_type}."
                        )
            except (UserNotFound, PrivateError):
                history["blocked_or_missing"].append(u_pk)
            except FeedbackRequired as exc:
                apply_account_safety_backoff(
                    username,
                    f"Instagram FeedbackRequired while following: {exc}",
                    level="restricted",
                )
                raise
            except Exception as exc:
                observe_platform_signal(username, exc, action="follow")
                update_account_metric(
                    username, "add_history",
                    value=f"⚠️ Follow skipped for @{u_name}: {str(exc)[:80]}"
                )
    except FeedbackRequired:
        raise
    except Exception as exc:
        observe_platform_signal(username, exc, action="target follow")
        update_account_metric(
            username, "add_history",
            value=f"⚠️ Target follow error on @{active_target}: {str(exc)[:100]}"
        )
    return actions_performed




def verify_like_state(cl, media, username, attempts=3):
    """
    True = authenticated media state confirms the viewer liked the media.
    False = media state explicitly says not liked.
    None = this response shape exposes no viewer-like boolean.
    """
    media_pk = str(getattr(media, "pk", "") or "")
    if not media_pk:
        try:
            media_pk = str(cl.media_pk(media.id))
        except Exception:
            return None

    saw_boolean = False
    for attempt in range(max(1, attempts)):
        try:
            info = cl.media_info_v1(media_pk)
            values = [
                getattr(info, "has_liked", None),
                getattr(info, "has_viewer_liked", None),
                getattr(info, "viewer_has_liked", None),
            ]
            booleans = [value for value in values if isinstance(value, bool)]
            if booleans:
                saw_boolean = True
                if any(booleans):
                    return True
        except Exception as exc:
            observe_platform_signal(username, exc, action="like verification")

        if attempt + 1 < attempts:
            time.sleep(2 + attempt)

    return False if saw_boolean else None

def is_login_required_error(exc) -> bool:
    message = str(exc or "").lower()
    return isinstance(exc, LoginRequired) or any(
        token in message
        for token in (
            "login_required",
            "login required",
            "loginrequired",
        )
    )


def stop_account_for_private_login_required(
    username: str,
    action: str,
    exc=None,
    hours: int = 6,
) -> None:
    """
    A write/private endpoint returning LoginRequired after browser bootstrap is
    treated as an authentication compatibility failure, not a rate-limit retry.

    Stop Auto for this account and preserve the saved browser/session files so
    the user can retry later without repeatedly hammering Instagram.
    """
    username = str(username or "").strip().lstrip("@")
    reason = (
        f"Instagram returned LoginRequired during {action}. "
        "The browser login is valid, but this imported session is not currently "
        "accepted for that private/mobile API action."
    )
    if exc:
        reason += f" ({type(exc).__name__}: {str(exc)[:140]})"

    until = datetime.now() + timedelta(hours=max(1, int(hours)))
    AUTH_RETRY_AFTER[username] = until
    ACCOUNT_COOLDOWNS[username] = until
    CONTROL_PAUSED_ACCOUNTS.add(username)
    CONTROL_FORCE_RUN.discard(username)
    NEXT_TASK_OVERRIDE.pop(username, None)
    AUTH_EVENT[username] = "private_login_required"

    update_account_metric(
        username,
        "status",
        status="Browser OK / Private API Blocked",
    )
    update_account_metric(
        username,
        "cooldown_until",
        value=until.strftime("%Y-%m-%d %H:%M"),
    )
    update_account_metric(
        username,
        "add_history",
        value=(
            f"🔐 {reason} Auto paused until "
            f"{until.strftime('%Y-%m-%d %H:%M')}."
        ),
    )
    print(f"🔐 @{username}: {reason}")


def hashtag_media_with_browser_session_fallback(
    cl,
    hashtag: str,
    amount: int,
    username: str,
):
    """
    Try authenticated private/mobile hashtag discovery first.

    Browser-imported sessions can identify the account successfully but still
    receive LoginRequired from the private hashtag endpoint. In that specific
    case, fall back to instagrapi's GraphQL hashtag-media reader. This changes
    discovery only; it does not bypass authentication for Likes/Follows/Posts.
    """
    try:
        medias = cl.hashtag_medias_recent(hashtag, amount=amount)
        return medias, "private"
    except Exception as exc:
        if not is_login_required_error(exc):
            raise

        update_account_metric(
            username,
            "add_history",
            value=(
                f"🌐 Private hashtag feed returned LoginRequired for #{hashtag}; "
                "trying GraphQL discovery with the valid browser session."
            ),
        )

        method = getattr(cl, "hashtag_medias_paginated_gql", None)
        if method is None:
            raise RuntimeError(
                "This instagrapi build does not expose "
                "hashtag_medias_paginated_gql for browser-session fallback."
            ) from exc

        result = method(hashtag, amount=amount)
        if isinstance(result, tuple):
            medias = result[0]
        else:
            medias = result

        return list(medias or []), "graphql"


def probe_browser_import_capabilities(cl, username: str, config=None) -> dict:
    """
    Non-destructive readiness probe after Browser Login.

    Identity/private profile reads are already checked separately. This probe
    tests hashtag discovery without performing any write action.
    """
    result = {
        "timeline": False,
        "hashtag_private": False,
        "hashtag_graphql": False,
    }

    try:
        cl.get_timeline_feed()
        result["timeline"] = True
    except Exception as exc:
        if is_login_required_error(exc):
            return result

    tag = "instagram"
    try:
        settings = get_account_control_settings(username, config or {})
        tags = _normalize_hashtag_list(settings.get("target_hashtags") or [])
        if tags:
            tag = tags[0]
    except Exception:
        pass

    try:
        cl.hashtag_medias_recent(tag, amount=1)
        result["hashtag_private"] = True
        return result
    except Exception as exc:
        if not is_login_required_error(exc):
            return result

    try:
        method = getattr(cl, "hashtag_medias_paginated_gql", None)
        if method is not None:
            result_value = method(tag, amount=1)
            medias = result_value[0] if isinstance(result_value, tuple) else result_value
            result["hashtag_graphql"] = medias is not None
    except Exception:
        pass

    return result

def interact_with_hashtags(cl, history, config, username, actions_performed):
    settings = get_account_control_settings(username, config)
    if not settings.get("enable_engage", False):
        return actions_performed

    allowed, _ = account_writes_allowed(username)
    if not allowed:
        return actions_performed

    tags = _normalize_hashtag_list(
        settings["target_hashtags"] or list(config.get("target_hashtags") or [])
    )
    if not tags:
        update_account_metric(
            username,
            "add_history",
            value="⚠️ No valid target hashtags configured.",
        )
        return actions_performed

    hashtag = re.sub(
        r"[^A-Za-z0-9_]",
        "",
        random.choice(tags).lstrip("#"),
    )
    if not hashtag:
        return actions_performed

    cap = int(settings["max_writes_per_workflow"])
    history.setdefault("like_verify_failures", 0)
    update_account_metric(
        username,
        "add_history",
        value=f"🔍 Streaming target #{hashtag}...",
    )

    try:
        medias, discovery_mode = hashtag_media_with_browser_session_fallback(
            cl,
            hashtag,
            amount=8,
            username=username,
        )

        if discovery_mode == "graphql":
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"🌐 #{hashtag} discovery is using GraphQL because this "
                    "browser-imported session is not accepted by the private "
                    "hashtag feed."
                ),
            )

        if not medias:
            update_account_metric(
                username,
                "add_history",
                value=f"⚠️ No media returned for #{hashtag}.",
            )
            return actions_performed

        for media in medias:
            if not get_account_control_settings(
                username,
                config,
            ).get("enable_engage", False):
                break

            allowed, _ = account_writes_allowed(username)
            if not allowed or actions_performed >= cap:
                break

            if media.id in history["liked_medias"]:
                continue

            try:
                if not wait_for_write_slot(username, "like"):
                    break

                accepted = cl.media_like(media.id)
                if not accepted:
                    history["like_verify_failures"] += 1
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            f"⚠️ Instagram did not accept the like request "
                            f"for @{media.user.username}."
                        ),
                    )
                    continue

                record_write(username, "like")
                history["liked_medias"].append(media.id)
                verified = verify_like_state(
                    cl,
                    media,
                    username,
                )

                if verified is True:
                    history["like_verify_failures"] = 0
                    update_account_metric(
                        username,
                        "total_likes",
                        increment=1,
                    )
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            f"❤️ Like confirmed on "
                            f"@{media.user.username}'s post."
                        ),
                    )
                elif verified is False:
                    history["like_verify_failures"] += 1
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            "⚠️ Like endpoint returned success, but authenticated "
                            f"media state did not show the like on "
                            f"@{media.user.username}'s post."
                        ),
                    )
                else:
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            f"♡ Like request accepted for "
                            f"@{media.user.username}; Instagram's returned media "
                            "object did not expose a viewer-like flag."
                        ),
                    )

                actions_performed += 1

                if history["like_verify_failures"] >= 3:
                    apply_account_safety_backoff(
                        username,
                        "Three recent like actions could not be confirmed "
                        "in authenticated media state.",
                        level="verification",
                        hours=4,
                    )
                    break

            except LoginRequired as exc:
                stop_account_for_private_login_required(
                    username,
                    "Like",
                    exc,
                    hours=6,
                )
                break
            except FeedbackRequired as exc:
                apply_account_safety_backoff(
                    username,
                    f"Instagram FeedbackRequired while liking: {exc}",
                    level="restricted",
                )
                raise
            except Exception as exc:
                if is_login_required_error(exc):
                    stop_account_for_private_login_required(
                        username,
                        "Like",
                        exc,
                        hours=6,
                    )
                    break

                observe_platform_signal(
                    username,
                    exc,
                    action="like",
                )
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        f"⚠️ Like skipped: {type(exc).__name__}: "
                        f"{str(exc)[:90]}"
                    ),
                )

    except LoginRequired as exc:
        # This should normally be consumed by the GraphQL discovery fallback.
        stop_account_for_private_login_required(
            username,
            "hashtag discovery",
            exc,
            hours=6,
        )
    except FeedbackRequired:
        raise
    except Exception as exc:
        if is_login_required_error(exc):
            stop_account_for_private_login_required(
                username,
                "hashtag discovery",
                exc,
                hours=6,
            )
        else:
            observe_platform_signal(
                username,
                exc,
                action="hashtag feed",
            )
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"⚠️ Hashtag error: {type(exc).__name__}: "
                    f"{str(exc)[:100]}"
                ),
            )

    return actions_performed





def handle_direct_messages(cl, history, username, max_replies=3, debug=False):
    if not get_account_control_settings(username).get("enable_dms", False):
        return 0
    processed = 0
    settings = get_account_control_settings(username)
    per_pass_limit = max(1, int(settings.get("max_dm_replies_per_pass", MAX_DM_REPLIES_PER_PASS)))
    history.setdefault("replied_dms", [])
    history.setdefault("dm_reply_memory", {})
    history.setdefault("dm_thread_last_reply_at", {})

    records = []
    seen_thread_ids = set()
    source_counts = {}

    def add_threads(source, threads):
        threads = list(threads or [])
        source_counts[source] = len(threads)
        for thread in threads:
            tid = str(getattr(thread, "id", "") or getattr(thread, "pk", ""))
            if not tid or tid in seen_thread_ids:
                continue
            seen_thread_ids.add(tid)
            records.append((source, thread))

    inbox_calls = [
        ("inbox", dict(amount=30, thread_message_limit=10)),
        ("unread", dict(amount=30, selected_filter="unread", thread_message_limit=10)),
        ("primary", dict(amount=30, box="primary", thread_message_limit=10)),
        ("general", dict(amount=30, box="general", thread_message_limit=10)),
    ]

    for source, kwargs in inbox_calls:
        try:
            add_threads(source, cl.direct_threads(**kwargs))
        except Exception as exc:
            source_counts[source] = -1
            if debug:
                print(f"⚠️ @{username} DM {source} fetch failed: {type(exc).__name__}: {exc}")

    try:
        add_threads("requests", cl.direct_requests(amount=30))
    except Exception as exc:
        source_counts["requests"] = -1
        if debug:
            print(f"⚠️ @{username} DM requests fetch failed: {type(exc).__name__}: {exc}")
        try:
            add_threads("pending", cl.direct_pending_inbox(amount=30))
        except Exception as pending_exc:
            source_counts["pending"] = -1
            if debug:
                print(
                    f"⚠️ @{username} pending inbox fetch failed: "
                    f"{type(pending_exc).__name__}: {pending_exc}"
                )

    summary = ", ".join(
        f"{name}={'ERR' if count < 0 else count}"
        for name, count in source_counts.items()
    )

    if debug:
        print(
            f"📥 @{username} DM scan: {summary}; "
            f"{len(records)} unique threads"
        )

    update_account_metric(
        username,
        "add_history",
        value=f"👂 DM scan: {summary}; {len(records)} unique threads"
    )

    for source, thread_stub in records:
        if not get_account_control_settings(username).get("enable_dms", False):
            break
        if processed >= min(max_replies, MAX_WRITES_PER_WORKFLOW, per_pass_limit):
            break
        if getattr(thread_stub, "is_group", False):
            continue

        thread_id = getattr(thread_stub, "id", None) or getattr(thread_stub, "pk", None)
        if not thread_id:
            continue

        try:
            thread = cl.direct_thread(int(thread_id), amount=25)
        except Exception as exc:
            thread = thread_stub
            if debug:
                print(
                    f"⚠️ @{username} could not refresh DM thread {thread_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        messages = list(getattr(thread, "messages", None) or [])
        try:
            messages.sort(
                key=lambda m: getattr(m, "timestamp", datetime.min),
                reverse=True,
            )
        except Exception:
            pass

        incoming = None
        for msg in messages:
            msg_id = getattr(msg, "id", None)
            if not msg_id or msg_id in history["replied_dms"]:
                continue

            sent_by_viewer = getattr(msg, "is_sent_by_viewer", None)
            if sent_by_viewer is True:
                continue
            if (
                sent_by_viewer is None
                and str(getattr(msg, "user_id", "")) == str(cl.user_id)
            ):
                continue

            incoming_text = str(getattr(msg, "text", "") or "").strip()
            if not incoming_text:
                continue

            msg_time = getattr(msg, "timestamp", None)
            if isinstance(msg_time, datetime):
                try:
                    now_for_msg = (
                        datetime.now(msg_time.tzinfo)
                        if msg_time.tzinfo
                        else datetime.now()
                    )
                    age_hours = (now_for_msg - msg_time).total_seconds() / 3600
                    if age_hours > DM_MAX_MESSAGE_AGE_HOURS:
                        continue
                    if age_hours * 3600 < DM_MIN_INCOMING_AGE_SECONDS:
                        continue
                except Exception:
                    pass

            incoming = msg
            break

        if incoming is None:
            continue

        sender = "user"
        incoming_user_id = str(getattr(incoming, "user_id", "") or "")
        for u in getattr(thread, "users", []) or []:
            u_pk = str(getattr(u, "pk", "") or "")
            if incoming_user_id and u_pk == incoming_user_id:
                sender = getattr(u, "username", "user") or "user"
                break
            if sender == "user" and u_pk != str(cl.user_id):
                sender = getattr(u, "username", "user") or "user"

        incoming_text = str(incoming.text).strip()
        print(f"💬 @{username} DM[{source}] from @{sender}: {incoming_text[:160]}")

        if source in {"requests", "pending"} and DM_AUTO_APPROVE_REQUESTS:
            if not wait_for_write_slot(username, "dm", max_wait=120):
                continue
            try:
                approved = cl.direct_request_approve(int(thread_id))
                record_write(username, "dm")
                print(f"✅ @{username} approved request from @{sender}: {approved}")
            except Exception as exc:
                print(
                    f"⚠️ @{username} request approval failed for @{sender}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

        conversation_context = _dm_conversation_excerpt(
            messages,
            cl.user_id,
            max_messages=10,
        )

        thread_key = str(thread_id)
        previous_replies = history["dm_reply_memory"].get(thread_key, [])
        last_thread_reply_at = float(history.get("dm_thread_last_reply_at", {}).get(thread_key, 0.0) or 0.0)
        if last_thread_reply_at and time.time() - last_thread_reply_at < DM_THREAD_REPLY_COOLDOWN_SECONDS:
            continue

        reply = generate_nonrepeating_dm_reply(
            incoming_text,
            sender,
            conversation_context,
            previous_replies,
            account_username=username,
        )

        if not reply:
            print(
                f"⚠️ @{username} could not generate a sufficiently distinct "
                f"complete DM reply for @{sender}."
            )
            continue

        if not wait_for_write_slot(username, "dm", max_wait=180):
            continue

        sent = None
        send_error = None
        time.sleep(random.uniform(ACTION_HUMAN_DELAY_MIN_SECONDS, ACTION_HUMAN_DELAY_MAX_SECONDS))
        try:
            sent = cl.direct_answer(int(thread_id), reply)
        except Exception as exc:
            send_error = exc
            try:
                sent = cl.direct_send(reply, thread_ids=[int(thread_id)])
                send_error = None
            except Exception as fallback_exc:
                send_error = fallback_exc

        if send_error is not None or not sent:
            print(
                f"❌ @{username} DM send failed for @{sender}: "
                f"{type(send_error).__name__ if send_error else 'Unknown'}: {send_error}"
            )
            continue

        record_write(username, "dm")

        history["replied_dms"].append(incoming.id)
        history["replied_dms"] = history["replied_dms"][-5000:]

        previous_replies.append(reply)
        history["dm_reply_memory"][thread_key] = previous_replies[-20:]
        history["dm_thread_last_reply_at"][thread_key] = time.time()

        print(f"📤 @{username} replied to @{sender}: {reply}")
        update_account_metric(
            username,
            "add_history",
            value=f"📤 Replied to @{sender}: {reply[:130]}"
        )
        processed += 1

    return processed






def handle_post_comments(cl, history, username):
    if not get_account_control_settings(username).get("enable_comments", False):
        return 0
    processed = 0
    settings = get_account_control_settings(username)
    per_pass_limit = max(1, int(settings.get("max_comment_replies_per_pass", MAX_COMMENT_REPLIES_PER_PASS)))
    try:
        my_medias = cl.user_medias(cl.user_id, amount=5)
        for media in my_medias:
            comments = cl.media_comments(media.id, amount=15)
            for comment in comments:
                if not get_account_control_settings(username).get("enable_comments", False):
                    return processed
                if processed >= min(3, per_pass_limit, MAX_WRITES_PER_WORKFLOW):
                    return processed
                if (
                    str(comment.user.pk) == str(cl.user_id)
                    or comment.id in history["replied_comments"]
                ):
                    continue

                update_account_metric(
                    username,
                    "add_history",
                    value=f"💭 Comment from @{comment.user.username}: {comment.text[:90]}"
                )
                reply = generate_interactive_reply(
                    "comment", comment.text, comment.user.username,
                    account_username=username
                )
                if not reply:
                    update_account_metric(
                        username,
                        "add_history",
                        value=f"⚠️ Ollama rejected an incomplete comment reply to @{comment.user.username}; skipped."
                    )
                    continue

                if processed >= min(MAX_WRITES_PER_WORKFLOW, per_pass_limit):
                    return processed
                if not wait_for_write_slot(username, "comment"):
                    return processed
                time.sleep(random.uniform(max(2, ACTION_HUMAN_DELAY_MIN_SECONDS // 2), max(4, ACTION_HUMAN_DELAY_MAX_SECONDS // 2)))
                cl.media_comment(
                    media.id,
                    reply,
                    replied_to_comment_id=comment.id,
                )
                record_write(username, "comment")
                history["replied_comments"].append(comment.id)
                history["replied_comments"] = history["replied_comments"][-5000:]
                update_account_metric(
                    username,
                    "add_history",
                    value=f"📤 Replied to @{comment.user.username}: {reply[:120]}"
                )
                processed += 1
    except FeedbackRequired:
        raise
    except Exception as exc:
        update_account_metric(
            username,
            "add_history",
            value=f"⚠️ Comment pass error: {str(exc)[:90]}"
        )
    return processed



def generate_video_thumbnail(video_path: Path) -> Path:
    """Generate a JPEG thumbnail explicitly so instagrapi never needs MoviePy for it.

    ffmpeg is preferred. MoviePy 2.x is only a fallback. The generated thumbnail
    is cached under the bot data directory and reused on later attempts.
    """
    video_path = Path(video_path)
    thumb_root = DOWNLOAD_ROOT / "video_thumbnails"
    thumb_root.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in video_path.stem)[:80]
    thumb_path = thumb_root / f"{safe_name}_{video_path.stat().st_size}_{video_path.stat().st_mtime_ns}.jpg"
    if thumb_path.exists() and thumb_path.stat().st_size > 1024:
        return thumb_path

    ffmpeg_candidates = []
    env_ffmpeg = os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    if env_ffmpeg:
        ffmpeg_candidates.append(env_ffmpeg)
    path_ffmpeg = shutil.which("ffmpeg")
    if path_ffmpeg and path_ffmpeg not in ffmpeg_candidates:
        ffmpeg_candidates.append(path_ffmpeg)
    # imageio-ffmpeg often ships its own executable even when ffmpeg is not on PATH.
    try:
        import imageio_ffmpeg
        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled_ffmpeg and bundled_ffmpeg not in ffmpeg_candidates:
            ffmpeg_candidates.append(bundled_ffmpeg)
    except Exception:
        pass

    errors = []
    for ffmpeg in ffmpeg_candidates:
        for seek in ("1.0", "0.1", "0"):
            try:
                cmd = [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", seek, "-i", str(video_path),
                    "-frames:v", "1",
                    "-vf", "scale=1080:-2:force_original_aspect_ratio=decrease",
                    "-q:v", "2", str(thumb_path),
                ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=45)
                if thumb_path.exists() and thumb_path.stat().st_size > 1024:
                    return thumb_path
            except Exception as exc:
                errors.append(f"ffmpeg({seek}s): {exc}")

    # MoviePy 2.2.1 fallback. This is optional; ffmpeg-generated JPEG is preferred.
    try:
        from moviepy import VideoFileClip
        clip = VideoFileClip(str(video_path))
        try:
            duration = float(clip.duration or 0)
            frame_at = min(1.0, max(0.0, duration / 3.0)) if duration else 0.0
            clip.save_frame(str(thumb_path), t=frame_at)
        finally:
            clip.close()
        if thumb_path.exists() and thumb_path.stat().st_size > 1024:
            return thumb_path
    except Exception as exc:
        errors.append(f"MoviePy: {exc}")

    raise RuntimeError(
        "Could not create an upload thumbnail. Install ffmpeg and put it on PATH "
        "(recommended), or install MoviePy 2.2.1. " + (" | ".join(errors[-2:]) if errors else "")
    )


def _upload_reel_with_profile_preview(cl, target, full_caption, thumbnail):
    # Force the profile/feed preview flag on. Fall back for older signatures.
    try:
        return cl.clip_upload(
            target,
            caption=full_caption,
            thumbnail=thumbnail,
            feed_show="1",
            show_preview_in_feed=True,
        )
    except TypeError as exc:
        if "show_preview_in_feed" not in str(exc):
            raise
        return cl.clip_upload(
            target,
            caption=full_caption,
            thumbnail=thumbnail,
            feed_show="1",
        )


def verify_media_visibility(cl, uploaded_media, username, kind):
    """
    Verify configured media using Instagram's authenticated private/mobile API only.

    Public GraphQL/media_info_gql is deliberately NOT used because Instagram often
    returns the normal HTML website with HTTP 200 instead of JSON, which causes
    noisy JSONDecodeError retry loops even when the upload itself succeeded.
    """
    media_id = str(getattr(uploaded_media, "id", "") or "")
    media_pk = str(getattr(uploaded_media, "pk", "") or "")

    if not media_pk and media_id:
        try:
            media_pk = str(cl.media_pk(media_id))
        except Exception:
            media_pk = media_id.split("_", 1)[0]

    code = str(getattr(uploaded_media, "code", "") or "")
    if not code and media_pk:
        try:
            code = cl.media_code_from_pk(media_pk)
        except Exception:
            code = ""

    permalink = ""
    if code:
        surface = "reel" if kind == "reel" else "p"
        permalink = f"https://www.instagram.com/{surface}/{code}/"
        print(f"🔗 @{username}: {permalink}")
        update_account_metric(
            username,
            "add_history",
            value=f"🔗 New media permalink: {permalink}"
        )

    private_visible = False
    profile_visible = False
    private_error = ""
    profile_error = ""

    for attempt in range(1, VISIBILITY_VERIFY_ATTEMPTS + 1):
        if media_pk and not private_visible:
            try:
                info = cl.media_info_v1(media_pk)
                private_visible = bool(info)
                private_error = ""
            except Exception as exc:
                private_error = f"{type(exc).__name__}: {str(exc)[:100]}"

        if media_pk and not profile_visible:
            try:
                if kind == "reel":
                    recent = cl.user_clips_v1(cl.user_id, amount=50)
                else:
                    recent = cl.user_medias_v1(cl.user_id, amount=50)

                profile_visible = any(
                    str(getattr(item, "pk", "")) == media_pk
                    for item in (recent or [])
                )
                if profile_visible:
                    profile_error = ""
            except Exception as exc:
                profile_error = f"{type(exc).__name__}: {str(exc)[:100]}"

        if private_visible and profile_visible:
            break

        if attempt < VISIBILITY_VERIFY_ATTEMPTS:
            print(
                f"⏳ @{username}: media configured but not fully surfaced yet; "
                f"private={private_visible}, profile={profile_visible}. "
                f"Retry {attempt}/{VISIBILITY_VERIFY_ATTEMPTS}..."
            )
            time.sleep(VISIBILITY_VERIFY_DELAY)

    if private_visible:
        print(
            f"✅ @{username}: authenticated media lookup confirms "
            f"{media_id or media_pk}"
        )
        update_account_metric(
            username,
            "add_history",
            value=f"✅ Authenticated media lookup confirms {media_id or media_pk}"
        )
    else:
        print(
            f"⚠️ @{username}: authenticated media lookup did not confirm media: "
            f"{private_error or 'not returned'}"
        )
        update_account_metric(
            username,
            "add_history",
            value=(
                f"⚠️ Authenticated media lookup failed: "
                f"{private_error or 'not returned'}"
            )[:180]
        )

    if profile_visible:
        collection = "Reels" if kind == "reel" else "profile"
        print(f"✅ @{username}: media appears in uploader {collection} collection.")
        update_account_metric(
            username,
            "add_history",
            value=f"✅ Media appears in uploader {collection} collection."
        )
    else:
        collection = "Reels" if kind == "reel" else "profile"
        print(
            f"⚠️ @{username}: media is NOT present in uploader {collection} "
            f"collection after verification. {profile_error}"
        )
        update_account_metric(
            username,
            "add_history",
            value=(
                f"⚠️ Media NOT present in uploader {collection} collection. "
                f"{profile_error}"
            )[:180]
        )

    # A single authenticated session cannot prove what a different account sees.
    # Keep this explicit instead of generating false negatives from public GraphQL.
    if permalink:
        print(
            f"👁️ External visibility check: open this URL while logged into the "
            f"other Instagram account: {permalink}"
        )
        update_account_metric(
            username,
            "add_history",
            value="👁️ External-account visibility must be checked from the printed permalink."
        )

    return {
        "media_id": media_id,
        "media_pk": media_pk,
        "code": code,
        "permalink": permalink,
        "private_visible": private_visible,
        "profile_visible": profile_visible,
        # Retained for compatibility with execute_repost_flow, but no unreliable
        # public GraphQL request is attempted.
        "public_visible": False,
        "external_visibility": "unverified",
    }



def reconcile_pending_uploads(cl, history, username):
    pending = list(history.get("pending_uploads") or [])
    if not pending:
        return

    keep = []
    now = time.time()

    for item in pending[:20]:
        try:
            media_pk = str(item.get("media_pk") or "")
            kind = str(item.get("kind") or "post")
            folder_id = str(item.get("folder_id") or "")
            created_ts = float(item.get("created_ts") or now)

            if not media_pk:
                continue

            recent = (
                cl.user_clips_v1(cl.user_id, amount=50)
                if kind == "reel"
                else cl.user_medias_v1(cl.user_id, amount=50)
            )
            visible = any(
                str(getattr(media, "pk", "")) == media_pk
                for media in (recent or [])
            )

            if visible:
                if folder_id and folder_id not in history["posted_ids"]:
                    history["posted_ids"].append(folder_id)
                    update_account_metric(username, "total_posts", increment=1)
                update_account_metric(
                    username, "add_history",
                    value=f"✅ Previously pending upload {media_pk} is now visible on the account."
                )
                continue

            if now - created_ts < 24 * 3600:
                keep.append(item)
            else:
                update_account_metric(
                    username, "add_history",
                    value=f"⚠️ Pending upload {media_pk} never surfaced after 24h; its source folder is available again."
                )
        except Exception as exc:
            observe_platform_signal(username, exc, action="pending upload verification")
            keep.append(item)

    history["pending_uploads"] = keep

def execute_repost_flow(cl, history, username, folder_pool):
    settings = get_account_control_settings(username)
    if not settings.get("enable_posts", False):
        return False

    allowed, reason = account_writes_allowed(username)
    if not allowed:
        update_account_metric(username, "add_history", value=f"🛡️ Upload skipped: {reason}")
        return False

    history.setdefault("posted_ids", [])
    history.setdefault("pending_uploads", [])
    history.setdefault("upload_verify_failures", 0)
    reconcile_pending_uploads(cl, history, username)

    pending_folder_ids = {
        str(item.get("folder_id") or "")
        for item in history["pending_uploads"]
        if isinstance(item, dict)
    }

    update_account_metric(
        username, "add_history",
        value="Scanning media pool for an unused folder..."
    )
    available_pool = [
        folder for folder in folder_pool
        if folder["id"] not in history["posted_ids"]
        and folder["id"] not in pending_folder_ids
    ]
    if not available_pool:
        update_account_metric(
            username, "add_history",
            value="No unused media folders are currently available."
        )
        return False

    random.shuffle(available_pool)
    selected_folder = None
    media_files = []
    valid_exts = (".jpg", ".jpeg", ".png", ".mp4")

    for candidate in available_pool:
        candidate_files = sorted(
            item for item in candidate["path"].iterdir()
            if item.is_file() and item.suffix.lower() in valid_exts
        )
        if candidate_files:
            selected_folder = candidate
            media_files = candidate_files
            break

    if not selected_folder:
        return False

    text_context = ""
    for txt in sorted(
        item for item in selected_folder["path"].iterdir()
        if item.is_file() and item.suffix.lower() in {".txt", ".caption", ".md"}
    )[:3]:
        try:
            body = txt.read_text(encoding="utf-8", errors="replace").strip()
            if body:
                text_context += (("\n" if text_context else "") + body[:3000])
        except OSError:
            pass

    settings = get_account_control_settings(username)
    post_analysis = analyze_media_for_caption(
        media_files,
        sidecar_text=text_context,
        source_account=selected_folder.get("source", ""),
        frame_count=settings["video_frames"],
        require_video_vision=settings["require_video_vision"],
    )

    if post_analysis.get("vision_required_failed"):
        update_account_metric(
            username, "add_history",
            value=(
                f"👁️ Video skipped: Require video vision is ON, but only "
                f"{post_analysis.get('frames_analyzed', 0)} frames could be analyzed "
                f"with {post_analysis.get('vision_model') or 'no vision model'}."
            )
        )
        return False

    semantic_context = post_analysis["context"]
    perspective = post_analysis["perspective"]
    vision_model = post_analysis["vision_model"]

    update_account_metric(
        username, "add_history",
        value=f"🤖 Preparing media from folder: {selected_folder['id']}"
    )
    update_account_metric(
        username, "add_history",
        value=(
            f"👁️ Media watched with {vision_model}; frames={post_analysis.get('frames_analyzed', 0)}, "
            f"perspective={perspective}."
            if vision_model else
            "👁️ No vision model detected; using sanitized text context only."
        )
    )

    caption = generate_rage_bait_caption(
        semantic_context,
        perspective=perspective,
        account_username=username,
    )
    if not caption:
        update_account_metric(
            username, "add_history",
            value="⚠️ Caption failed validation; upload skipped."
        )
        return False

    hashtag_prompt = f"""
Generate EXACTLY 5 relevant Instagram hashtags based ONLY on the media analysis below.

{semantic_context}

Rules:
- exactly 5
- searchable and directly relevant
- no source usernames
- no trading/HFT unless the media itself is about trading
- output only 5 space-separated hashtags
""".strip()

    try:
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": settings["persona_prompt"]},
                {"role": "user", "content": hashtag_prompt},
            ],
            options={"temperature": 0.45, "top_p": 0.9},
        )
        generated_tags = _clean_ollama_output(
            response.get("message", {}).get("content", "")
        )
    except Exception:
        generated_tags = ""

    tags = []
    for token in generated_tags.replace("\n", " ").split():
        token = token.strip(" ,.;:!?")
        if token and not token.startswith("#"):
            token = "#" + token
        if re.fullmatch(r"#[A-Za-z0-9_]+", token) and token not in tags:
            tags.append(token)
        if len(tags) == 5:
            break

    for fallback in ["#video", "#reels", "#creator", "#explore", "#daily"]:
        if len(tags) >= 5:
            break
        if fallback not in tags:
            tags.append(fallback)

    tag_line = " ".join(tags[:5])
    total_limit = int(settings["caption_char_limit"])
    caption = _clip_chars(caption, max(20, total_limit - len(tag_line) - 2))
    full_caption = f"{caption}\n\n{tag_line}".strip()

    video_files = [item for item in media_files if item.suffix.lower() == ".mp4"]
    photo_files = [
        item for item in media_files
        if item.suffix.lower() in {".jpg", ".jpeg", ".png"}
    ]

    upload_slot_reserved = False
    try:
        if not wait_for_write_slot(username, "upload", max_wait=180):
            update_account_metric(
                username, "add_history",
                value="⏳ Upload deferred by pacing/safety state."
            )
            return False
        upload_slot_reserved = True

        kind = "post"

        if len(media_files) == 1:
            target = media_files[0]
            if target.suffix.lower() == ".mp4":
                kind = "reel"
                thumbnail = generate_video_thumbnail(target)
                print(f"⬆️ @{username}: starting Reel upload: {target.name}")
                uploaded_media = _upload_reel_with_profile_preview(
                    cl, target, full_caption, thumbnail
                )
            else:
                print(f"⬆️ @{username}: starting photo upload: {target.name}")
                uploaded_media = cl.photo_upload(target, caption=full_caption)

        elif video_files:
            kind = "reel"
            target = video_files[0]
            thumbnail = generate_video_thumbnail(target)
            print(f"⬆️ @{username}: starting Reel upload: {target.name}")
            uploaded_media = _upload_reel_with_profile_preview(
                cl, target, full_caption, thumbnail
            )

        elif photo_files:
            print(
                f"⬆️ @{username}: starting carousel upload with {len(photo_files)} images"
            )
            uploaded_media = cl.album_upload(photo_files, caption=full_caption)

        else:
            _finish_shared_write_reservation(username, "upload", False)
            upload_slot_reserved = False
            return False

        media_id = str(getattr(uploaded_media, "id", "") or "")
        if not uploaded_media or not media_id:
            raise RuntimeError("Instagram returned no uploaded media id")

        # API accepted the media: now commit the long upload cooldown.
        record_write(username, "upload")
        upload_slot_reserved = False

        visibility = verify_media_visibility(cl, uploaded_media, username, kind)
        media_pk = str(visibility.get("media_pk") or "")

        if visibility["profile_visible"]:
            history["upload_verify_failures"] = 0
            if selected_folder["id"] not in history["posted_ids"]:
                history["posted_ids"].append(selected_folder["id"])
                update_account_metric(username, "total_posts", increment=1)

            record_recent_post(
                username=username,
                folder_path=selected_folder["path"],
                caption=full_caption,
                media_id=media_id,
                permalink=visibility.get("permalink", ""),
            )
            update_account_metric(
                username, "add_history",
                value="✅ Upload confirmed in the account's profile/Reels collection."
            )
            return True

        # Do not lie to the dashboard and count this as a visible post.
        history["upload_verify_failures"] += 1
        history["pending_uploads"].append({
            "folder_id": selected_folder["id"],
            "media_pk": media_pk,
            "media_id": media_id,
            "kind": kind,
            "created_ts": time.time(),
            "permalink": visibility.get("permalink", ""),
        })
        history["pending_uploads"] = history["pending_uploads"][-30:]

        update_account_metric(
            username, "add_history",
            value=(
                "⏳ Instagram returned a media id, but the media is not visible in "
                "the account collection yet. Marked pending and NOT counted as a post."
            )
        )

        if history["upload_verify_failures"] >= 2:
            apply_account_safety_backoff(
                username,
                "Two recent uploads returned media ids but did not surface in the account collection.",
                level="verification",
                hours=4,
            )

        return False

    except FeedbackRequired as exc:
        if upload_slot_reserved:
            _finish_shared_write_reservation(username, "upload", False)
        apply_account_safety_backoff(
            username,
            f"Instagram FeedbackRequired during upload: {exc}",
            level="restricted",
        )
        raise

    except Exception as exc:
        if upload_slot_reserved:
            _finish_shared_write_reservation(username, "upload", False)
        observe_platform_signal(username, exc, action="upload")
        update_account_metric(
            username, "add_history",
            value=f"⚠️ Upload failed: {type(exc).__name__}: {str(exc)[:140]}"
        )
        return False






def patch_obsolete_qe_expose(cl, username):
    """
    Instagram currently returns HTTP 404 for the legacy /qe/expose/ experiment
    endpoint on some accounts.

    instagrapi's Reel upload flow calls self.expose() *after* the Reel has already
    been uploaded and configured.  A 404 here can therefore make a successful
    Reel look like an upload failure.

    Only swallow the known qe/expose 404.  Every other ClientError is re-raised.
    """
    if getattr(cl, "_qe_expose_404_patch", False):
        return cl

    original_expose = getattr(cl, "expose", None)
    if not callable(original_expose):
        return cl

    def safe_expose(*args, **kwargs):
        try:
            return original_expose(*args, **kwargs)
        except ClientError as exc:
            message = str(exc)
            lower = message.lower()
            if (
                "qe/expose" in lower
                and (
                    "404" in lower
                    or "not found" in lower
                    or "does not exist" in lower
                )
            ):
                update_account_metric(
                    username,
                    "add_history",
                    value="ℹ️ Ignored obsolete Instagram /qe/expose/ 404 after media configure.",
                )
                print(
                    f"ℹ️ @{username}: ignoring obsolete /qe/expose/ 404 "
                    "(media upload/configure already completed)."
                )
                return {}
            raise

    cl.expose = safe_expose
    cl._qe_expose_404_patch = True
    return cl


def _browser_profile_dir(username: str) -> Path:
    return DOWNLOAD_ROOT / f"browser_{str(username).strip().lstrip('@').replace('.', '_')}"


def _browser_profile_saved(username: str) -> bool:
    if _browser_storage_state_available(username):
        return True
    live_path = _browser_live_profile_dir(username)
    if live_path.exists() and live_path.is_dir():
        return True
    legacy_path = _browser_profile_dir(username)
    return legacy_path.exists() and legacy_path.is_dir()






def _activate_browser_native_mode(username: str, reason: str = "") -> None:
    username = str(username or "").strip().lstrip("@")
    with CONTROL_LOCK:
        BROWSER_NATIVE_ACCOUNTS.add(username)
        CLIENT_CACHE.pop(username, None)
        CONTROL_DISCONNECTED_ACCOUNTS.discard(username)
        CONTROL_PAUSED_ACCOUNTS.add(username)
        CONTROL_FORCE_RUN.discard(username)
        AUTH_EVENT[username] = "browser_native"

        # Private-API auth cooldowns must not block Browser Mode. Browser Mode
        # uses Instagram Web and has its own challenge/restriction detection.
        ACCOUNT_COOLDOWNS.pop(username, None)

    update_account_metric(
        username,
        "status",
        status="Browser Mode / Auto Off",
    )
    if reason:
        update_account_metric(
            username,
            "add_history",
            value=f"🌐 Browser Mode enabled: {reason}",
        )


def _deactivate_browser_native_mode(username: str) -> None:
    BROWSER_NATIVE_ACCOUNTS.discard(
        str(username or "").strip().lstrip("@")
    )


def _browser_context_logged_in(context) -> bool:
    try:
        cookies = context.cookies("https://www.instagram.com/")
        return any(
            c.get("name") == "sessionid" and c.get("value")
            for c in cookies
        )
    except Exception:
        return False


def _browser_transient_load_error(page) -> str:
    """Detect Instagram Web's transient SPA error page."""
    phrases = (
        "something went wrong",
        "there's an issue and the page could not be loaded",
        "there is an issue and the page could not be loaded",
        "page could not be loaded",
    )

    try:
        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=1200) or "",
        ).lower()
    except Exception:
        return ""

    for phrase in phrases:
        if phrase in body:
            return phrase

    return ""

def _browser_page_problem(page) -> str:
    """
    Detect login/challenge/restriction UI. Never attempts to bypass it.
    """
    try:
        url = str(page.url or "").lower()
    except Exception:
        url = ""

    if any(token in url for token in ("/accounts/login", "/challenge/", "/checkpoint/")):
        return f"Instagram verification/login page: {url[:180]}"

    phrases = (
        "confirm it's you",
        "help us confirm you own this account",
        "suspicious login attempt",
        "challenge required",
        "account compromised",
        "your account has been compromised",
        "we noticed unusual activity",
        "unusual activity",
        "secure your account",
        "try again later",
        "we restrict certain activity",
        "your account has been temporarily",
        "please wait a few minutes",
    )
    try:
        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=900) or "",
        ).lower()
        for phrase in phrases:
            if phrase in body:
                return phrase
    except Exception:
        pass

    return ""


def _browser_saved_account_resume_visible(page, username: str) -> bool:
    """Detect Instagram's saved-account/profile chooser for this exact account."""
    username = str(username or "").strip().lstrip("@").lower()
    try:
        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=1200) or "",
        ).lower()
    except Exception:
        return False

    return (
        username in body
        and "continue" in body
        and (
            "use another account" in body
            or "use another profile" in body
        )
    )


def _browser_visible_controls(page, limit: int = 32) -> list[str]:
    """Collect visible control labels only; never collect form-field values."""
    try:
        items = page.locator(
            "button, a, [role='button'], [role='menuitem']"
        ).evaluate_all(
            """(els) => els
                .filter((e) => {
                    const r = e.getBoundingClientRect();
                    const s = window.getComputedStyle(e);
                    return r.width > 0 && r.height > 0 &&
                           s.visibility !== 'hidden' &&
                           s.display !== 'none';
                })
                .map((e) => (
                    e.getAttribute('aria-label') ||
                    e.innerText ||
                    e.textContent ||
                    ''
                ).trim())
                .filter(Boolean)"""
        )
    except Exception:
        return []

    out = []
    seen = set()
    for item in items:
        label = re.sub(r"\s+", " ", str(item or "")).strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(label[:120])
        if len(out) >= max(1, int(limit)):
            break
    return out


def _browser_safe_nav_snapshot(page, username: str) -> dict:
    try:
        url = str(page.url or "")
    except Exception:
        url = ""
    try:
        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=1200) or "",
        ).strip()
    except Exception:
        body = ""
    return {
        "username": str(username or "").strip().lstrip("@"),
        "url": url[:500],
        "page_text": body[:2200],
        "visible_controls": _browser_visible_controls(page),
    }


def _browser_ai_nav_decision(page, username: str, reason: str = "") -> str:
    """
    Ask Ollama to choose exactly one pre-authorized, ordinary navigation action.
    Ollama never supplies selectors, JavaScript, credentials, URLs, or arbitrary
    clicks.
    """
    if not BROWSER_AI_NAV_ENABLED:
        return "NONE"

    snapshot = _browser_safe_nav_snapshot(page, username)
    allowed = (
        "CONTINUE_SAVED_ACCOUNT",
        "DISMISS_NOT_NOW",
        "CLOSE_DIALOG",
        "GO_HOME",
        "RELOAD",
        "BACK",
        "WAIT",
        "MANUAL_REQUIRED",
        "NONE",
    )

    prompt = f"""
Classify this current Instagram Web page for a SAFE recovery helper.

Configured account: @{snapshot['username']}
Reason: {reason or '(none)'}
URL: {snapshot['url']}

Visible page text:
{snapshot['page_text']}

Visible controls:
{json.dumps(snapshot['visible_controls'], ensure_ascii=False)}

Return exactly ONE token:
{", ".join(allowed)}

Rules:
- CONTINUE_SAVED_ACCOUNT only when Continue is visibly offered for the exact
  configured saved profile/account.
- DISMISS_NOT_NOW only for optional non-security prompts.
- CLOSE_DIALOG only for an ordinary non-security modal.
- GO_HOME, RELOAD, BACK, WAIT only for ordinary navigation/loading recovery.
- MANUAL_REQUIRED for CAPTCHA, checkpoint/challenge, suspicious login,
  password/2FA, identity verification, disabled/suspended account, rate limit,
  restriction, or any security-sensitive page.
- Never suggest bypassing a challenge, restriction, or security control.
- NONE when no safe action is justified.

Return the token only.
""".strip()

    executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=f"ig-nav-{username}",
    )
    try:
        future = executor.submit(
            ollama.chat,
            model=BROWSER_AI_NAV_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Return one allowed action token only. "
                        "Be conservative around account security."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            options={
                "temperature": 0.0,
                "top_p": 0.1,
                "num_predict": 12,
            },
        )
        response = future.result(timeout=BROWSER_AI_NAV_TIMEOUT_SECONDS)
        raw = (
            response.get("message", {}).get("content", "")
            if isinstance(response, dict)
            else getattr(getattr(response, "message", None), "content", "")
        )
        upper = str(raw or "").upper()
        compact = re.sub(r"[^A-Z_]", "", upper.strip())
        if compact in allowed:
            return compact
        for candidate in allowed:
            if candidate in upper:
                return candidate
        return "NONE"
    except FutureTimeoutError:
        return "NONE"
    except Exception:
        return "NONE"
    finally:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def _browser_execute_safe_nav_action(page, context, username: str, action: str) -> bool:
    """Execute only the fixed safe-action allowlist."""
    action = str(action or "").strip().upper()

    if action == "CONTINUE_SAVED_ACCOUNT":
        return _browser_resume_saved_account(page, context, username)

    if action == "DISMISS_NOT_NOW":
        clicked = _browser_click_text_button(
            page,
            r"^(Not Now|Not now)$",
            timeout=4000,
        )
        if clicked:
            page.wait_for_timeout(700)
        return clicked

    if action == "CLOSE_DIALOG":
        clicked = _browser_click_text_button(
            page,
            r"^Close$",
            timeout=3500,
        )
        if not clicked:
            try:
                svg = page.locator("svg[aria-label='Close']").first
                if svg.count() and svg.is_visible(timeout=500):
                    _browser_clickable_from_svg(svg).click(timeout=3500)
                    clicked = True
            except Exception:
                pass
        if clicked:
            page.wait_for_timeout(600)
        return clicked

    if action == "GO_HOME":
        try:
            page.goto(
                "https://www.instagram.com/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(900)
            return True
        except Exception:
            return False

    if action == "RELOAD":
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(900)
            return True
        except Exception:
            return False

    if action == "BACK":
        try:
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(700)
            return True
        except Exception:
            return False

    if action == "WAIT":
        try:
            page.wait_for_timeout(1500)
            return True
        except Exception:
            return False

    return False


def _browser_safe_recover_page(
    page,
    context,
    username: str,
    reason: str = "",
    *,
    max_steps: int | None = None,
) -> bool:
    """
    Recover ordinary UI/navigation interruptions without bypassing security,
    challenges, CAPTCHAs, restrictions, disabled-account states, or rate limits.
    """
    username = str(username or "").strip().lstrip("@")
    max_steps = (
        BROWSER_AI_NAV_MAX_STEPS
        if max_steps is None
        else max(1, min(5, int(max_steps)))
    )

    for step in range(1, max_steps + 1):
        problem = _browser_page_problem(page)
        if problem:
            update_account_metric(
                username,
                "add_history",
                value=(
                    "🛑 Adaptive browser recovery stopped for manual attention: "
                    f"{problem}"
                ),
            )
            return False

        if _browser_page_authenticated(page, username):
            return True

        if _browser_saved_account_resume_visible(page, username):
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"👤 Adaptive recovery detected Instagram's saved-profile "
                    f"Continue screen for @{username}."
                ),
            )
            _browser_resume_saved_account(page, context, username)
            if _browser_page_authenticated(page, username):
                return True
            page.wait_for_timeout(700)
            continue

        transient = _browser_transient_load_error(page)
        if transient:
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"🔄 Adaptive recovery: transient Instagram error "
                    f"'{transient}', reloading."
                ),
            )
            _browser_execute_safe_nav_action(
                page, context, username, "RELOAD"
            )
            continue

        action = _browser_ai_nav_decision(
            page,
            username,
            reason=reason,
        )

        if action == "MANUAL_REQUIRED":
            update_account_metric(
                username,
                "add_history",
                value=(
                    "🛑 Ollama classified the page as security-sensitive/manual. "
                    "No automatic click was made."
                ),
            )
            return False

        if action in {"NONE", ""}:
            return False

        update_account_metric(
            username,
            "add_history",
            value=(
                f"🤖 Ollama browser recovery step {step}/{max_steps}: {action}"
            ),
        )

        if not _browser_execute_safe_nav_action(
            page,
            context,
            username,
            action,
        ):
            return False

        if _browser_page_authenticated(page, username):
            return True

    return _browser_page_authenticated(page, username)

def _browser_page_authenticated(page, username: str) -> bool:
    """
    Determine whether the CURRENT live Instagram page looks authenticated.

    This is intentionally separate from sessionid-cookie presence. Instagram
    can present a working logged-in SPA state or saved-account resume flow while
    cookie timing/state changes underneath Playwright.
    """
    username = str(username or "").strip().lstrip("@")

    try:
        url = str(page.url or "").lower()
    except Exception:
        url = ""

    if any(
        token in url
        for token in (
            "/accounts/login",
            "/challenge/",
            "/checkpoint/",
        )
    ):
        return False

    try:
        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=1200) or "",
        ).lower()
    except Exception:
        body = ""

    # Saved-account chooser is not authenticated yet. Instagram currently
    # uses both "Use another account" and "Use another profile".
    if _browser_saved_account_resume_visible(page, username):
        return False

    # Strong positive UI signals from the authenticated Instagram shell.
    selectors = (
        "svg[aria-label='Home']",
        "svg[aria-label='Search']",
        "svg[aria-label='Explore']",
        "svg[aria-label='Reels']",
        "svg[aria-label='Messages']",
        "svg[aria-label='Notifications']",
        "svg[aria-label='New post']",
        "svg[aria-label='Create']",
        f"a[href='/{username}/']",
        f"a[href='/{username}']",
    )

    visible_signals = 0
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible(timeout=180):
                visible_signals += 1
                if visible_signals >= 1:
                    return True
        except Exception:
            pass

    # Text fallback for layouts where navigation labels are rendered outside
    # accessible SVG labels.
    nav_terms = (
        " home ",
        " search ",
        " explore ",
        " reels ",
        " messages ",
        " notifications ",
        " create ",
    )
    padded = f" {body} "
    if sum(term in padded for term in nav_terms) >= 3:
        return True

    return False

def _browser_resume_saved_account(page, context, username: str) -> bool:
    """
    Handle Instagram's normal saved-account/profile chooser for the exact
    configured account. Supports both "Use another account" and
    "Use another profile".
    """
    username = str(username or "").strip().lstrip("@")
    if not _browser_saved_account_resume_visible(page, username):
        return False

    update_account_metric(
        username,
        "add_history",
        value=(
            f"👤 Instagram presented the saved-account/profile Continue "
            f"screen for @{username}; selecting Continue..."
        ),
    )

    clicked = False
    patterns = (
        rf"^Continue as\s+@?{re.escape(username)}$",
        rf"^Continue as\s+{re.escape(username)}$",
        r"^Continue$",
    )

    for pattern in patterns:
        try:
            candidate = page.get_by_role(
                "button",
                name=re.compile(pattern, re.I),
            ).first
            if candidate.count() and candidate.is_visible(timeout=700):
                candidate.click(timeout=5000)
                clicked = True
                break
        except Exception:
            pass

    if not clicked:
        for pattern in patterns:
            try:
                candidate = page.get_by_text(
                    re.compile(pattern, re.I),
                    exact=True,
                ).first
                if candidate.count() and candidate.is_visible(timeout=700):
                    candidate.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                pass

    if not clicked:
        update_account_metric(
            username,
            "add_history",
            value=(
                "⚠️ Saved-profile resume screen detected, but Continue "
                "could not be located."
            ),
        )
        return False

    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass

    deadline = time.time() + 12
    while time.time() < deadline:
        page.wait_for_timeout(500)

        if _browser_page_problem(page):
            return False

        if _browser_page_authenticated(page, username):
            _browser_refresh_saved_sessionid(context, username)
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"✅ Continue succeeded; live Chromium reached the "
                    f"authenticated Instagram shell for @{username}."
                ),
            )
            return True

        if not _browser_saved_account_resume_visible(page, username):
            # Page advanced. The caller/adaptive agent can classify the new state.
            return True

    update_account_metric(
        username,
        "add_history",
        value=(
            "⚠️ Continue was clicked, but Instagram remained on the "
            "saved-profile chooser for 12s."
        ),
    )
    return False


def _browser_check_ready(page, context, username: str) -> None:
    """
    Validate Browser Mode against the actual live Instagram page.
    Ordinary navigation hurdles use the safe adaptive recovery layer.
    """
    username = str(username or "").strip().lstrip("@")

    if _browser_page_authenticated(page, username):
        _browser_refresh_saved_sessionid(context, username)
        return

    recovered = _browser_safe_recover_page(
        page,
        context,
        username,
        reason="Browser Mode authentication/readiness check",
    )

    if recovered and _browser_page_authenticated(page, username):
        _browser_refresh_saved_sessionid(context, username)
        return

    problem = _browser_page_problem(page)
    if problem:
        CONTROL_PAUSED_ACCOUNTS.add(username)
        update_account_metric(
            username,
            "status",
            status="Browser Verification Needed",
        )
        raise RuntimeError(
            f"Instagram Web requires manual attention: {problem}. "
            "Complete the prompt manually in the live Chromium window."
        )

    try:
        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=1200) or "",
        ).strip()
    except Exception:
        body = ""

    update_account_metric(
        username,
        "add_history",
        value=(
            "⚠️ Adaptive browser recovery exhausted its safe actions. "
            f"Current URL={str(page.url or '')[:160]} | "
            f"page text={body[:220]}"
        ),
    )

    raise RuntimeError(
        "The live Instagram page is not authenticated/ready after safe "
        "adaptive recovery. Inspect the visible Chromium window."
    )







def _browser_session_cookie_file(username: str) -> Path:
    safe = str(username or "").strip().lstrip("@").replace(".", "_")
    return DOWNLOAD_ROOT / f"browser_session_{safe}.json"


def _load_saved_browser_sessionid(username: str) -> str:
    path = _browser_session_cookie_file(username)
    if not path.exists():
        return ""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("sessionid", "") or "").strip()
    except Exception:
        return ""


def _save_browser_sessionid(username: str, sessionid: str) -> None:
    sessionid = str(sessionid or "").strip()
    if not sessionid:
        return

    path = _browser_session_cookie_file(username)
    try:
        path.write_text(
            json.dumps({"sessionid": sessionid}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _restore_saved_browser_session(context, username: str) -> bool:
    """
    Restore the locally saved Instagram Web session cookie into the Playwright
    context if the persistent Chromium profile itself came up logged out.

    This uses only the session cookie captured from the user's own successful
    Browser Login. It does not bypass login challenges or verification.
    """
    if _browser_context_logged_in(context):
        return True

    sessionid = _load_saved_browser_sessionid(username)
    if not sessionid:
        return False

    cookie_variants = (
        {
            "name": "sessionid",
            "value": sessionid,
            "domain": ".instagram.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "sessionid",
            "value": sessionid,
            "url": "https://www.instagram.com/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
    )

    for cookie in cookie_variants:
        try:
            context.add_cookies([cookie])
            if _browser_context_logged_in(context):
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "🔐 Restored saved Instagram browser session into "
                        "Chromium because the persistent profile opened logged out."
                    ),
                )
                return True
        except Exception:
            continue

    return _browser_context_logged_in(context)


def _browser_refresh_saved_sessionid(context, username: str) -> None:
    """
    Persist rotated sessionid and refresh the complete Playwright storage state.
    """
    try:
        cookies = context.cookies("https://www.instagram.com/")
        sessionid = next(
            (
                c.get("value")
                for c in cookies
                if c.get("name") == "sessionid" and c.get("value")
            ),
            None,
        )
        if sessionid:
            _save_browser_sessionid(username, sessionid)
    except Exception:
        pass

    try:
        _save_full_browser_storage_state(context, username)
    except Exception:
        pass



def _browser_live_profile_dir(username: str) -> Path:
    safe = str(username or "").strip().lstrip("@").replace(".", "_")
    return DOWNLOAD_ROOT / f"browser_live_{safe}"

def _browser_live_port(username: str) -> int:
    username = str(username or "").strip().lstrip("@")
    try:
        index = list(CONTROL_ROSTER.keys()).index(username)
    except Exception:
        index = sum(ord(ch) for ch in username) % 20
    return 9460 + int(index)


def _browser_live_endpoint(username: str) -> str:
    return f"http://127.0.0.1:{_browser_live_port(username)}"


def _browser_connect_live(p, username: str):
    try:
        browser = p.chromium.connect_over_cdp(
            _browser_live_endpoint(username),
            timeout=2500,
        )
        contexts = browser.contexts
        if not contexts:
            return None, None
        context = contexts[0]
        LIVE_CDP_CONTEXT_IDS.add(id(context))
        BROWSER_HANDLE_BY_CONTEXT[id(context)] = browser
        return browser, context
    except Exception:
        return None, None


def _browser_close_stale_live_process(p, username: str) -> bool:
    """
    Browser Login must always give the user a visible Chromium window.

    If a live-CDP Chromium from an earlier bot run is still listening on this
    account's port, close that stale browser before launching a fresh visible
    one. Normal Browser Mode actions do NOT call this helper and continue to
    reuse the live session.
    """
    browser = None
    try:
        browser = p.chromium.connect_over_cdp(
            _browser_live_endpoint(username),
            timeout=2000,
        )
    except Exception:
        return False

    try:
        # Closing through the CDP browser connection terminates the stale
        # external Chromium instance that owns this dedicated account port.
        browser.close()
    except Exception:
        return False

    # Give Windows/Linux a moment to release the debugging port and profile.
    deadline = time.time() + 6
    while time.time() < deadline:
        time.sleep(0.25)
        probe = None
        try:
            probe = p.chromium.connect_over_cdp(
                _browser_live_endpoint(username),
                timeout=600,
            )
            if probe is not None:
                try:
                    probe.close()
                except Exception:
                    pass
        except Exception:
            return True

    return True

def _browser_launch_live_process(
    p,
    username: str,
    *,
    force_fresh_visible: bool = False,
):
    """
    Launch/reconnect the per-account live Chromium session.

    Important Windows behavior:
    Chromium can make the initially spawned process exit with code 0 after
    handing work to another browser process. Therefore an immediate code-0
    exit is NOT treated as failure; we continue probing the localhost CDP port.

    Browser Login uses a dedicated live profile directory so it cannot collide
    with the older persistent-profile directory used by legacy builds.
    """
    username = str(username or "").strip().lstrip("@")

    if force_fresh_visible:
        closed = _browser_close_stale_live_process(
            p,
            username,
        )
        if closed:
            update_account_metric(
                username,
                "add_history",
                value=(
                    "🧹 Closed stale live Chromium listener from a previous "
                    "bot run before launching Browser Login."
                ),
            )
    else:
        existing_browser, existing_context = _browser_connect_live(
            p,
            username,
        )
        if existing_context is not None:
            return existing_browser, existing_context

    executable = p.chromium.executable_path

    # Do NOT reuse the legacy browser_<username> profile here. A previous
    # Chromium process may still own its SingletonLock on Windows.
    profile_dir = _browser_live_profile_dir(username)
    profile_dir.mkdir(parents=True, exist_ok=True)

    port = _browser_live_port(username)

    cmd = [
        str(executable),
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        "--new-window",
        "https://www.instagram.com/",
    ]

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    LIVE_BROWSER_PROCESSES[username] = proc

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🌐 Launching fresh live Chromium with dedicated profile "
            f"{profile_dir} on localhost port {port}..."
        ),
    )

    deadline = time.time() + 20
    last_exc = None
    launcher_exit_code = None

    while time.time() < deadline:
        time.sleep(0.35)

        poll = proc.poll()
        if poll is not None and launcher_exit_code is None:
            launcher_exit_code = poll

            # Code 0 is common when Chromium hands off to another process.
            # Keep probing CDP rather than failing immediately.
            if poll == 0:
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "ℹ️ Chromium launcher process exited with code 0; "
                        "continuing to look for the live browser process..."
                    ),
                )
            else:
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        f"⚠️ Chromium launcher exited with code {poll}; "
                        "still probing the debugging port briefly."
                    ),
                )

        try:
            browser = p.chromium.connect_over_cdp(
                _browser_live_endpoint(username),
                timeout=1800,
            )
            contexts = browser.contexts

            if contexts:
                context = contexts[0]
                LIVE_CDP_CONTEXT_IDS.add(id(context))
                BROWSER_HANDLE_BY_CONTEXT[id(context)] = browser

                page = (
                    context.pages[0]
                    if context.pages
                    else context.new_page()
                )

                try:
                    page.bring_to_front()
                except Exception:
                    pass

                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "✅ Fresh visible Chromium is running and connected "
                        f"for Browser Login on localhost port {port}."
                    ),
                )
                return browser, context

        except Exception as exc:
            last_exc = exc

    # Only terminate the original launcher if it is still alive.
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass

    LIVE_BROWSER_PROCESSES.pop(username, None)

    raise RuntimeError(
        "Could not connect to the fresh Chromium Browser Login session. "
        f"Launcher exit code={launcher_exit_code!r}; localhost port={port}. "
        "Close any Chromium window whose profile belongs to this bot and retry. "
        f"Last connection error: "
        f"{type(last_exc).__name__ if last_exc else 'none'}: "
        f"{str(last_exc)[:160] if last_exc else ''}"
    )




def _browser_live_port_open(username: str, timeout: float = 0.18) -> bool:
    try:
        with socket.create_connection(
            ("127.0.0.1", _browser_live_port(username)),
            timeout=max(0.05, float(timeout)),
        ):
            return True
    except OSError:
        return False


def _mark_browser_login_needed(username: str, reason: str) -> None:
    username = str(username or "").strip().lstrip("@")
    with CONTROL_LOCK:
        BROWSER_NATIVE_ACCOUNTS.discard(username)
        CLIENT_CACHE.pop(username, None)
        CONTROL_DISCONNECTED_ACCOUNTS.add(username)
        CONTROL_PAUSED_ACCOUNTS.add(username)
        CONTROL_FORCE_RUN.discard(username)
        NEXT_TASK_OVERRIDE.pop(username, None)
        AUTH_EVENT[username] = "browser_login_needed"
    update_account_metric(username, "status", status="Browser Login Needed")
    update_account_metric(username, "add_history", value=f"🔌 Browser Login needed: {reason}")

def _browser_live_session_available(username: str) -> bool:
    return _browser_live_port_open(username)


def _browser_storage_state_file(username: str) -> Path:
    safe = str(username or "").strip().lstrip("@").replace(".", "_")
    return DOWNLOAD_ROOT / f"browser_storage_{safe}.json"


def _browser_storage_state_available(username: str) -> bool:
    path = _browser_storage_state_file(username)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and isinstance(data.get("cookies"), list)
    except Exception:
        return False


def _save_full_browser_storage_state(context, username: str) -> bool:
    path = _browser_storage_state_file(username)
    try:
        try:
            context.storage_state(
                path=str(path),
                indexed_db=True,
            )
        except TypeError:
            context.storage_state(
                path=str(path),
            )
        return path.exists()
    except Exception as exc:
        update_account_metric(
            username,
            "add_history",
            value=(
                "⚠️ Full browser storage-state save failed: "
                f"{type(exc).__name__}: {str(exc)[:160]}"
            ),
        )
        return False


def _browser_close_context(context) -> None:
    context_id = id(context)
    browser = BROWSER_HANDLE_BY_CONTEXT.pop(context_id, None)

    if context_id in LIVE_CDP_CONTEXT_IDS:
        LIVE_CDP_CONTEXT_IDS.discard(context_id)
        # External live Chromium must remain open. The surrounding Playwright
        # scope will simply detach this client connection.
        return

    try:
        context.close()
    except Exception:
        pass

    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass


def _browser_preflight_login(username: str, *, headed: bool = False) -> bool:
    """
    Fast Browser Mode auth preflight.

    When attached to the exact live Chromium session, inspect the current page
    first instead of reloading it and potentially forcing Instagram back into a
    saved-account resume state.
    """
    if sync_playwright is None:
        raise RuntimeError("Playwright is required for Browser Mode.")

    with sync_playwright() as p:
        context = _browser_launch(
            p,
            username,
            headed=headed,
        )
        page = context.pages[0] if context.pages else context.new_page()
        live_context = id(context) in LIVE_CDP_CONTEXT_IDS

        try:
            if "instagram.com" not in str(page.url or "").lower():
                page.goto(
                    "https://www.instagram.com/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(900)
            elif live_context:
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                page.wait_for_timeout(500)
            else:
                page.reload(
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(900)

            _browser_check_ready(
                page,
                context,
                username,
            )
            _browser_refresh_saved_sessionid(
                context,
                username,
            )

            update_account_metric(
                username,
                "add_history",
                value=(
                    "✅ Browser auth preflight passed against the current "
                    "live Instagram page."
                ),
            )
            return True

        finally:
            _browser_close_context(context)




def _browser_launch(p, username: str, *, headed: bool | None = None):
    if headed is None:
        headed = os.environ.get(
            "IG_BROWSER_AUTOMATION_HEADED", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}

    browser, context = _browser_connect_live(p, username)
    if context is not None:
        print(
            f"🌐 @{username}: attached to existing live Instagram Chromium session.",
            flush=True,
        )
        return context

    state_path = _browser_storage_state_file(username)

    if _browser_storage_state_available(username):
        browser = p.chromium.launch(
            headless=not headed,
        )
        try:
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1280, "height": 900},
            )
        except Exception:
            try:
                browser.close()
            except Exception:
                pass
            raise

        BROWSER_HANDLE_BY_CONTEXT[id(context)] = browser
        print(
            f"🔐 @{username}: loaded full Instagram Web storage state (fallback).",
            flush=True,
        )
        return context

    context = p.chromium.launch_persistent_context(
        str(_browser_profile_dir(username)),
        headless=not headed,
        viewport={"width": 1280, "height": 900},
    )

    if not _browser_context_logged_in(context):
        restored = _restore_saved_browser_session(
            context,
            username,
        )
        if restored:
            print(
                f"🔐 @{username}: restored legacy Instagram Web session.",
                flush=True,
            )

    return context





def _browser_clickable_from_svg(svg):
    for xpath in (
        "xpath=ancestor::button[1]",
        "xpath=ancestor::*[@role='button'][1]",
        "xpath=ancestor::div[@role='button'][1]",
    ):
        try:
            loc = svg.locator(xpath)
            if loc.count():
                return loc.first
        except Exception:
            pass
    return svg


def _browser_find_svg_action(scope, labels):
    for label in labels:
        try:
            locs = scope.locator(f"svg[aria-label='{label}']")
            for i in range(min(locs.count(), 8)):
                loc = locs.nth(i)
                if loc.is_visible(timeout=250):
                    return loc
        except Exception:
            pass
    return None


def _browser_click_text_button(scope, pattern, timeout=5000):
    try:
        btn = scope.get_by_role(
            "button",
            name=re.compile(pattern, re.I),
        ).first
        if btn.count() and btn.is_visible(timeout=500):
            btn.click(timeout=timeout)
            return True
    except Exception:
        pass

    try:
        item = scope.get_by_text(
            re.compile(pattern, re.I),
            exact=True,
        ).first
        if item.count() and item.is_visible(timeout=500):
            item.click(timeout=timeout)
            return True
    except Exception:
        pass

    return False


def _browser_collect_profile_media_links(page, username: str) -> set[str]:
    try:
        page.goto(
            f"https://www.instagram.com/{username.lstrip('@')}/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(1400)
        hrefs = page.locator(
            "a[href*='/p/'], a[href*='/reel/']"
        ).evaluate_all(
            """els => els.map(e => e.href || e.getAttribute('href') || '')
                         .filter(Boolean)"""
        )
        return {
            str(href).split("?", 1)[0]
            for href in hrefs
            if "/p/" in str(href) or "/reel/" in str(href)
        }
    except Exception:
        return set()


def _browser_follow_debug_snapshot(page, username: str, label: str) -> None:
    """
    Save HTML + screenshot for Browser Follow selector/state debugging.
    """
    safe_user = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(username or "account"))
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label or "follow"))
    debug_dir = DOWNLOAD_ROOT / "browser_debug"
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = debug_dir / f"{safe_user}_{safe_label}_{stamp}"

    try:
        page.screenshot(
            path=str(base.with_suffix(".png")),
            full_page=True,
        )
    except Exception:
        pass

    try:
        base.with_suffix(".html").write_text(
            page.content(),
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        pass


def _browser_wait_for_follow_gap(username: str) -> bool:
    """
    Short Browser-Mode Follow gap. This intentionally does not inherit the old
    40-second instagrapi/API action gap.

    Genuine safety backoff still blocks the action.
    """
    state = get_account_safety_state(username)
    if state["active"]:
        return False

    deadline = time.monotonic() + 60

    while time.monotonic() < deadline:
        last = float(BROWSER_LAST_FOLLOW_ACTION.get(username, 0.0) or 0.0)
        elapsed = time.monotonic() - last
        remaining = BROWSER_FOLLOW_MIN_GAP_SECONDS - elapsed

        if remaining <= 0:
            return True

        time.sleep(min(0.5, max(0.1, remaining)))

    return False


def _browser_candidate_row(root, candidate_name: str):
    """
    Re-query the live follower/following row by username after Instagram rerenders.
    """
    username = str(candidate_name or "").strip().lstrip("@")
    if not username:
        return None

    selectors = (
        f"a[href='/{username}/']",
        f"a[href='/{username}']",
        f"a[href*='/{username}/']",
    )

    for selector in selectors:
        try:
            links = root.locator(selector)
            for i in range(min(links.count(), 8)):
                link = links.nth(i)
                if not link.is_visible(timeout=200):
                    continue

                # Walk upward until we reach the smallest container that also
                # contains a button for this user.
                for depth in range(1, 8):
                    try:
                        row = link.locator(
                            f"xpath=ancestor::div[{depth}]"
                        )
                        if row.count() and row.locator("button").count():
                            return row
                    except Exception:
                        pass
        except Exception:
            pass

    return None


def _browser_row_follow_state(root, candidate_name: str) -> tuple[str, str]:
    """
    Return (state, detail) where state is one of:
      following, requested, follow, ambiguous
    """
    row = _browser_candidate_row(root, candidate_name)
    if row is None:
        return "ambiguous", "candidate row not found"

    texts = []
    try:
        buttons = row.locator("button")
        for i in range(min(buttons.count(), 8)):
            try:
                value = re.sub(
                    r"\s+",
                    " ",
                    buttons.nth(i).inner_text(timeout=500) or "",
                ).strip()
            except Exception:
                value = ""
            if value:
                texts.append(value)

                if re.fullmatch(r"Following", value, re.I):
                    return "following", value
                if re.fullmatch(r"Requested", value, re.I):
                    return "requested", value

    except Exception:
        pass

    if any(re.fullmatch(r"Follow", value, re.I) for value in texts):
        return "follow", " | ".join(texts[:8])

    return "ambiguous", " | ".join(texts[:8])


def _browser_verify_follow_profile(
    context,
    candidate_name: str,
    timeout_seconds: int = 6,
) -> tuple[bool | None, str]:
    """
    Secondary verification for an ambiguous/stale followers-dialog row.

    This only reads the candidate's Instagram Web profile. It does not click.
    """
    username = str(candidate_name or "").strip().lstrip("@")
    if not username or username == "candidate":
        return None, "username unavailable"

    probe = None
    deadline = time.time() + max(3, int(timeout_seconds))
    saw_follow = False
    last_text = ""

    try:
        probe = context.new_page()
        probe.goto(
            f"https://www.instagram.com/{username}/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        probe.wait_for_timeout(900)

        while time.time() < deadline:
            problem = _browser_page_problem(probe)
            if problem:
                return None, f"profile check interrupted: {problem}"

            try:
                buttons = probe.get_by_role(
                    "button",
                    name=re.compile(
                        r"^(Follow|Following|Requested)$",
                        re.I,
                    ),
                )
                for i in range(min(buttons.count(), 8)):
                    btn = buttons.nth(i)
                    if not btn.is_visible(timeout=200):
                        continue

                    value = re.sub(
                        r"\s+",
                        " ",
                        btn.inner_text(timeout=500) or "",
                    ).strip()
                    if not value:
                        continue

                    last_text = value

                    if re.fullmatch(r"Following", value, re.I):
                        return True, "Following"
                    if re.fullmatch(r"Requested", value, re.I):
                        return True, "Requested"
                    if re.fullmatch(r"Follow", value, re.I):
                        saw_follow = True
            except Exception:
                pass

            probe.wait_for_timeout(600)

        if saw_follow:
            return False, last_text or "Follow"

        return None, last_text or "relationship button not found"

    except Exception as exc:
        return None, (
            f"profile verification error: "
            f"{type(exc).__name__}: {str(exc)[:120]}"
        )

    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


def _browser_pause_for_manual_security(username: str, reason: str) -> None:
    """
    Pause automation on security/restriction UI without trying to bypass it.

    The live Chromium window is intentionally left open so the user can inspect
    and complete any legitimate Instagram security step manually.
    """
    username = str(username or "").strip().lstrip("@")
    CONTROL_PAUSED_ACCOUNTS.add(username)
    CONTROL_FORCE_RUN.discard(username)
    NEXT_TASK_OVERRIDE.pop(username, None)

    update_account_metric(
        username,
        "status",
        status="Browser Verification Needed",
    )
    update_account_metric(
        username,
        "add_history",
        value=(
            "🛑 Browser automation paused for manual Instagram security "
            f"attention: {str(reason or 'security prompt')[:180]}. "
            "No further follow clicks will be attempted."
        ),
    )


def _browser_visible_follow_dwell(
    page,
    username: str,
    seconds: float,
) -> str:
    """
    Keep the headed browser visibly stable while polling for security UI.

    Returns a problem string when Instagram presents a security/restriction
    message; otherwise returns an empty string.
    """
    deadline = time.time() + max(0.0, float(seconds))

    while time.time() < deadline:
        problem = _browser_page_problem(page)
        if problem:
            return problem
        page.wait_for_timeout(350)

    return _browser_page_problem(page)


def _browser_wait_for_follow_confirmation_in_place(
    page,
    root,
    candidate_name: str,
    timeout_seconds: int | None = None,
) -> tuple[bool | None, str]:
    """
    Verify Follow only from the SAME Followers/Following dialog row.

    No profile navigation, no extra tabs, no refresh, and no alternate
    relationship-menu clicks. This keeps the browser visibly stable.
    """
    timeout_seconds = (
        BROWSER_FOLLOW_VERIFY_SECONDS
        if timeout_seconds is None
        else max(2, int(timeout_seconds))
    )

    deadline = time.time() + timeout_seconds
    last_state = "ambiguous"
    last_detail = ""

    while time.time() < deadline:
        state, detail = _browser_row_follow_state(
            root,
            candidate_name,
        )
        last_state = state
        last_detail = detail

        if state in {"following", "requested"}:
            return True, detail

        page.wait_for_timeout(500)

    if last_state == "follow":
        return False, last_detail or "Follow"

    return None, last_detail or last_state

def _browser_wait_for_follow_confirmation(
    page,
    context,
    root,
    candidate_name: str,
) -> tuple[bool | None, str]:
    """
    Compatibility wrapper for visible in-place verification.

    The old secondary profile-navigation verifier is intentionally not used.
    """
    return _browser_wait_for_follow_confirmation_in_place(
        page,
        root,
        candidate_name,
        timeout_seconds=BROWSER_FOLLOW_VERIFY_SECONDS,
    )


def _browser_dialog_text(dialog) -> str:
    try:
        return re.sub(
            r"\s+",
            " ",
            dialog.inner_text(timeout=1200) or "",
        ).strip()
    except Exception:
        return ""


def _browser_is_profile_action_menu(dialog) -> bool:
    text = _browser_dialog_text(dialog).lower()
    if not text:
        return False

    markers = (
        "add to close friends",
        "add to favorites",
        "remove from favorites",
        "mute",
        "restrict",
        "unfollow",
        "about this account",
    )
    return sum(marker in text for marker in markers) >= 2


def _browser_close_wrong_profile_menu(page, dialog) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:
        pass

    try:
        if dialog.count() and dialog.is_visible(timeout=200):
            close_svg = dialog.locator("svg[aria-label='Close']").first
            if close_svg.count() and close_svg.is_visible(timeout=200):
                _browser_clickable_from_svg(close_svg).click(timeout=1500)
                page.wait_for_timeout(250)
    except Exception:
        pass


def _browser_network_dialog_valid(dialog, target: str, source: str) -> tuple[bool, str]:
    if not dialog.count():
        return False, "no dialog"

    try:
        if not dialog.is_visible(timeout=500):
            return False, "dialog not visible"
    except Exception:
        return False, "dialog visibility check failed"

    if _browser_is_profile_action_menu(dialog):
        return False, "profile action menu"

    text = _browser_dialog_text(dialog).lower()

    try:
        hrefs = dialog.locator("a[href^='/']").evaluate_all(
            """els => els.map(e => e.getAttribute('href') || '')"""
        )
    except Exception:
        hrefs = []

    usernames = set()
    for href in hrefs:
        match = re.fullmatch(r"/([^/?#]+)/?", str(href))
        if not match:
            continue
        candidate = match.group(1).lower()
        if candidate not in {
            "accounts", "explore", "reels", "direct",
            str(target).lower(),
        }:
            usernames.add(candidate)

    if usernames:
        return True, f"people list with {len(usernames)} profile link(s)"

    if source.lower() in text:
        return True, f"{source} dialog title detected"

    return False, "dialog does not look like a people list"


def _browser_stat_text_matches(value: str, source: str) -> bool:
    """
    Match only profile-stat style labels such as:
      1,234 followers
      82 following
      1.2K followers

    A bare relationship button reading only "Following" can never match.
    """
    value = re.sub(
        r"\s+",
        " ",
        str(value or "").strip(),
    )

    source = str(source or "").strip().lower()

    if source not in {"followers", "following"}:
        return False

    pattern = rf"^[0-9][0-9,.\s]*[KMBkmb]?\s+{re.escape(source)}$"
    return bool(re.fullmatch(pattern, value, re.I))


def _browser_profile_stat_triggers(page, target: str, source: str):
    """
    Return safely verified Followers/Following stat controls.

    Priority:
      1. exact /<target>/<source> href
      2. numeric profile-header stat controls such as "82 following"

    Never returns a bare "Following" relationship button.
    """
    target = str(target or "").strip().lstrip("@")
    source = str(source or "").strip().lower()
    expected = f"/{target}/{source}".lower()

    matches = []
    seen = set()

    def _add(locator):
        try:
            key = locator.evaluate(
                """el => {
                    const r = el.getBoundingClientRect();
                    return [
                        el.tagName,
                        el.getAttribute('href') || '',
                        el.getAttribute('aria-label') || '',
                        (el.innerText || el.textContent || '').trim(),
                        Math.round(r.x), Math.round(r.y),
                        Math.round(r.width), Math.round(r.height)
                    ].join('|');
                }"""
            )
        except Exception:
            key = f"locator:{len(matches)}"

        if key in seen:
            return

        try:
            if locator.is_visible(timeout=150):
                seen.add(key)
                matches.append(locator)
        except Exception:
            pass

    # 1) Exact href match.
    try:
        anchors = page.locator("a[href]")
        for i in range(min(anchors.count(), 300)):
            anchor = anchors.nth(i)

            try:
                href = str(anchor.get_attribute("href") or "")
            except Exception:
                continue

            normalized = (
                href.split("?", 1)[0]
                .split("#", 1)[0]
                .rstrip("/")
                .lower()
            )

            if normalized == expected:
                _add(anchor)
    except Exception:
        pass

    # 2) Current Instagram layouts can render profile stats as buttons/divs
    # without a useful href. Restrict fallback to header/main controls that
    # contain a NUMERIC count plus the exact source word.
    selectors = (
        "header a, header button, header [role='button'], header li",
        "main header a, main header button, main header [role='button'], main header li",
        "main a, main button, main [role='button']",
    )

    for selector in selectors:
        try:
            candidates = page.locator(selector)
            for i in range(min(candidates.count(), 220)):
                candidate = candidates.nth(i)

                try:
                    visible = candidate.is_visible(timeout=100)
                except Exception:
                    visible = False

                if not visible:
                    continue

                try:
                    inner = str(candidate.inner_text(timeout=250) or "").strip()
                except Exception:
                    inner = ""

                try:
                    aria = str(candidate.get_attribute("aria-label") or "").strip()
                except Exception:
                    aria = ""

                try:
                    title = str(candidate.get_attribute("title") or "").strip()
                except Exception:
                    title = ""

                values = (inner, aria, title)

                if any(
                    _browser_stat_text_matches(value, source)
                    for value in values
                ):
                    _add(candidate)
        except Exception:
            pass

    return matches


def _browser_try_direct_network_route(
    page,
    username: str,
    target: str,
    source: str,
):
    """
    Safe fallback: navigate directly to Instagram's normal profile network route
    and accept it only if a validated people-list dialog appears.
    """
    target = str(target or "").strip().lstrip("@")
    source = str(source or "").strip().lower()

    route = f"https://www.instagram.com/{target}/{source}/"

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🧭 Browser Follow: no usable clickable {source} stat was exposed; "
            f"trying Instagram's direct /{target}/{source}/ route..."
        ),
    )

    try:
        page.goto(
            route,
            wait_until="domcontentloaded",
            timeout=60000,
        )
    except Exception:
        pass

    deadline = time.time() + 10

    while time.time() < deadline:
        page.wait_for_timeout(350)

        try:
            _browser_check_ready(
                page,
                page.context,
                username,
            )
        except Exception:
            # Do not mask a normal dialog just because route navigation is
            # mid-transition. The caller will validate the dialog strictly.
            pass

        dialogs = page.locator("[role='dialog']")
        if dialogs.count():
            dialog = dialogs.last

            valid, reason = _browser_network_dialog_valid(
                dialog,
                target,
                source,
            )

            if valid:
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        f"✅ Direct route opened @{target}'s {source} "
                        f"people list ({reason})."
                    ),
                )
                return dialog

            if reason == "profile action menu":
                _browser_close_wrong_profile_menu(
                    page,
                    dialog,
                )
                return None

        # Some layouts render the people list as the main page rather than a
        # formal role=dialog. Require several profile links plus the source word.
        try:
            body = re.sub(
                r"\s+",
                " ",
                page.locator("body").inner_text(timeout=600) or "",
            ).lower()
        except Exception:
            body = ""

        if source in body:
            try:
                hrefs = page.locator(
                    "main a[href^='/']"
                ).evaluate_all(
                    """els => els.map(e => e.getAttribute('href') || '')"""
                )
            except Exception:
                hrefs = []

            usernames = set()
            for href in hrefs:
                match = re.fullmatch(
                    r"/([^/?#]+)/?",
                    str(href),
                )
                if not match:
                    continue

                candidate = match.group(1).lower()

                if candidate not in {
                    target.lower(),
                    "accounts",
                    "explore",
                    "reels",
                    "direct",
                }:
                    usernames.add(candidate)

            if len(usernames) >= 3:
                root = page.locator("main").first
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        f"✅ Direct route opened @{target}'s {source} "
                        f"network page with {len(usernames)} profile link(s)."
                    ),
                )
                return root

    return None

def _browser_profile_stat_links(page, target: str, source: str):
    """
    Compatibility wrapper retained for existing callers.

    The implementation now returns exact href matches OR numeric stat controls
    such as "82 following", never a bare relationship button.
    """
    return _browser_profile_stat_triggers(
        page,
        target,
        source,
    )



def _browser_open_network_list(page, username: str, target: str, source: str):
    """
    Open and validate the exact Followers/Following people list.

    Safe strategies:
      1. exact stat href
      2. numeric profile-stat control ("82 following")
      3. direct /<target>/<source>/ route

    Never clicks a bare "Following" relationship button.
    """
    target = str(target or "").strip().lstrip("@")
    source = str(source or "").strip().lower()

    triggers = _browser_profile_stat_triggers(
        page,
        target,
        source,
    )

    last_reason = ""

    if triggers:
        update_account_metric(
            username,
            "add_history",
            value=(
                f"🔎 Browser Follow found {len(triggers)} verified "
                f"{source} stat trigger(s) for @{target}."
            ),
        )

    for trigger in triggers[:4]:
        try:
            trigger.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        try:
            trigger.click(timeout=5000)
        except Exception:
            try:
                trigger.evaluate("(el) => el.click()")
            except Exception as exc:
                last_reason = f"click failed: {type(exc).__name__}"
                continue

        deadline = time.time() + 8

        while time.time() < deadline:
            page.wait_for_timeout(350)

            dialogs = page.locator("[role='dialog']")
            if not dialogs.count():
                continue

            dialog = dialogs.last

            valid, reason = _browser_network_dialog_valid(
                dialog,
                target,
                source,
            )

            if valid:
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        f"✅ Opened @{target}'s {source} people list "
                        f"({reason})."
                    ),
                )
                return dialog

            last_reason = reason

            if reason == "profile action menu":
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "⚠️ Instagram opened the relationship menu "
                        "(Mute/Restrict/Unfollow) instead of the people list; "
                        "closing it."
                    ),
                )
                _browser_close_wrong_profile_menu(
                    page,
                    dialog,
                )
                break

    # The direct route is preferable to any broad text/button fallback.
    direct_root = _browser_try_direct_network_route(
        page,
        username,
        target,
        source,
    )

    if direct_root is not None:
        return direct_root

    _browser_follow_debug_snapshot(
        page,
        username,
        f"missing_or_invalid_{source}_network_list",
    )

    update_account_metric(
        username,
        "add_history",
        value=(
            f"⚠️ Browser Follow could not open @{target}'s {source} people "
            "list using exact href, numeric stat, or direct route. "
            "No generic Following-button click was attempted."
        ),
    )

    raise RuntimeError(
        f"Could not open @{target}'s verified {source} people list "
        f"({last_reason or 'Instagram layout did not expose a valid list'})."
    )


def _browser_visible_profile_header_text(page) -> str:
    for selector in ("main header", "header", "main"):
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible(timeout=250):
                body = re.sub(
                    r"\s+",
                    " ",
                    loc.inner_text(timeout=900) or "",
                ).strip()
                if body:
                    return body[:700]
        except Exception:
            pass
    return ""


def _browser_click_profile_stat_in_place(
    page,
    target: str,
    source: str,
) -> tuple[bool, str]:
    """
    Stay on the target profile and click only a real numeric Followers/Following
    stat. Bare relationship buttons such as "Following" are rejected.
    """
    target = str(target or "").strip().lstrip("@")
    source = str(source or "").strip().lower()

    if source not in {"followers", "following"}:
        return False, "unsupported source"

    expected = f"/{target}/{source}".lower()

    # Exact profile-stat href, when present.
    try:
        anchors = page.locator("a[href]")
        for i in range(min(anchors.count(), 300)):
            anchor = anchors.nth(i)
            try:
                href = str(anchor.get_attribute("href") or "")
            except Exception:
                continue

            normalized = (
                href.split("?", 1)[0]
                .split("#", 1)[0]
                .rstrip("/")
                .lower()
            )

            if normalized != expected:
                continue

            try:
                if not anchor.is_visible(timeout=120):
                    continue
            except Exception:
                continue

            try:
                label = re.sub(
                    r"\s+",
                    " ",
                    anchor.inner_text(timeout=300) or "",
                ).strip()
            except Exception:
                label = source

            anchor.click(timeout=5000)
            return True, f"exact href stat ({label})"
    except Exception:
        pass

    # Current Instagram layouts sometimes expose stats without useful hrefs.
    # Search only visible profile-header/main controls and require a numeric
    # count + the exact requested word in the same small clickable container.
    try:
        result = page.evaluate(
            """
            ({source}) => {
              const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                       s.visibility !== 'hidden' &&
                       s.display !== 'none';
              };

              const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();

              const numericSource = (txt) => {
                const t = clean(txt);
                const re = new RegExp(
                  '(?:^|\\\\s)(?:\\\\d[\\\\d,.]*|\\\\d+(?:\\\\.\\\\d+)?[KMBkmb])\\\\s+' +
                  source + '(?:\\\\s|$)',
                  'i'
                );
                return re.test(t);
              };

              const roots = [
                document.querySelector('main header'),
                document.querySelector('header'),
                document.querySelector('main')
              ].filter(Boolean);

              for (const root of roots) {
                const nodes = root.querySelectorAll(
                  'a, button, [role="button"], li, span, div'
                );

                for (const node of nodes) {
                  if (!visible(node)) continue;

                  const own = clean(
                    node.getAttribute('aria-label') ||
                    node.getAttribute('title') ||
                    node.innerText ||
                    node.textContent
                  );

                  if (!own.toLowerCase().includes(source)) continue;

                  let cur = node;
                  for (let depth = 0; depth < 5 && cur; depth++, cur = cur.parentElement) {
                    if (!visible(cur)) continue;

                    const txt = clean(
                      cur.getAttribute('aria-label') ||
                      cur.getAttribute('title') ||
                      cur.innerText ||
                      cur.textContent
                    );

                    if (!numericSource(txt)) continue;

                    const clickable = (
                      cur.tagName === 'A' ||
                      cur.tagName === 'BUTTON' ||
                      cur.getAttribute('role') === 'button' ||
                      typeof cur.onclick === 'function' ||
                      getComputedStyle(cur).cursor === 'pointer'
                    );

                    if (!clickable) continue;

                    const r = cur.getBoundingClientRect();

                    // Reject giant layout containers.
                    if (r.width > window.innerWidth * 0.8 ||
                        r.height > window.innerHeight * 0.35) {
                      continue;
                    }

                    cur.click();
                    return {
                      clicked: true,
                      text: txt.slice(0, 180),
                      tag: cur.tagName
                    };
                  }
                }
              }

              return {clicked: false};
            }
            """,
            {"source": source},
        )
    except Exception as exc:
        return False, f"DOM stat search error: {type(exc).__name__}"

    if isinstance(result, dict) and result.get("clicked"):
        return True, f"numeric header stat ({result.get('text') or source})"

    return False, "no numeric profile-header stat control found"


def _browser_open_network_list_stable(
    page,
    username: str,
    target: str,
    source: str,
):
    """
    Manual networking opener that never route-hops.

    It clicks one in-place profile stat once and then waits for exactly one
    validated people-list dialog.
    """
    header = _browser_visible_profile_header_text(page)
    if header:
        update_account_metric(
            username,
            "add_history",
            value=f"👁️ Browser Follow target header: {header[:260]}",
        )

    clicked, detail = _browser_click_profile_stat_in_place(
        page,
        target,
        source,
    )

    if not clicked:
        _browser_follow_debug_snapshot(
            page,
            username,
            f"stable_missing_{source}_stat",
        )
        raise RuntimeError(
            f"Could not locate a visible numeric {source} stat on "
            f"@{target}'s profile ({detail})."
        )

    update_account_metric(
        username,
        "add_history",
        value=(
            f"👆 Clicked @{target}'s {source} stat once: {detail}. "
            "Waiting in place for the people-list popup."
        ),
    )

    deadline = time.time() + 10

    while time.time() < deadline:
        page.wait_for_timeout(350)

        problem = _browser_page_problem(page)
        if problem:
            _browser_pause_for_manual_security(username, problem)
            raise RuntimeError(
                f"Instagram requires manual attention: {problem}"
            )

        dialogs = page.locator("[role='dialog']")
        if not dialogs.count():
            continue

        dialog = dialogs.last
        valid, reason = _browser_network_dialog_valid(
            dialog,
            target,
            source,
        )

        if valid:
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"✅ Opened @{target}'s {source} people list "
                    f"without leaving the target profile ({reason})."
                ),
            )
            return dialog

        if reason == "profile action menu":
            _browser_close_wrong_profile_menu(page, dialog)
            raise RuntimeError(
                "Instagram opened the relationship menu instead of the "
                f"{source} people list; stopped without retrying."
            )

    _browser_follow_debug_snapshot(
        page,
        username,
        f"stable_no_{source}_dialog",
    )

    raise RuntimeError(
        f"Clicked @{target}'s {source} stat, but Instagram did not open "
        "a valid people-list popup within 10s."
    )

def _local_day_key(timestamp: float | None = None) -> str:
    ts = time.time() if timestamp is None else float(timestamp)
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _daily_follow_attempts_status(username: str) -> tuple[int, int]:
    """
    Return (used_today, hard_cap) from the persistent shared pacing file.
    """
    fd = _acquire_shared_pacing_lock()
    if fd is None:
        return 0, DAILY_FOLLOW_HARD_CAP

    try:
        data = _load_shared_pacing()
        row = data.setdefault("accounts", {}).setdefault(username, {})
        today = _local_day_key()

        if str(row.get("follow_day") or "") != today:
            return 0, DAILY_FOLLOW_HARD_CAP

        used = int(row.get("follow_attempts_today") or 0)
        return max(0, used), DAILY_FOLLOW_HARD_CAP
    finally:
        _release_shared_pacing_lock(fd)

def _reserve_browser_follow_slot(username: str) -> tuple[bool, str]:
    """
    Reserve one Browser Follow attempt.

    Enforces:
      * safety backoff
      * 50 follow-click attempts per local calendar day, persisted on disk
      * configured rolling write budget
      * cross-process global minimum gap

    The daily counter increments BEFORE the click so ambiguous/failed follow
    attempts still count toward the safety ceiling.
    """
    allowed, reason = account_writes_allowed(username)
    if not allowed:
        return False, reason

    fd = _acquire_shared_pacing_lock()
    if fd is None:
        return False, "shared pacing lock busy"

    try:
        data = _load_shared_pacing()
        now = time.time()
        today = _local_day_key(now)

        settings = get_account_control_settings(username)
        window = int(settings["write_window_seconds"])
        max_window = int(settings["max_writes_per_window"])

        accounts = data.setdefault("accounts", {})
        row = accounts.setdefault(
            username,
            {
                "writes": [],
                "last_write": 0.0,
                "last_upload": 0.0,
                "pending_upload_until": 0.0,
                "follow_day": today,
                "follow_attempts_today": 0,
            },
        )

        if str(row.get("follow_day") or "") != today:
            row["follow_day"] = today
            row["follow_attempts_today"] = 0

        used_today = int(row.get("follow_attempts_today") or 0)

        if used_today >= DAILY_FOLLOW_HARD_CAP:
            return (
                False,
                f"daily follow hard cap reached "
                f"({used_today}/{DAILY_FOLLOW_HARD_CAP}); resumes next local day",
            )

        cutoff = now - window
        row["writes"] = [
            float(t)
            for t in row.get("writes", [])
            if isinstance(t, (int, float)) and float(t) >= cutoff
        ]

        if len(row["writes"]) >= max_window:
            wait = int(max(1, row["writes"][0] + window - now))
            return False, f"rolling write budget; retry in ~{wait}s"

        global_last = float(data.get("global_last") or 0.0)
        if now - global_last < GLOBAL_WRITE_MIN_GAP_SECONDS:
            wait = int(
                max(
                    1,
                    GLOBAL_WRITE_MIN_GAP_SECONDS - (now - global_last),
                )
            )
            return False, f"shared global gap; retry in ~{wait}s"

        # Reserve the attempt atomically before Chromium clicks Follow.
        row["follow_attempts_today"] = used_today + 1
        row["writes"].append(now)
        row["last_write"] = now
        data["global_last"] = now
        _save_shared_pacing(data)

        return True, ""
    finally:
        _release_shared_pacing_lock(fd)



def _next_overnight_auto_delay(
    username: str,
    result: str,
    *,
    manual_run: bool = False,
) -> tuple[int, str]:
    if result != "ran":
        if result in {"disconnected", "paused"}:
            return max(WORKFLOW_RETRY_IDLE_SECONDS, 300), "idle/disconnected"
        return WORKFLOW_RETRY_IDLE_SECONDS, "retry"

    if manual_run:
        return random.randint(60, 150), "short rest after manual action"

    pace_mode = _runtime_pace_mode(username)

    if pace_mode == "burst":
        # Burst Now is temporary. After the active worker finishes, retire the
        # request and return to the account's persisted mode.
        BURST_UNTIL.pop(username, None)
        persisted = get_account_control_settings(username).get(
            "pace_mode",
            "normal",
        )
        if persisted == "overnight":
            delay = random.randint(
                OVERNIGHT_REST_MIN_SECONDS,
                OVERNIGHT_REST_MAX_SECONDS,
            )
            return delay, "post-burst overnight rest"
        return random.randint(120, 300), "post-burst rest"

    if pace_mode == "overnight":
        count = int(AUTO_SUCCESS_PASSES.get(username, 0) or 0) + 1
        target = int(
            AUTO_LONG_BREAK_TARGET.get(username, 0)
            or random.randint(
                AUTO_PASSES_BEFORE_LONG_MIN,
                AUTO_PASSES_BEFORE_LONG_MAX,
            )
        )
        AUTO_SUCCESS_PASSES[username] = count
        AUTO_LONG_BREAK_TARGET[username] = target

        if count >= target:
            delay = random.randint(
                OVERNIGHT_LONG_REST_MIN_SECONDS,
                OVERNIGHT_LONG_REST_MAX_SECONDS,
            )
            AUTO_SUCCESS_PASSES[username] = 0
            AUTO_LONG_BREAK_TARGET[username] = random.randint(
                AUTO_PASSES_BEFORE_LONG_MIN,
                AUTO_PASSES_BEFORE_LONG_MAX,
            )
            return delay, "scheduled long overnight rest"

        delay = random.randint(
            OVERNIGHT_REST_MIN_SECONDS,
            OVERNIGHT_REST_MAX_SECONDS,
        )
        return delay, f"overnight rest ({count}/{target} active sessions before long rest)"

    # Normal pace retains the quieter single-pass cadence.
    count = int(AUTO_SUCCESS_PASSES.get(username, 0) or 0) + 1
    target = int(
        AUTO_LONG_BREAK_TARGET.get(username, 0)
        or random.randint(
            AUTO_PASSES_BEFORE_LONG_MIN,
            AUTO_PASSES_BEFORE_LONG_MAX,
        )
    )
    AUTO_SUCCESS_PASSES[username] = count
    AUTO_LONG_BREAK_TARGET[username] = target

    if count >= target:
        delay = random.randint(
            AUTO_LONG_REST_MIN_SECONDS,
            AUTO_LONG_REST_MAX_SECONDS,
        )
        AUTO_SUCCESS_PASSES[username] = 0
        AUTO_LONG_BREAK_TARGET[username] = random.randint(
            AUTO_PASSES_BEFORE_LONG_MIN,
            AUTO_PASSES_BEFORE_LONG_MAX,
        )
        return delay, "normal scheduled long rest"

    delay = random.randint(
        AUTO_ACTIVE_REST_MIN_SECONDS,
        AUTO_ACTIVE_REST_MAX_SECONDS,
    )
    return delay, f"normal rest ({count}/{target} passes before long rest)"


def _browser_follow_network(username, history, config, manual=False) -> int:
    settings = get_account_control_settings(username, config)
    if not manual and not settings.get("enable_follow", False):
        return 0

    targets = settings["target_accounts"] or list(
        config.get("competitor_accounts") or []
    )
    if not targets:
        update_account_metric(
            username,
            "add_history",
            value="⚠️ Browser Follow is enabled but no target accounts are configured.",
        )
        return 0

    target = random.choice(targets).lstrip("@")
    source = settings.get("follow_source", "followers")
    if source == "both":
        source = random.choice(["followers", "following"])

    configured_limit = max(
        1,
        int(settings.get("follow_limit", 1)),
    )

    if manual:
        # Manual Network stays one visible action per dashboard click.
        limit = 1
    else:
        # Auto works in a bounded batch while the verified people-list popup
        # is already open. The batch target varies from 1..N, with N capped at 5.
        auto_cap = max(
            1,
            min(
                AUTO_FOLLOW_BATCH_MAX,
                int(settings.get("auto_follow_batch_max", AUTO_FOLLOW_BATCH_MAX)),
                10,
            ),
        )
        limit = random.randint(AUTO_FOLLOW_BATCH_MIN, auto_cap)

    daily_used, daily_cap = _daily_follow_attempts_status(username)

    if daily_used >= daily_cap:
        update_account_metric(
            username,
            "add_history",
            value=(
                f"🛑 Browser Follow skipped: daily follow hard cap reached "
                f"({daily_used}/{daily_cap})."
            ),
        )
        return 0

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🌐 Browser Follow: @{target} {source}; "
            f"batch target={limit}; "
            f"mode={'visible-manual' if manual else 'visible-auto'}; "
            f"visible rows only; no list refresh/scroll; "
            f"daily follows={daily_used}/{daily_cap}; "
            f"browser gap≈{BROWSER_FOLLOW_MIN_GAP_SECONDS}s"
        ),
    )

    confirmed_count = 0
    attempt_count = 0
    attempted_users = set()

    with sync_playwright() as p:
        context = _browser_launch(
            p,
            username,
            headed=True if manual else None,
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto(
                f"https://www.instagram.com/{target}/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(1400)

            # Recover the normal saved-profile chooser at most once, then
            # return to the requested target profile once.
            if _browser_saved_account_resume_visible(page, username):
                _browser_resume_saved_account(
                    page,
                    context,
                    username,
                )
                page.goto(
                    f"https://www.instagram.com/{target}/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(1400)

            _browser_check_ready(page, context, username)

            # Manual and Auto both use the stable in-place profile-header
            # opener. No direct /followers/ route hopping or list refreshes.
            dialog = _browser_open_network_list_stable(
                page,
                username,
                target,
                source,
            )

            page.wait_for_timeout(500)
            _browser_check_ready(page, context, username)

            # Validated Followers/Following people-list dialog only.
            root = dialog

            idle_rounds = 0

            # HARD CAP IS ATTEMPTS, NOT CONFIRMATIONS.
            while attempt_count < limit and idle_rounds < 5:
                _browser_check_ready(page, context, username)

                buttons = root.get_by_role(
                    "button",
                    name=re.compile(r"^Follow$", re.I),
                )
                count = buttons.count()

                candidate = None
                candidate_name = ""

                for i in range(min(count, 40)):
                    button = buttons.nth(i)

                    try:
                        if not button.is_visible(timeout=200):
                            continue
                    except Exception:
                        continue

                    row = button.locator(
                        "xpath=ancestor::div[.//a][1]"
                    )

                    hrefs = []
                    try:
                        hrefs = row.locator(
                            "a[href^='/']"
                        ).evaluate_all(
                            """els => els.map(
                                e => e.getAttribute('href') || ''
                            )"""
                        )
                    except Exception:
                        pass

                    for candidate_href in hrefs:
                        match = re.fullmatch(
                            r"/([^/?#]+)/?",
                            str(candidate_href),
                        )
                        if not match:
                            continue

                        possible = match.group(1)
                        if possible.lower() in {
                            target.lower(),
                            "accounts",
                            "explore",
                        }:
                            continue

                        candidate_name = possible
                        break

                    if not candidate_name:
                        candidate_name = f"candidate_{i}"

                    if candidate_name in attempted_users:
                        continue

                    attempted_users.add(candidate_name)
                    candidate = button
                    break

                if candidate is None:
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            "👁️ Browser Follow: no more currently visible "
                            "Follow buttons in this open people-list popup; "
                            "ending the batch without scrolling or refreshing."
                        ),
                    )
                    break

                idle_rounds = 0

                if not _browser_wait_for_follow_gap(username):
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            "⏳ Browser Follow stopped by safety/pacing state."
                        ),
                    )
                    break

                slot_ok, slot_reason = _reserve_browser_follow_slot(username)
                if not slot_ok:
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            "⏳ Browser Follow batch stopped by rolling "
                            f"write budget: {slot_reason}"
                        ),
                    )
                    break

                attempt_count += 1
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        f"👤 Browser Follow candidate "
                        f"{attempt_count}/{limit}: @{candidate_name}. "
                        "Keeping the people-list dialog visible before clicking."
                    ),
                )

                # Let the headed browser visibly settle instead of immediately
                # jumping/clicking like the old fast routine.
                preview_seconds = 2.5 if manual else 1.25
                problem = _browser_visible_follow_dwell(
                    page,
                    username,
                    preview_seconds,
                )
                if problem:
                    _browser_pause_for_manual_security(
                        username,
                        problem,
                    )
                    break

                try:
                    candidate.click(timeout=4000)
                except Exception:
                    candidate.evaluate("(el) => el.click()")

                BROWSER_LAST_FOLLOW_ACTION[username] = time.monotonic()

                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        f"👆 Follow clicked for @{candidate_name}; "
                        "waiting visibly in the same dialog for Instagram's "
                        "button state to settle."
                    ),
                )

                settle_seconds = 4.0 if manual else 2.0
                problem = _browser_visible_follow_dwell(
                    page,
                    username,
                    settle_seconds,
                )
                if problem:
                    _browser_pause_for_manual_security(
                        username,
                        problem,
                    )
                    break

                confirmed, detail = (
                    _browser_wait_for_follow_confirmation_in_place(
                        page,
                        root,
                        candidate_name,
                        timeout_seconds=BROWSER_FOLLOW_VERIFY_SECONDS,
                    )
                )

                if confirmed is True:
                    record_write(username, "follow")
                    confirmed_count += 1
                    update_account_metric(
                        username,
                        "total_follows",
                        increment=1,
                    )
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            f"✅ Browser Follow confirmed: "
                            f"@{candidate_name} "
                            f"({confirmed_count} confirmed / "
                            f"{attempt_count} attempted) "
                            f"[{detail[:100]}]"
                        ),
                    )

                elif confirmed is False:
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            f"⚠️ Browser Follow explicitly not confirmed for "
                            f"@{candidate_name}: {detail[:160]}"
                        ),
                    )

                else:
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            f"⚠️ Browser Follow state ambiguous for "
                            f"@{candidate_name}; not counted: "
                            f"{detail[:160]}"
                        ),
                    )

                # Keep the same people-list viewport fixed for the entire
                # batch. We only act on Follow buttons that are already visible.

                if attempt_count < limit:
                    between_follow_gap = random.uniform(3.0, 6.0)
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            f"… Visible follow batch gap ~{between_follow_gap:.1f}s "
                            "before the next currently visible row."
                        ),
                    )
                    page.wait_for_timeout(int(between_follow_gap * 1000))

            if attempt_count and not confirmed_count:
                _browser_follow_debug_snapshot(
                    page,
                    username,
                    "follow_zero_confirmed",
                )
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "🧪 Browser Follow had attempts but zero confirmations; "
                        "saved screenshot/HTML under browser_debug."
                    ),
                )

            daily_used_after, daily_cap_after = (
                _daily_follow_attempts_status(username)
            )
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"🌐 Browser Follow visible batch finished: "
                    f"{attempt_count}/{limit} attempt(s), "
                    f"{confirmed_count} confirmed; popup stayed in place; "
                    f"daily follows={daily_used_after}/{daily_cap_after}."
                ),
            )

        finally:
            try:
                _browser_refresh_saved_sessionid(
                    context,
                    username,
                )
            except Exception:
                pass
            _browser_close_context(context)

    return confirmed_count



def _browser_engage_percent(settings: dict, key: str, default: int) -> bool:
    try:
        pct = int(settings.get(key, default))
    except Exception:
        pct = default
    pct = max(0, min(100, pct))
    return random.random() * 100.0 < pct


def _browser_engage_security_problem(page, username: str) -> str:
    problem = _browser_page_problem(page)
    if problem:
        apply_account_safety_backoff(
            username,
            f"Instagram Web engagement restriction/challenge: {problem}",
            level="restricted",
            hours=4,
        )
        _browser_pause_for_manual_security(username, problem)
        return problem
    return ""


def _browser_try_like_engage(page, username: str, href: str, history: dict) -> bool:
    if href in history.setdefault("browser_liked_urls", []):
        return False

    like_svg = _browser_find_svg_action(page, ("Like",))
    if like_svg is None:
        update_account_metric(
            username,
            "add_history",
            value=f"↪️ Engage skip Like: control not found on {href}",
        )
        return False

    if not wait_for_write_slot(
        username,
        "like",
        max_wait=0,
        fail_fast=True,
    ):
        update_account_metric(
            username,
            "add_history",
            value="↪️ Engage skip Like: current write pacing/budget is full; browsing continues.",
        )
        return False

    try:
        _browser_clickable_from_svg(like_svg).click(timeout=4000)
        page.wait_for_timeout(650)
    except Exception as exc:
        update_account_metric(
            username,
            "add_history",
            value=f"⚠️ Engage Like click failed; continuing: {type(exc).__name__}",
        )
        return False

    if _browser_find_svg_action(page, ("Unlike",)) is not None:
        record_write(username, "like")
        history["browser_liked_urls"].append(href)
        history["browser_liked_urls"] = history["browser_liked_urls"][-5000:]
        update_account_metric(username, "total_likes", increment=1)
        update_account_metric(
            username,
            "add_history",
            value=f"❤️ Browser Like confirmed: {href}",
        )
        return True

    update_account_metric(
        username,
        "add_history",
        value=f"⚠️ Browser Like was not confirmed; continuing to next action/clip: {href}",
    )
    return False


def _browser_try_repost_engage(page, username: str, href: str, history: dict) -> bool:
    history.setdefault("browser_reposted_urls", [])
    if href in history["browser_reposted_urls"]:
        return False

    repost_svg = _browser_find_svg_action(page, ("Repost",))
    if repost_svg is None:
        # Some layouts expose the control as a button with accessible text.
        try:
            button = page.get_by_role(
                "button",
                name=re.compile(r"^Repost$", re.I),
            ).first
            if not button.count() or not button.is_visible(timeout=250):
                button = None
        except Exception:
            button = None
    else:
        button = _browser_clickable_from_svg(repost_svg)

    if button is None:
        update_account_metric(
            username,
            "add_history",
            value=f"↪️ Engage skip Repost: control not found on {href}",
        )
        return False

    if not wait_for_write_slot(
        username,
        "repost",
        max_wait=0,
        fail_fast=True,
    ):
        update_account_metric(
            username,
            "add_history",
            value="↪️ Engage skip Repost: current write pacing/budget is full; browsing continues.",
        )
        return False

    try:
        button.click(timeout=4000)
        page.wait_for_timeout(500)

        # If Instagram opens a small confirmation menu, choose its exact
        # Repost item once. Do not click broad Share controls.
        try:
            exact = page.get_by_text(
                re.compile(r"^Repost$", re.I),
                exact=True,
            )
            for i in range(min(exact.count(), 4)):
                item = exact.nth(i)
                if item.is_visible(timeout=150):
                    item.click(timeout=2500)
                    page.wait_for_timeout(650)
                    break
        except Exception:
            pass
    except Exception as exc:
        update_account_metric(
            username,
            "add_history",
            value=f"⚠️ Engage Repost click failed; continuing: {type(exc).__name__}",
        )
        return False

    confirmed = (
        _browser_find_svg_action(
            page,
            ("Remove repost", "Undo repost", "Reposted"),
        )
        is not None
    )

    if not confirmed:
        try:
            body = re.sub(
                r"\s+",
                " ",
                page.locator("body").inner_text(timeout=500) or "",
            ).lower()
            confirmed = any(
                phrase in body
                for phrase in (
                    "reposted",
                    "remove repost",
                    "undo repost",
                )
            )
        except Exception:
            confirmed = False

    if confirmed:
        record_write(username, "repost")
        history["browser_reposted_urls"].append(href)
        history["browser_reposted_urls"] = history["browser_reposted_urls"][-5000:]
        update_account_metric(
            username,
            "add_history",
            value=f"🔁 Browser Repost confirmed: {href}",
        )
        return True

    update_account_metric(
        username,
        "add_history",
        value=f"⚠️ Browser Repost state ambiguous; not counted and browsing continues: {href}",
    )
    return False


def _browser_engage_comment_text(page, username: str) -> str:
    settings = get_account_control_settings(username)
    persona = str(
        settings.get("persona_prompt", RAGE_BAIT_PERSONA)
        or RAGE_BAIT_PERSONA
    ).strip()
    extra = str(settings.get("comment_prompt", "") or "").strip()

    try:
        scope = page.locator("article").first
        if not scope.count():
            scope = page.locator("main").first
        visible = re.sub(
            r"\s+",
            " ",
            scope.inner_text(timeout=900) or "",
        ).strip()[:1200]
    except Exception:
        visible = ""

    if not visible:
        return ""

    prompt = f"""
Write ONE short Instagram comment about this visible post/reel context:

{visible}

ACCOUNT COMMENT INSTRUCTIONS:
{extra or "(none)"}

Rules:
- Comment on the actual visible subject.
- Do not invent facts, identities, relationships, or locations.
- Do not mention automation, bots, prompts, or source metadata.
- No threats or slurs.
- 3 to 24 words.
- Output only the comment.
""".strip()

    result = _ollama_generate(
        prompt,
        min_words=3,
        max_words=24,
        attempts=2,
        system_prompt=persona,
    )
    return _clip_chars(
        result,
        int(settings.get("reply_char_limit", 280)),
    ) if result else ""


def _browser_try_comment_engage(page, username: str, href: str, history: dict) -> bool:
    history.setdefault("browser_commented_urls", [])
    if href in history["browser_commented_urls"]:
        return False

    try:
        box = page.locator(
            "textarea[placeholder*='comment' i], "
            "textarea[aria-label*='comment' i]"
        ).first
        if not box.count() or not box.is_visible(timeout=300):
            update_account_metric(
                username,
                "add_history",
                value=f"↪️ Engage skip Comment: comment box not found on {href}",
            )
            return False
    except Exception:
        return False

    comment = _browser_engage_comment_text(page, username)
    if not comment:
        update_account_metric(
            username,
            "add_history",
            value=f"↪️ Engage skip Comment: no usable generated comment for {href}",
        )
        return False

    if not wait_for_write_slot(
        username,
        "comment",
        max_wait=0,
        fail_fast=True,
    ):
        update_account_metric(
            username,
            "add_history",
            value="↪️ Engage skip Comment: current write pacing/budget is full; browsing continues.",
        )
        return False

    try:
        box.fill(comment)
        page.wait_for_timeout(200)

        post_button = page.get_by_role(
            "button",
            name=re.compile(r"^(Post|Submit)$", re.I),
        ).first

        if not post_button.count() or not post_button.is_visible(timeout=300):
            update_account_metric(
                username,
                "add_history",
                value="↪️ Engage skip Comment: Post button was not available.",
            )
            return False

        post_button.click(timeout=4000)
        page.wait_for_timeout(800)
    except Exception as exc:
        update_account_metric(
            username,
            "add_history",
            value=f"⚠️ Engage Comment failed; continuing: {type(exc).__name__}",
        )
        return False

    try:
        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=700) or "",
        )
        confirmed = comment.lower() in body.lower()
    except Exception:
        confirmed = False

    if confirmed:
        record_write(username, "comment")
        history["browser_commented_urls"].append(href)
        history["browser_commented_urls"] = history["browser_commented_urls"][-5000:]
        update_account_metric(
            username,
            "add_history",
            value=f"💬 Browser Comment confirmed: {comment[:120]}",
        )
        return True

    update_account_metric(
        username,
        "add_history",
        value="⚠️ Browser Comment was submitted but not visibly confirmed; browsing continues.",
    )
    return False


def _browser_try_follow_author_engage(page, username: str, href: str) -> bool:
    used, cap = _daily_follow_attempts_status(username)
    if used >= cap:
        update_account_metric(
            username,
            "add_history",
            value=f"↪️ Engage skip Follow-author: daily hard cap reached ({used}/{cap}).",
        )
        return False

    scope = page.locator("article").first
    if not scope.count():
        scope = page.locator("main").first

    try:
        follows = scope.get_by_role(
            "button",
            name=re.compile(r"^Follow$", re.I),
        )
        button = None
        for i in range(min(follows.count(), 5)):
            candidate = follows.nth(i)
            if candidate.is_visible(timeout=150):
                button = candidate
                break
    except Exception:
        button = None

    if button is None:
        update_account_metric(
            username,
            "add_history",
            value=f"↪️ Engage skip Follow-author: exact Follow button not found on {href}",
        )
        return False

    ok, reason = _reserve_browser_follow_slot(username)
    if not ok:
        update_account_metric(
            username,
            "add_history",
            value=f"↪️ Engage skip Follow-author: {reason}",
        )
        return False

    try:
        button.click(timeout=4000)
        page.wait_for_timeout(750)
    except Exception as exc:
        update_account_metric(
            username,
            "add_history",
            value=f"⚠️ Engage Follow-author click failed; continuing: {type(exc).__name__}",
        )
        return False

    confirmed = False
    detail = ""

    for state in ("Following", "Requested"):
        try:
            loc = scope.get_by_role(
                "button",
                name=re.compile(rf"^{re.escape(state)}$", re.I),
            ).first
            if loc.count() and loc.is_visible(timeout=250):
                confirmed = True
                detail = state
                break
        except Exception:
            pass

    if confirmed:
        record_write(username, "follow")
        update_account_metric(username, "total_follows", increment=1)
        used_after, cap_after = _daily_follow_attempts_status(username)
        update_account_metric(
            username,
            "add_history",
            value=(
                f"✅ Engage Follow-author confirmed [{detail}]; "
                f"daily follows={used_after}/{cap_after}."
            ),
        )
        return True

    update_account_metric(
        username,
        "add_history",
        value="⚠️ Engage Follow-author state ambiguous; not counted as confirmed.",
    )
    return False


def _browser_collect_engage_links(
    page,
    username: str,
    target_count: int,
    scroll_steps: int,
) -> list[str]:
    """
    Browse/scroll the hashtag page and collect unique post/reel links.
    Scrolling is read-only and continues even when write actions are unavailable.
    """
    links = []

    def collect():
        try:
            hrefs = page.locator(
                "a[href*='/p/'], a[href*='/reel/']"
            ).evaluate_all(
                """els => els.map(e => e.href || e.getAttribute('href') || '')
                             .filter(Boolean)"""
            )
        except Exception:
            hrefs = []

        for href in hrefs:
            clean = str(href).split("?", 1)[0]
            if clean and clean not in links:
                links.append(clean)

    collect()

    for step in range(max(1, int(scroll_steps))):
        problem = _browser_page_problem(page)
        if problem:
            _browser_pause_for_manual_security(username, problem)
            break

        try:
            page.mouse.wheel(0, random.randint(650, 1100))
        except Exception:
            try:
                page.evaluate(
                    "(y) => window.scrollBy({top:y,behavior:'smooth'})",
                    random.randint(650, 1100),
                )
            except Exception:
                pass

        page.wait_for_timeout(random.randint(450, 850))
        collect()

        update_account_metric(
            username,
            "add_history",
            value=(
                f"↕️ Engage discovery scroll {step + 1}/{scroll_steps}; "
                f"candidate links={len(links)}."
            ),
        )

        if len(links) >= max(target_count * 2, target_count + 4):
            # We already have enough material for this pass; don't scroll just
            # to manufacture additional load.
            break

    return links

def _browser_current_visible_reel_key(page, fallback_index: int = 0) -> str:
    """
    Find the reel permalink nearest the viewport center without navigating.
    """
    try:
        href = page.evaluate(
            """
            () => {
              const links = [...document.querySelectorAll('a[href*="/reel/"]')];
              const vh = window.innerHeight || 1;
              const vw = window.innerWidth || 1;
              let best = null;
              let bestScore = Infinity;

              for (const a of links) {
                const r = a.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                if (r.bottom <= 0 || r.top >= vh) continue;

                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;
                const score =
                  Math.abs(cx - vw / 2) * 0.25 +
                  Math.abs(cy - vh / 2);

                if (score < bestScore) {
                  bestScore = score;
                  best = a.href || a.getAttribute('href') || '';
                }
              }
              return best || '';
            }
            """
        )
    except Exception:
        href = ""

    href = str(href or "").split("?", 1)[0].strip()
    if href:
        return href

    return f"reels-demo:{int(fallback_index)}:{int(time.time())}"


def _browser_open_reel_comments_if_needed(page) -> bool:
    """
    Open the visible reel's comments panel if Instagram exposes a Comment
    action. Missing controls are non-fatal.
    """
    try:
        existing = page.locator(
            "textarea[placeholder*='comment' i], "
            "textarea[aria-label*='comment' i]"
        ).first
        if existing.count() and existing.is_visible(timeout=200):
            return True
    except Exception:
        pass

    comment_svg = _browser_find_svg_action(
        page,
        ("Comment", "Comments"),
    )
    if comment_svg is None:
        return False

    try:
        _browser_clickable_from_svg(comment_svg).click(timeout=3500)
        page.wait_for_timeout(500)
    except Exception:
        return False

    try:
        box = page.locator(
            "textarea[placeholder*='comment' i], "
            "textarea[aria-label*='comment' i]"
        ).first
        return bool(
            box.count()
            and box.is_visible(timeout=400)
        )
    except Exception:
        return False


def _browser_reels_scroll_next(page) -> bool:
    """
    Advance the Reels feed without refresh/navigation.
    """
    try:
        vh = int(
            page.evaluate(
                "() => Math.max(500, window.innerHeight || 800)"
            )
        )
    except Exception:
        vh = 800

    try:
        page.mouse.wheel(
            0,
            int(vh * random.uniform(0.82, 1.05)),
        )
        page.wait_for_timeout(random.randint(900, 1500))
        return True
    except Exception:
        try:
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(random.randint(900, 1500))
            return True
        except Exception:
            return False


def _browser_reels_demo(username, history, config) -> int:
    """
    Manual, visible Reels demonstration.

    Opens Instagram Reels once, then stays in the feed and advances by scrolling.
    Individual action failures never terminate the demo. Security/restriction
    UI does terminate it immediately.
    """
    settings = get_account_control_settings(username, config)

    reel_target = max(
        1,
        min(
            10,
            int(settings.get("engage_clips_per_pass", 6)),
        ),
    )

    history.setdefault("browser_liked_urls", [])
    history.setdefault("browser_reposted_urls", [])
    history.setdefault("browser_commented_urls", [])

    viewed = 0
    confirmed_actions = 0
    seen_keys = set()

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🎞️ Reels Demo starting: up to {reel_target} reel(s); "
            f"Like≈{int(settings.get('engage_like_percent',75))}% · "
            f"Repost≈{int(settings.get('engage_repost_percent',75))}% · "
            f"Comment≈{int(settings.get('engage_comment_percent',10))}% "
            f"{'(enabled)' if settings.get('enable_comments',False) else '(comments disabled)'} · "
            f"Follow-author≈{int(settings.get('engage_follow_percent',25))}% "
            f"{'(enabled)' if settings.get('enable_follow',False) else '(follow disabled)'}."
        ),
    )

    with sync_playwright() as p:
        context = _browser_launch(
            p,
            username,
            headed=True,
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto(
                "https://www.instagram.com/reels/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(1400)
            _browser_check_ready(page, context, username)

            while viewed < reel_target:
                if username in CONTROL_PAUSED_ACCOUNTS:
                    break

                safety = get_account_safety_state(username)
                if safety["active"]:
                    break

                problem = _browser_engage_security_problem(
                    page,
                    username,
                )
                if problem:
                    break

                reel_key = _browser_current_visible_reel_key(
                    page,
                    viewed + 1,
                )

                # If a small scroll did not advance to a distinct reel, try one
                # more feed advance rather than refreshing/reloading the page.
                if reel_key in seen_keys:
                    if not _browser_reels_scroll_next(page):
                        break
                    reel_key = _browser_current_visible_reel_key(
                        page,
                        viewed + 1,
                    )
                    if reel_key in seen_keys:
                        update_account_metric(
                            username,
                            "add_history",
                            value=(
                                "↪️ Reels Demo could not identify a new visible "
                                "reel after scrolling; ending without refresh."
                            ),
                        )
                        break

                seen_keys.add(reel_key)
                viewed += 1

                update_account_metric(
                    username,
                    "add_history",
                    value=f"🎬 Reels Demo {viewed}/{reel_target}: {reel_key}",
                )

                # Allow the reel UI to settle visibly before optional actions.
                page.wait_for_timeout(random.randint(900, 1500))

                if _browser_engage_percent(
                    settings,
                    "engage_like_percent",
                    75,
                ):
                    if _browser_try_like_engage(
                        page,
                        username,
                        reel_key,
                        history,
                    ):
                        confirmed_actions += 1

                if _browser_engage_security_problem(page, username):
                    break

                if _browser_engage_percent(
                    settings,
                    "engage_repost_percent",
                    75,
                ):
                    if _browser_try_repost_engage(
                        page,
                        username,
                        reel_key,
                        history,
                    ):
                        confirmed_actions += 1

                if _browser_engage_security_problem(page, username):
                    break

                if (
                    settings.get("enable_follow", False)
                    and _browser_engage_percent(
                        settings,
                        "engage_follow_percent",
                        25,
                    )
                ):
                    if _browser_try_follow_author_engage(
                        page,
                        username,
                        reel_key,
                    ):
                        confirmed_actions += 1

                if _browser_engage_security_problem(page, username):
                    break

                if (
                    settings.get("enable_comments", False)
                    and _browser_engage_percent(
                        settings,
                        "engage_comment_percent",
                        10,
                    )
                ):
                    if _browser_open_reel_comments_if_needed(page):
                        if _browser_try_comment_engage(
                            page,
                            username,
                            reel_key,
                            history,
                        ):
                            confirmed_actions += 1
                    else:
                        update_account_metric(
                            username,
                            "add_history",
                            value=(
                                "↪️ Reels Demo skip Comment: visible reel "
                                "comment control/panel was not available."
                            ),
                        )

                if _browser_engage_security_problem(page, username):
                    break

                if viewed >= reel_target:
                    break

                update_account_metric(
                    username,
                    "add_history",
                    value="↕️ Reels Demo scrolling to the next reel; no refresh.",
                )

                if not _browser_reels_scroll_next(page):
                    update_account_metric(
                        username,
                        "add_history",
                        value="⚠️ Reels Demo could not advance the feed; ending.",
                    )
                    break

        finally:
            try:
                _browser_refresh_saved_sessionid(
                    context,
                    username,
                )
            except Exception:
                pass
            _browser_close_context(context)

    history["browser_liked_urls"] = history["browser_liked_urls"][-5000:]
    history["browser_reposted_urls"] = history["browser_reposted_urls"][-5000:]
    history["browser_commented_urls"] = history["browser_commented_urls"][-5000:]

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🎞️ Reels Demo finished: viewed {viewed} reel(s), "
            f"confirmed actions={confirmed_actions}. "
            "The Reels feed was advanced by scrolling, not refreshing."
        ),
    )

    return confirmed_actions

def _browser_engage_hashtag(username, history, config, manual=False) -> int:
    settings = get_account_control_settings(username, config)

    if not manual and not settings.get("enable_engage", False):
        return 0

    tags = _normalize_hashtag_list(
        settings["target_hashtags"]
        or list(config.get("target_hashtags") or [])
    )
    if not tags:
        update_account_metric(
            username,
            "add_history",
            value="⚠️ Browser Engage: no valid target hashtags configured.",
        )
        return 0

    tag = random.choice(tags).lstrip("#")
    clip_target = max(
        1,
        min(
            20,
            int(settings.get("engage_clips_per_pass", 12)),
        ),
    )
    scroll_steps = max(
        1,
        min(
            20,
            int(settings.get("engage_scroll_steps", 8)),
        ),
    )

    history.setdefault("browser_liked_urls", [])
    history.setdefault("browser_reposted_urls", [])
    history.setdefault("browser_commented_urls", [])

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🌐 Browser Engage starting: #{tag}; clips≤{clip_target}; "
            f"discovery scrolls≤{scroll_steps}; "
            f"Like≈{int(settings.get('engage_like_percent',75))}% · "
            f"Repost≈{int(settings.get('engage_repost_percent',75))}% · "
            f"Comment≈{int(settings.get('engage_comment_percent',10))}% · "
            f"Follow-author≈{int(settings.get('engage_follow_percent',25))}%."
        ),
    )

    clips_viewed = 0
    confirmed_actions = 0

    with sync_playwright() as p:
        update_account_metric(
            username,
            "add_history",
            value="🌐 Browser Engage: attaching to saved Chromium profile...",
        )

        context = _browser_launch(
            p,
            username,
            headed=True if manual else None,
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto(
                f"https://www.instagram.com/explore/tags/{tag}/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(1200)
            _browser_check_ready(page, context, username)

            links = _browser_collect_engage_links(
                page,
                username,
                clip_target,
                scroll_steps,
            )

            update_account_metric(
                username,
                "add_history",
                value=f"🌐 Browser Engage: collected {len(links)} candidate post/reel link(s).",
            )

            if not links:
                update_account_metric(
                    username,
                    "add_history",
                    value=f"⚠️ Browser Engage found no usable posts under #{tag}.",
                )
                return 0

            # Prefer unseen items, but still browse older candidates if all are
            # previously liked. Like history no longer causes the entire clip
            # to be skipped because Repost/Comment/Follow may still be eligible.
            ordered = list(links)
            random.shuffle(ordered)

            for href in ordered:
                if clips_viewed >= clip_target:
                    break

                if username in CONTROL_PAUSED_ACCOUNTS:
                    break

                safety = get_account_safety_state(username)
                if safety["active"]:
                    break

                clips_viewed += 1

                update_account_metric(
                    username,
                    "add_history",
                    value=f"▶️ Browser Engage clip {clips_viewed}/{clip_target}: {href}",
                )

                try:
                    page.goto(
                        href,
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    page.wait_for_timeout(random.randint(700, 1200))
                    _browser_check_ready(page, context, username)
                except Exception as exc:
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            f"⚠️ Engage clip navigation failed; moving on: "
                            f"{type(exc).__name__}: {str(exc)[:100]}"
                        ),
                    )
                    continue

                problem = _browser_engage_security_problem(
                    page,
                    username,
                )
                if problem:
                    break

                attempted_any = False

                if _browser_engage_percent(
                    settings,
                    "engage_like_percent",
                    75,
                ):
                    attempted_any = True
                    if _browser_try_like_engage(
                        page,
                        username,
                        href,
                        history,
                    ):
                        confirmed_actions += 1

                if _browser_engage_security_problem(page, username):
                    break

                if _browser_engage_percent(
                    settings,
                    "engage_repost_percent",
                    75,
                ):
                    attempted_any = True
                    if _browser_try_repost_engage(
                        page,
                        username,
                        href,
                        history,
                    ):
                        confirmed_actions += 1

                if _browser_engage_security_problem(page, username):
                    break

                if (
                    settings.get("enable_comments", False)
                    and _browser_engage_percent(
                        settings,
                        "engage_comment_percent",
                        10,
                    )
                ):
                    attempted_any = True
                    if _browser_try_comment_engage(
                        page,
                        username,
                        href,
                        history,
                    ):
                        confirmed_actions += 1

                if _browser_engage_security_problem(page, username):
                    break

                if (
                    settings.get("enable_follow", False)
                    and _browser_engage_percent(
                        settings,
                        "engage_follow_percent",
                        25,
                    )
                ):
                    attempted_any = True
                    if _browser_try_follow_author_engage(
                        page,
                        username,
                        href,
                    ):
                        confirmed_actions += 1

                if _browser_engage_security_problem(page, username):
                    break

                if not attempted_any:
                    update_account_metric(
                        username,
                        "add_history",
                        value="👀 Engage viewed this clip; no configured action selected.",
                    )

                # Continue browsing regardless of an individual Like/Repost/
                # Comment/Follow failure. There is no page refresh here.
                if clips_viewed < clip_target:
                    view_gap = random.randint(800, 1800)
                    update_account_metric(
                        username,
                        "add_history",
                        value=f"↘️ Engage advancing to next clip in ~{view_gap/1000:.1f}s.",
                    )
                    page.wait_for_timeout(view_gap)

        finally:
            try:
                _browser_refresh_saved_sessionid(
                    context,
                    username,
                )
            except Exception:
                pass
            _browser_close_context(context)

    history["browser_liked_urls"] = history["browser_liked_urls"][-5000:]
    history["browser_reposted_urls"] = history["browser_reposted_urls"][-5000:]
    history["browser_commented_urls"] = history["browser_commented_urls"][-5000:]

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🌐 Browser Engage finished: viewed {clips_viewed} clip(s), "
            f"confirmed actions={confirmed_actions}. "
            "Individual action failures did not terminate the pass."
        ),
    )
    return confirmed_actions




def _browser_select_post_media(username, history, folder_pool):
    """Fast media selection with no Ollama work."""
    history.setdefault("posted_ids", [])
    history.setdefault("browser_pending_uploads", [])

    now = time.time()
    pending = []
    pending_ids = set()

    for item in history["browser_pending_uploads"]:
        if not isinstance(item, dict):
            continue
        created = float(item.get("created_ts") or 0)
        if now - created < 24 * 3600:
            pending.append(item)
            pending_ids.add(str(item.get("folder_id") or ""))

    history["browser_pending_uploads"] = pending

    pool = [
        folder
        for folder in folder_pool
        if folder["id"] not in history["posted_ids"]
        and folder["id"] not in pending_ids
    ]
    random.shuffle(pool)

    valid_exts = {".jpg", ".jpeg", ".png", ".mp4"}
    checked = 0

    for folder in pool:
        checked += 1
        try:
            media_files = sorted(
                p
                for p in folder["path"].iterdir()
                if (
                    p.is_file()
                    and p.suffix.lower() in valid_exts
                    and p.stat().st_size > 0
                )
            )
        except OSError:
            continue

        if not media_files:
            continue

        videos = [p for p in media_files if p.suffix.lower() == ".mp4"]
        photos = [
            p for p in media_files
            if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        ]
        upload_files = [videos[0]] if videos else photos[:10]
        if not upload_files:
            continue

        text_context = ""
        try:
            sidecars = sorted(
                p
                for p in folder["path"].iterdir()
                if p.is_file()
                and p.suffix.lower() in {".txt", ".caption", ".md"}
            )[:3]
        except OSError:
            sidecars = []

        for txt in sidecars:
            try:
                body = txt.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).strip()
                if body:
                    text_context += (
                        ("\n" if text_context else "")
                        + body[:3000]
                    )
            except OSError:
                pass

        return {
            "folder": folder,
            "files": upload_files,
            "analysis_files": media_files,
            "text_context": text_context,
            "checked_candidates": checked,
            "available_candidates": len(pool),
        }

    return None


def _browser_post_context_blob(selection, analysis=None) -> str:
    parts = []

    if analysis:
        for key in ("context", "narration_notes", "summary", "description"):
            value = str(analysis.get(key) or "").strip()
            if value:
                parts.append(value)

    sidecar = _sanitize_post_context(
        selection.get("text_context", ""),
        selection["folder"].get("source", ""),
    ).strip()
    if sidecar:
        parts.append(sidecar)

    return " ".join(parts).lower()


def _browser_post_topic(selection, analysis=None) -> str:
    """
    Lightweight topic classification used only for choosing fallback hashtags.
    """
    blob = _browser_post_context_blob(selection, analysis)

    finance_terms = (
        "stock", "stocks", "trading", "trader", "forex", "xau",
        "gold", "futures", "market", "markets", "candlestick",
        "price action", "chart", "equities", "nasdaq", "s&p",
        "sp500", "dow", "options",
    )
    climbing_terms = (
        "climb", "climbing", "boulder", "bouldering", "rock climbing",
        "crag", "climbing gym", "route", "hold", "holds", "wall",
    )

    finance_score = sum(term in blob for term in finance_terms)
    climbing_score = sum(term in blob for term in climbing_terms)

    if finance_score > climbing_score and finance_score > 0:
        return "finance"
    if climbing_score > finance_score and climbing_score > 0:
        return "climbing"
    return "general"


def _browser_clean_hashtag(tag: str) -> str:
    tag = str(tag or "").strip()
    if not tag:
        return ""
    tag = tag.lstrip("#")
    tag = re.sub(r"[^A-Za-z0-9_]", "", tag)
    return f"#{tag}" if tag else ""


def _browser_preferred_post_hashtags(
    username,
    selection,
    analysis=None,
    *,
    ai_tags=None,
):
    """
    Return exactly five useful hashtags.

    Finance media gets the requested market-oriented set. Climbing media gets
    climbing tags. Generic low-signal tags such as #photo/#creator/#daily are
    deliberately excluded.
    """
    topic = _browser_post_topic(selection, analysis)
    settings = get_account_control_settings(username)

    if topic == "finance":
        preferred = [
            "#stocks",
            "#trading",
            "#forex",
            "#futures",
            "#fyp",
        ]
    elif topic == "climbing":
        preferred = [
            "#climbing",
            "#bouldering",
            "#rockclimbing",
            "#climbinggym",
            "#fyp",
        ]
    else:
        preferred = []

        # AI tags come first for non-classified media.
        for tag in ai_tags or []:
            cleaned = _browser_clean_hashtag(tag)
            if cleaned:
                preferred.append(cleaned)

        # Then use the account's configured target topics.
        for tag in _normalize_hashtag_list(
            settings.get("target_hashtags") or []
        ):
            cleaned = _browser_clean_hashtag(tag)
            if cleaned:
                preferred.append(cleaned)

        # Keep a couple of neutral discovery tags available as final fill.
        preferred.extend(["#fyp", "#reels"])

    blocked = {
        "#photo",
        "#creator",
        "#explore",
        "#daily",
        "#instagood",
    }

    result = []
    seen = set()

    for tag in preferred:
        cleaned = _browser_clean_hashtag(tag)
        key = cleaned.lower()

        if not cleaned or key in blocked or key in seen:
            continue

        seen.add(key)
        result.append(cleaned)

        if len(result) == 5:
            break

    # Ensure exactly five without falling back to the old generic spam set.
    filler = (
        ["#markets", "#investing", "#priceaction", "#fyp", "#finance"]
        if topic == "finance"
        else
        ["#climbinglife", "#climber", "#outdoors", "#fyp", "#reels"]
        if topic == "climbing"
        else
        ["#fyp", "#reels", "#video", "#content", "#social"]
    )

    for tag in filler:
        cleaned = _browser_clean_hashtag(tag)
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) == 5:
            break

    return result[:5]


def _browser_extract_caption_from_ai(raw: str) -> str:
    """
    Accept both the requested CAPTION:/HASHTAGS: format and ordinary model
    output so a harmless formatting miss does not force a fallback.
    """
    raw = _clean_ollama_output(raw or "").strip()
    if not raw:
        return ""

    match = re.search(
        r"CAPTION:\s*(.*?)(?:\n\s*HASHTAGS:|\Z)",
        raw,
        re.I | re.S,
    )
    if match:
        return match.group(1).strip()

    # Remove a trailing hashtag section if the model omitted CAPTION:.
    cleaned = re.split(
        r"\n\s*HASHTAGS?\s*:",
        raw,
        maxsplit=1,
        flags=re.I,
    )[0].strip()

    # Also strip standalone hashtag-only tail lines.
    lines = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if stripped and re.fullmatch(
            r"(?:#[A-Za-z0-9_]+\s*){2,}",
            stripped,
        ):
            continue
        if stripped:
            lines.append(stripped)

    cleaned = " ".join(lines).strip()
    cleaned = re.sub(r"^CAPTION\s*:\s*", "", cleaned, flags=re.I)
    return cleaned


def _browser_caption_readback(locator) -> str:
    try:
        tag = str(locator.evaluate("(el) => el.tagName.toLowerCase()"))
    except Exception:
        tag = ""

    if tag in {"textarea", "input"}:
        try:
            return str(locator.input_value(timeout=600) or "")
        except Exception:
            return ""

    try:
        return str(
            locator.evaluate(
                "(el) => (el.innerText || el.textContent || '')"
            )
            or ""
        )
    except Exception:
        return ""


def _browser_find_caption_editor(page):
    """
    Prefer caption controls inside the visible Create/Share dialog.
    """
    roots = []

    try:
        dialogs = page.locator("[role='dialog']")
        for i in range(dialogs.count() - 1, -1, -1):
            dialog = dialogs.nth(i)
            try:
                if dialog.is_visible(timeout=150):
                    roots.append(dialog)
                    break
            except Exception:
                pass
    except Exception:
        pass

    roots.append(page)

    selectors = (
        "textarea[aria-label*='caption' i]",
        "textarea[placeholder*='caption' i]",
        "[contenteditable='true'][aria-label*='caption' i]",
        "[contenteditable='true'][data-lexical-editor='true']",
        "div[role='textbox'][contenteditable='true']",
    )

    for root in roots:
        for selector in selectors:
            try:
                locators = root.locator(selector)
                for i in range(locators.count() - 1, -1, -1):
                    loc = locators.nth(i)
                    if loc.is_visible(timeout=250):
                        return loc
            except Exception:
                pass

    return None


def _browser_set_caption_verified(page, caption_box, full_caption: str) -> tuple[bool, str]:
    """
    Write caption text and verify Instagram's editor actually contains it.

    Share is not allowed to continue merely because .fill() returned without an
    exception.
    """
    expected = re.sub(r"\s+", " ", full_caption).strip()
    expected_tags = re.findall(r"#[A-Za-z0-9_]+", full_caption)

    def valid(readback: str) -> bool:
        actual = re.sub(r"\s+", " ", str(readback or "")).strip()
        if not actual:
            return False

        # Require a meaningful portion of caption plus all five hashtags.
        caption_head = re.sub(
            r"\s+#[A-Za-z0-9_]+.*$",
            "",
            expected,
        ).strip()
        head_probe = caption_head[: min(40, len(caption_head))].strip()

        if head_probe and head_probe.lower() not in actual.lower():
            return False

        return all(tag.lower() in actual.lower() for tag in expected_tags)

    attempts = []

    # 1) Normal Playwright fill.
    try:
        caption_box.fill(full_caption)
        page.wait_for_timeout(350)
        readback = _browser_caption_readback(caption_box)
        attempts.append(("fill", readback))
        if valid(readback):
            return True, readback
    except Exception:
        pass

    # 2) Real keyboard insertion into the focused editor.
    try:
        caption_box.click(timeout=2000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(full_caption)
        page.wait_for_timeout(450)
        readback = _browser_caption_readback(caption_box)
        attempts.append(("keyboard", readback))
        if valid(readback):
            return True, readback
    except Exception:
        pass

    # 3) Native setter + input event for React/contenteditable variants.
    try:
        caption_box.evaluate(
            """
            (el, value) => {
              el.focus();

              if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                const proto = el.tagName === 'TEXTAREA'
                  ? HTMLTextAreaElement.prototype
                  : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(
                  proto,
                  'value'
                ).set;
                setter.call(el, value);
              } else {
                el.textContent = value;
              }

              el.dispatchEvent(
                new InputEvent(
                  'input',
                  {
                    bubbles: true,
                    inputType: 'insertText',
                    data: value
                  }
                )
              );
              el.dispatchEvent(new Event('change', {bubbles:true}));
            }
            """,
            full_caption,
        )
        page.wait_for_timeout(500)
        readback = _browser_caption_readback(caption_box)
        attempts.append(("native", readback))
        if valid(readback):
            return True, readback
    except Exception:
        pass

    last = attempts[-1][1] if attempts else ""
    return False, last

def _browser_fallback_caption(
    username,
    selection,
    reason="AI unavailable",
    analysis=None,
):
    """
    Guaranteed fallback so AI failure does not cancel a valid upload.

    Uses topic-aware hashtags instead of the old generic
    #photo/#creator/#explore/#daily/#instagood set.
    """
    settings = get_account_control_settings(username)

    clean_sidecar = _sanitize_post_context(
        selection.get("text_context", ""),
        selection["folder"].get("source", ""),
    ).strip()

    if clean_sidecar:
        caption = _clip_chars(
            clean_sidecar.splitlines()[0].strip(),
            260,
        )
    else:
        topic = _browser_post_topic(selection, analysis)
        if topic == "finance":
            caption = "Watching this setup develop."
        elif topic == "climbing":
            caption = "One move at a time."
        else:
            caption = "Worth a closer look."

    tags = _browser_preferred_post_hashtags(
        username,
        selection,
        analysis,
    )
    tag_line = " ".join(tags)

    total_limit = int(settings["caption_char_limit"])
    caption = _clip_chars(
        caption,
        max(20, total_limit - len(tag_line) - 2),
    )

    return {
        "caption": f"{caption}\n\n{tag_line}".strip(),
        "used_fallback": True,
        "reason": str(reason or "AI unavailable"),
        "analysis": analysis,
        "hashtags": tags,
    }



def _browser_generate_post_caption(username, selection):
    """
    Slow AI phase after media is already attached in Chromium.

    Uses a small vision-frame cap and one combined text-model call for both
    caption and hashtags. Any failure falls back instead of cancelling upload.
    """
    settings = get_account_control_settings(username)

    try:
        requested_frames = int(settings.get("video_frames", 5))
    except Exception:
        requested_frames = 5

    frame_count = max(
        3,
        min(requested_frames, BROWSER_POST_VISION_FRAMES),
    )

    try:
        analysis = analyze_media_for_caption(
            selection["analysis_files"],
            sidecar_text=selection.get("text_context", ""),
            source_account=selection["folder"].get("source", ""),
            frame_count=frame_count,
            require_video_vision=settings["require_video_vision"],
        )
    except Exception as exc:
        return _browser_fallback_caption(
            username,
            selection,
            reason=f"vision analysis error: {type(exc).__name__}",
        )

    if analysis.get("vision_required_failed"):
        return _browser_fallback_caption(
            username,
            selection,
            reason=(
                "required video vision unavailable "
                f"(frames={analysis.get('frames_analyzed', 0)})"
            ),
            analysis=analysis,
        )

    semantic_context = str(analysis.get("context") or "").strip()
    perspective = str(analysis.get("perspective") or "UNKNOWN").upper()

    if perspective in {"POV_FIRST_PERSON", "SELFIE_VLOG"}:
        perspective_rule = (
            "Use natural first-person narration as the fictional filmer/account "
            "voice without identifying any real person."
        )
    elif perspective == "THIRD_PERSON":
        perspective_rule = (
            "Use the account voice reacting to the visible scene without "
            "claiming to literally be an identifiable person shown."
        )
    else:
        perspective_rule = (
            "Use a natural account voice without inventing identities or events."
        )

    extra = str(settings.get("caption_prompt", "") or "").strip()
    persona = str(
        settings.get("persona_prompt", RAGE_BAIT_PERSONA) or RAGE_BAIT_PERSONA
    ).strip()
    char_limit = int(settings["caption_char_limit"])
    preferred_tags = _browser_preferred_post_hashtags(
        username,
        selection,
        analysis,
    )
    preferred_tag_line = " ".join(preferred_tags)

    prompt = f"""
Write ONE Instagram caption and EXACTLY 5 relevant hashtags for this exact media.

MEDIA ANALYSIS:
{semantic_context}

PERSPECTIVE:
{perspective}
{perspective_rule}

ACCOUNT-SPECIFIC CAPTION INSTRUCTIONS:
{extra or "(none)"}

PREFERRED ACCOUNT HASHTAGS WHEN RELEVANT:
{preferred_tag_line}

Return exactly:
CAPTION: <caption text>
HASHTAGS: #tag1 #tag2 #tag3 #tag4 #tag5

Rules:
- Base the caption on the supplied visual analysis.
- Never mention source usernames, repost metadata, archive folders, or filenames.
- Do not invent a location, identity, profession, event, or stock/trading topic.
- Keep the caption under {char_limit} characters before hashtags.
- Complete natural sentences.
- Prefer the supplied account hashtags when they fit the visual subject.
- Avoid generic low-signal tags such as #photo #creator #explore #daily #instagood.
""".strip()

    try:
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": persona},
                {"role": "user", "content": prompt},
            ],
            options={
                "temperature": 0.5,
                "top_p": 0.85,
                "num_predict": 220,
            },
        )

        raw = _clean_ollama_output(
            response.get("message", {}).get("content", "")
            if isinstance(response, dict)
            else getattr(
                getattr(response, "message", None),
                "content",
                "",
            )
        ).strip()
    except Exception as exc:
        return _browser_fallback_caption(
            username,
            selection,
            reason=f"caption model error: {type(exc).__name__}",
            analysis=analysis,
        )

    caption = _browser_extract_caption_from_ai(raw)

    hashtag_match = re.search(
        r"HASHTAGS:\s*(.*)",
        raw,
        re.I | re.S,
    )
    hashtag_source = hashtag_match.group(1) if hashtag_match else raw

    tags = []
    for token in re.findall(r"#[A-Za-z0-9_]+", hashtag_source):
        if token not in tags:
            tags.append(token)
        if len(tags) == 5:
            break

    tags = _browser_preferred_post_hashtags(
        username,
        selection,
        analysis,
        ai_tags=tags,
    )

    if not caption:
        return _browser_fallback_caption(
            username,
            selection,
            reason="caption model returned no usable caption",
            analysis=analysis,
        )

    tag_line = " ".join(tags[:5])
    caption = _clip_chars(
        caption,
        max(20, char_limit - len(tag_line) - 2),
    )

    return {
        "caption": f"{caption}\n\n{tag_line}".strip(),
        "used_fallback": False,
        "reason": "",
        "analysis": analysis,
        "hashtags": tags[:5],
    }


def _browser_prepare_post(username, history, folder_pool):
    """Compatibility wrapper; Browser Post now uses split attach-first flow."""
    selection = _browser_select_post_media(
        username,
        history,
        folder_pool,
    )
    if not selection:
        return None

    generated = _browser_generate_post_caption(
        username,
        selection,
    )

    return {
        "folder": selection["folder"],
        "files": selection["files"],
        "caption": generated["caption"],
    }



def _browser_explicit_share_confirmation(page) -> tuple[bool, str]:
    """Require Instagram's own explicit successful-share message."""
    success_phrases = (
        "your post has been shared",
        "your reel has been shared",
        "post shared",
        "reel shared",
        "shared successfully",
    )
    try:
        body = re.sub(r"\\s+", " ", page.locator("body").inner_text(timeout=1200) or "").lower()
    except Exception:
        body = ""
    for phrase in success_phrases:
        if phrase in body:
            return True, phrase
    return False, ""

def _browser_upload_failure_signal(page) -> str:
    """Detect obvious visible share/upload failure messages."""
    failure_phrases = (
        "couldn't share",
        "could not share",
        "upload failed",
        "failed to upload",
        "your post could not be shared",
        "your reel could not be shared",
    )
    try:
        body = re.sub(r"\\s+", " ", page.locator("body").inner_text(timeout=1200) or "").lower()
    except Exception:
        return ""
    for phrase in failure_phrases:
        if phrase in body:
            return phrase
    return ""

def _browser_find_new_permalink_after_share(page, username: str, before_links: set[str], timeout_seconds: int = 60) -> str:
    """Resolve a permalink only after Instagram explicitly confirmed the share."""
    deadline = time.time() + max(10, int(timeout_seconds))
    while time.time() < deadline:
        try:
            page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1200)
        except Exception:
            pass
        current = _browser_collect_profile_media_links(page, username)
        new_links = current - before_links
        if new_links:
            return sorted(new_links)[-1]
        page.wait_for_timeout(2500)
    return ""

def _browser_post(username, history, folder_pool, manual=False) -> bool:
    settings = get_account_control_settings(username)

    if not manual and not settings.get("enable_posts", False):
        return False

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🧰 Browser Post starting ({'manual' if manual else 'Auto'}); "
            f"media folders discovered={len(folder_pool)}"
        ),
    )

    update_account_metric(
        username,
        "add_history",
        value="🔐 Browser Post: checking live Instagram Web login before media selection...",
    )

    preflight_started = time.time()
    _browser_preflight_login(
        username,
        headed=True if manual else False,
    )
    preflight_elapsed = int(time.time() - preflight_started)

    update_account_metric(
        username,
        "add_history",
        value=f"✅ Browser Post login preflight passed in {preflight_elapsed}s.",
    )

    if not folder_pool:
        update_account_metric(
            username,
            "add_history",
            value=f"⚠️ Browser Post: no media folders found under {get_media_root()}.",
        )
        return False

    select_started = time.time()
    selection = _browser_select_post_media(
        username,
        history,
        folder_pool,
    )
    select_elapsed = time.time() - select_started

    if not selection:
        update_account_metric(
            username,
            "add_history",
            value=(
                "⚠️ Browser Post found no unused uploadable "
                f".mp4/.jpg/.jpeg/.png media in {select_elapsed:.1f}s."
            ),
        )
        return False

    selected = selection["folder"]
    files = selection["files"]

    update_account_metric(
        username,
        "add_history",
        value=(
            f"✅ Browser Post selected media in {select_elapsed:.1f}s: "
            f"{', '.join(p.name for p in files)} from {selected['path']}. "
            "Opening Instagram BEFORE AI caption generation."
        ),
    )

    if not wait_for_write_slot(
        username,
        "upload",
        max_wait=180,
        fail_fast=bool(manual),
    ):
        update_account_metric(
            username,
            "add_history",
            value="⏳ Browser Post deferred by pacing/safety state.",
        )
        return False

    ai_executor = None
    ai_future = None
    full_caption = None

    with sync_playwright() as p:
        update_account_metric(
            username,
            "add_history",
            value="🌐 Browser Post: attaching to live Chromium session...",
        )

        context = _browser_launch(
            p,
            username,
            headed=True if manual else None,
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            update_account_metric(
                username,
                "add_history",
                value="🔎 Browser Post: snapshotting current profile posts before upload...",
            )
            before_links = _browser_collect_profile_media_links(
                page,
                username,
            )
            _browser_check_ready(page, context, username)

            update_account_metric(
                username,
                "add_history",
                value=(
                    f"🔎 Browser Post: baseline contains "
                    f"{len(before_links)} profile post/reel URL(s)."
                ),
            )

            page.goto(
                "https://www.instagram.com/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(700)
            _browser_check_ready(page, context, username)

            update_account_metric(
                username,
                "add_history",
                value="➕ Browser Post: opening Instagram Create composer...",
            )

            opened = False
            for label in ("New post", "Create"):
                svg = _browser_find_svg_action(page, (label,))
                if svg is not None:
                    _browser_clickable_from_svg(svg).click(timeout=5000)
                    opened = True
                    break

            if not opened:
                opened = _browser_click_text_button(page, r"^Create$")

            if not opened:
                raise RuntimeError(
                    "Instagram Web Create/Post control was not found."
                )

            page.wait_for_timeout(700)

            try:
                menu_post = page.get_by_role(
                    "menuitem",
                    name=re.compile(r"^Post$", re.I),
                ).first
                if menu_post.count() and menu_post.is_visible(timeout=500):
                    menu_post.click(timeout=4000)
                    page.wait_for_timeout(500)
            except Exception:
                pass

            update_account_metric(
                username,
                "add_history",
                value="📎 Browser Post: waiting for Instagram file input...",
            )

            file_input = page.locator("input[type='file']").first
            deadline = time.time() + 12

            while time.time() < deadline and not file_input.count():
                page.wait_for_timeout(350)
                file_input = page.locator("input[type='file']").first

            if not file_input.count():
                raise RuntimeError(
                    "Instagram Web post composer did not expose a file input."
                )

            file_input.set_input_files(
                [str(path) for path in files]
            )

            update_account_metric(
                username,
                "add_history",
                value=(
                    f"⬆️ Browser Post attached "
                    f"{', '.join(p.name for p in files)}. "
                    "Instagram is now processing the media."
                ),
            )

            ai_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"ig-caption-{username}",
            )
            ai_future = ai_executor.submit(
                _browser_generate_post_caption,
                username,
                selection,
            )
            ai_started = time.time()

            update_account_metric(
                username,
                "add_history",
                value=(
                    "🧠 Browser Post AI started AFTER media attachment. "
                    f"Vision uses up to {BROWSER_POST_VISION_FRAMES} frame(s); "
                    "caption + hashtags use one text-model call."
                ),
            )

            page.wait_for_timeout(1400)

            for next_index in range(3):
                if _browser_click_text_button(
                    page,
                    r"^Next$",
                    timeout=5000,
                ):
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            f"➡️ Browser Post advanced composer step "
                            f"{next_index + 1} while AI runs."
                        ),
                    )
                    page.wait_for_timeout(900)
                else:
                    break

            update_account_metric(
                username,
                "add_history",
                value=(
                    "✍️ Browser Post: locating caption editor; "
                    "media is already loaded in Chromium."
                ),
            )

            caption_box = _browser_find_caption_editor(page)

            if caption_box is None:
                raise RuntimeError(
                    "Instagram Web caption editor was not found in the visible composer."
                )

            generated = None
            last_notice = 0.0

            while generated is None:
                elapsed = time.time() - ai_started
                remaining = BROWSER_POST_AI_TIMEOUT_SECONDS - elapsed

                if remaining <= 0:
                    generated = _browser_fallback_caption(
                        username,
                        selection,
                        reason=(
                            f"AI exceeded "
                            f"{BROWSER_POST_AI_TIMEOUT_SECONDS}s timeout"
                        ),
                    )
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            "⚠️ Browser Post AI timed out; continuing with "
                            "fallback caption instead of cancelling upload."
                        ),
                    )
                    break

                try:
                    generated = ai_future.result(
                        timeout=min(12, max(1, remaining))
                    )
                except FutureTimeoutError:
                    if time.time() - last_notice >= 15:
                        update_account_metric(
                            username,
                            "add_history",
                            value=(
                                f"🧠 Browser Post AI still working "
                                f"({int(elapsed)}s); media is already attached "
                                "and composer is ready."
                            ),
                        )
                        last_notice = time.time()
                except Exception as exc:
                    generated = _browser_fallback_caption(
                        username,
                        selection,
                        reason=f"AI worker error: {type(exc).__name__}",
                    )

            full_caption = generated["caption"]

            if generated.get("used_fallback"):
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "⚠️ Browser Post using fallback caption: "
                        f"{generated.get('reason', 'AI unavailable')}."
                    ),
                )
            else:
                ai_elapsed = int(time.time() - ai_started)
                update_account_metric(
                    username,
                    "add_history",
                    value=f"✅ Browser Post AI caption ready in {ai_elapsed}s.",
                )

            caption_ok, caption_readback = _browser_set_caption_verified(
                page,
                caption_box,
                full_caption,
            )

            if not caption_ok:
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "❌ Browser Post caption verification failed; Share was "
                        "not clicked. Instagram's visible caption editor did not "
                        "retain the expected caption + hashtags."
                    ),
                )
                raise RuntimeError(
                    "Instagram caption editor did not retain the caption; "
                    "upload left in composer instead of sharing without text."
                )

            post_tags = re.findall(
                r"#[A-Za-z0-9_]+",
                full_caption,
            )
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"✍️ Browser Post caption verified in editor "
                    f"({len(caption_readback)} chars); hashtags="
                    f"{' '.join(post_tags[:5])}."
                ),
            )

            page.wait_for_timeout(500)

            if not _browser_click_text_button(
                page,
                r"^(Share|Publish)$",
                timeout=6000,
            ):
                raise RuntimeError(
                    "Instagram Web Share button was not found."
                )

            update_account_metric(
                username,
                "add_history",
                value=(
                    "⬆️ Browser Post submitted; waiting for Instagram's own "
                    "explicit share confirmation. Existing profile URLs are "
                    "ignored until that confirmation appears."
                ),
            )

            share_confirmed = False
            share_signal = ""
            deadline = time.time() + 90
            last_notice = 0.0

            while time.time() < deadline:
                page.wait_for_timeout(1500)

                problem = _browser_page_problem(page)
                if problem:
                    raise RuntimeError(
                        f"Instagram Web upload interrupted: {problem}"
                    )

                failure = _browser_upload_failure_signal(page)
                if failure:
                    update_account_metric(
                        username,
                        "add_history",
                        value=f"❌ Instagram reported upload/share failure: {failure}",
                    )
                    break

                share_confirmed, share_signal = _browser_explicit_share_confirmation(page)
                if share_confirmed:
                    break

                if time.time() - last_notice >= 15:
                    remaining = max(0, int(deadline - time.time()))
                    update_account_metric(
                        username,
                        "add_history",
                        value=f"⏳ Waiting for Instagram share confirmation: {remaining}s remaining.",
                    )
                    last_notice = time.time()

            if not share_confirmed:
                history.setdefault("browser_pending_uploads", []).append(
                    {
                        "folder_id": selected["id"],
                        "created_ts": time.time(),
                        "files": [str(p) for p in files],
                    }
                )
                history["browser_pending_uploads"] = history["browser_pending_uploads"][-30:]

                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "⚠️ Post was NOT confirmed by Instagram. It was not counted, "
                        "and no existing or old post URL was accepted as proof. "
                        "The media folder is held for 24h."
                    ),
                )
                return False

            update_account_metric(
                username,
                "add_history",
                value=(
                    f"✅ Instagram explicitly confirmed the share ({share_signal}). "
                    "Resolving the new permalink..."
                ),
            )

            final_link = _browser_find_new_permalink_after_share(
                page,
                username,
                before_links,
                timeout_seconds=60,
            )

            record_write(username, "upload")

            if selected["id"] not in history["posted_ids"]:
                history["posted_ids"].append(selected["id"])

            update_account_metric(username, "total_posts", increment=1)

            record_recent_post(
                username=username,
                folder_path=selected["path"],
                caption=full_caption,
                media_id="browser",
                permalink=final_link,
            )

            if final_link:
                update_account_metric(
                    username,
                    "add_history",
                    value=f"✅ Browser Post confirmed by Instagram: {final_link}",
                )
            else:
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "✅ Browser Post was explicitly confirmed by Instagram, "
                        "but the new permalink has not resolved yet. No old post URL was attached."
                    ),
                )

            return True

        except Exception as exc:
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"❌ Browser Post failed: {type(exc).__name__}: "
                    f"{str(exc)[:220]}"
                ),
            )
            raise

        finally:
            if ai_executor is not None:
                try:
                    ai_executor.shutdown(
                        wait=False,
                        cancel_futures=True,
                    )
                except Exception:
                    pass

            try:
                _browser_refresh_saved_sessionid(
                    context,
                    username,
                )
            except Exception:
                pass

            _browser_close_context(context)




def _runtime_pace_mode(username: str) -> str:
    if float(BURST_UNTIL.get(username, 0.0) or 0.0) > time.time():
        return "burst"
    return str(
        get_account_control_settings(username).get("pace_mode", "normal")
    ).strip().lower()


def _control_set_pace_mode(username: str, mode: str):
    username = str(username or "").strip().lstrip("@")
    mode = str(mode or "").strip().lower()

    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")
    if mode not in {"normal", "overnight"}:
        raise ValueError("Pace mode must be normal or overnight.")

    settings = update_account_control_settings(
        username,
        {"pace_mode": mode},
    )

    if mode == "normal":
        BURST_UNTIL.pop(username, None)

    if username not in CONTROL_PAUSED_ACCOUNTS:
        CONTROL_FORCE_RUN.add(username)

    update_account_metric(
        username,
        "add_history",
        value=(
            f"⏱️ Pace mode set to {mode.upper()}. "
            + (
                "Auto will use multi-minute active sessions with periodic rests."
                if mode == "overnight"
                else "Auto returned to normal single-pass scheduling."
            )
        ),
    )
    return {"ok": True, "pace_mode": settings["pace_mode"]}


def _control_burst_now(username: str):
    username = str(username or "").strip().lstrip("@")

    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")

    if username in BROWSER_LOGIN_IN_PROGRESS:
        raise RuntimeError("Finish Browser Login before starting a burst.")

    if username in BROWSER_NATIVE_ACCOUNTS and not _browser_live_port_open(username):
        _mark_browser_login_needed(
            username,
            "the live Chromium window is closed",
        )
        raise RuntimeError("Live Chromium is closed. Use Quick Login or Browser Login first.")

    state = get_account_safety_state(username)
    if state["active"]:
        raise RuntimeError(
            f"Safety Backoff is active until {state['until']}: "
            f"{state['reason'][:140]}"
        )

    duration = random.randint(
        BURST_SESSION_MIN_SECONDS,
        BURST_SESSION_MAX_SECONDS,
    )
    BURST_UNTIL[username] = time.time() + duration

    CONTROL_PAUSED_ACCOUNTS.discard(username)
    CONTROL_FORCE_RUN.add(username)

    update_account_metric(
        username,
        "status",
        status="Burst Session Requested",
    )
    update_account_metric(
        username,
        "add_history",
        value=(
            f"⚡ Burst Now requested for ~{duration//60}m "
            f"{duration%60:02d}s of active work. Rolling write budget, "
            "50/day follow cap, and security stops remain active."
        ),
    )

    return {
        "ok": True,
        "burst_seconds": duration,
        "burst_until": BURST_UNTIL[username],
    }


def _browser_readonly_dm_check(username: str, context, page) -> bool:
    settings = get_account_control_settings(username)
    if not settings.get("enable_dms", False):
        return False

    now = time.time()
    due = float(NEXT_BROWSER_DM_CHECK.get(username, 0.0) or 0.0)
    if due and now < due:
        return False

    NEXT_BROWSER_DM_CHECK[username] = now + random.randint(
        BROWSER_DM_CHECK_MIN_SECONDS,
        BROWSER_DM_CHECK_MAX_SECONDS,
    )
    LAST_BROWSER_DM_CHECK[username] = now

    update_account_metric(
        username,
        "add_history",
        value="📥 Browser Mode: performing a low-frequency read-only DM inbox check.",
    )

    try:
        page.goto(
            "https://www.instagram.com/direct/inbox/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(1200)
        _browser_check_ready(page, context, username)

        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=1200) or "",
        ).lower()

        hints = []
        for phrase in ("message requests", "requests", "new message", "unread"):
            if phrase in body and phrase not in hints:
                hints.append(phrase)

        update_account_metric(
            username,
            "add_history",
            value=(
                "📥 Browser DM check complete"
                + (f"; visible hints: {', '.join(hints[:3])}" if hints else "; no obvious unread/request text detected")
                + ". Browser Mode does not auto-reply to DMs in this build."
            ),
        )
        return True
    except Exception as exc:
        update_account_metric(
            username,
            "add_history",
            value=f"⚠️ Browser DM read-only check skipped: {type(exc).__name__}: {str(exc)[:120]}",
        )
        return False


def _browser_readonly_comment_check(username: str, context, page) -> bool:
    settings = get_account_control_settings(username)
    if not settings.get("enable_comments", False):
        return False

    now = time.time()
    due = float(NEXT_BROWSER_COMMENT_CHECK.get(username, 0.0) or 0.0)
    if due and now < due:
        return False

    NEXT_BROWSER_COMMENT_CHECK[username] = now + random.randint(
        BROWSER_COMMENT_CHECK_MIN_SECONDS,
        BROWSER_COMMENT_CHECK_MAX_SECONDS,
    )
    LAST_BROWSER_COMMENT_CHECK[username] = now

    update_account_metric(
        username,
        "add_history",
        value="💬 Browser Mode: performing a low-frequency read-only comment check.",
    )

    try:
        page.goto(
            f"https://www.instagram.com/{username}/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(1000)
        _browser_check_ready(page, context, username)

        links = page.locator(
            "a[href*='/p/'], a[href*='/reel/']"
        ).evaluate_all(
            """els => els.map(e => e.href || e.getAttribute('href') || '').filter(Boolean)"""
        )
        if not links:
            update_account_metric(
                username,
                "add_history",
                value="💬 Browser comment check complete; no recent post/reel link was visible.",
            )
            return True

        page.goto(
            str(links[0]).split("?", 1)[0],
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(1000)
        _browser_check_ready(page, context, username)

        body = re.sub(
            r"\s+",
            " ",
            page.locator("body").inner_text(timeout=1200) or "",
        )

        comment_match = re.search(
            r"View all\s+([0-9,]+)\s+comments?",
            body,
            re.I,
        )
        summary = (
            f"visible comment count hint={comment_match.group(1)}"
            if comment_match
            else "no comment-count hint detected"
        )

        update_account_metric(
            username,
            "add_history",
            value=(
                f"💬 Browser comment check complete; {summary}. "
                "Browser Mode does not auto-reply to comments in this build."
            ),
        )
        return True
    except Exception as exc:
        update_account_metric(
            username,
            "add_history",
            value=f"⚠️ Browser comment read-only check skipped: {type(exc).__name__}: {str(exc)[:120]}",
        )
        return False


def _browser_maybe_run_readonly_listeners(username: str) -> None:
    settings = get_account_control_settings(username)
    if not (
        settings.get("enable_dms", False)
        or settings.get("enable_comments", False)
    ):
        return

    if not _browser_live_port_open(username):
        return

    with sync_playwright() as p:
        context = _browser_launch(p, username, headed=None)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            # At most one listener navigation per active-session checkpoint.
            checks = []
            if settings.get("enable_dms", False):
                checks.append("dms")
            if settings.get("enable_comments", False):
                checks.append("comments")
            random.shuffle(checks)

            for check in checks:
                if check == "dms" and _browser_readonly_dm_check(
                    username, context, page
                ):
                    break
                if check == "comments" and _browser_readonly_comment_check(
                    username, context, page
                ):
                    break
        finally:
            _browser_close_context(context)


def _browser_auto_choices(username: str, settings: dict, folder_pool) -> list[str]:
    choices = []

    if settings.get("enable_follow", False):
        choices.append("networking")
    if settings.get("enable_engage", False):
        choices.append("hashtags")

    if settings.get("enable_posts", False) and folder_pool:
        # Do not let a long upload cooldown stall an active browsing session.
        if not _shared_write_block_reason(username, "upload"):
            choices.append("repost")

    return choices


def _run_browser_active_session(
    username: str,
    history: dict,
    conf: dict,
    folder_pool,
    settings: dict,
    mode: str,
) -> None:
    """
    Multi-minute active session with short navigation gaps.

    The rolling write budget remains the hard ceiling. Security/restriction
    warnings immediately stop the session.
    """
    if mode == "burst":
        duration = random.randint(
            BURST_SESSION_MIN_SECONDS,
            BURST_SESSION_MAX_SECONDS,
        )
    else:
        duration = random.randint(
            ACTIVE_SESSION_MIN_SECONDS,
            ACTIVE_SESSION_MAX_SECONDS,
        )

    hard_deadline = time.monotonic() + duration
    cycles = 0
    post_used = False
    max_passes = max(
        1,
        min(
            20,
            int(settings.get("active_session_max_passes", 8)),
        ),
    )

    update_account_metric(
        username,
        "add_history",
        value=(
            f"⚡ {mode.title()} active session started for up to ~{duration//60}m "
            f"{duration%60:02d}s; short browsing gaps "
            f"{(BURST_BROWSE_GAP_MIN_SECONDS if mode == 'burst' else ACTIVE_BROWSE_GAP_MIN_SECONDS)}-"
            f"{(BURST_BROWSE_GAP_MAX_SECONDS if mode == 'burst' else ACTIVE_BROWSE_GAP_MAX_SECONDS)}s; "
            f"max passes={max_passes}."
        ),
    )

    # Low-frequency listener checkpoint near the beginning.
    _browser_maybe_run_readonly_listeners(username)

    while time.monotonic() < hard_deadline:
        if username in CONTROL_PAUSED_ACCOUNTS:
            break

        safety = get_account_safety_state(username)
        if safety["active"]:
            break

        settings = get_account_control_settings(username, conf)
        choices = _browser_auto_choices(username, settings, folder_pool)

        if post_used and "repost" in choices:
            choices.remove("repost")

        if not choices:
            update_account_metric(
                username,
                "add_history",
                value=(
                    "⏳ Active session has no currently eligible outward action; "
                    "ending this session and entering its scheduled rest."
                ),
            )
            break

        cycle_index = int(
            history.get("browser_burst_cycle_index", 0) or 0
        )
        task = choices[cycle_index % len(choices)]
        history["browser_burst_cycle_index"] = cycle_index + 1

        update_account_metric(
            username,
            "add_history",
            value=f"⚡ Active session step {cycles + 1}: {task}",
        )

        try:
            if task == "networking":
                _browser_follow_network(
                    username,
                    history,
                    conf,
                    manual=False,
                )
            elif task == "hashtags":
                _browser_engage_hashtag(
                    username,
                    history,
                    conf,
                    manual=False,
                )
            elif task == "repost":
                _browser_post(
                    username,
                    history,
                    folder_pool,
                    manual=False,
                )
                post_used = True
        except RuntimeError as exc:
            # Security/restriction problems are already classified elsewhere.
            update_account_metric(
                username,
                "add_history",
                value=f"⚠️ Active session step stopped: {str(exc)[:160]}",
            )
            if get_account_safety_state(username)["active"]:
                break

        cycles += 1

        if cycles >= max_passes:
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"⚡ Active session reached configured pass limit "
                    f"({cycles}/{max_passes}); entering rest."
                ),
            )
            break

        if time.monotonic() >= hard_deadline:
            break

        if mode == "burst":
            gap = random.randint(
                BURST_BROWSE_GAP_MIN_SECONDS,
                BURST_BROWSE_GAP_MAX_SECONDS,
            )
        else:
            gap = random.randint(
                ACTIVE_BROWSE_GAP_MIN_SECONDS,
                ACTIVE_BROWSE_GAP_MAX_SECONDS,
            )
        update_account_metric(
            username,
            "add_history",
            value=f"… Active-session browsing gap ~{gap}s.",
        )
        time.sleep(gap)

        # Listener checks are deliberately sparse and do nothing when not due.
        if cycles % 3 == 0:
            _browser_maybe_run_readonly_listeners(username)

    update_account_metric(
        username,
        "add_history",
        value=f"⚡ {mode.title()} active session finished after {cycles} step(s).",
    )

def run_browser_profile_workflow(username, conf, folder_pool):
    ACCOUNT_COOLDOWNS.pop(username, None)

    if username in BROWSER_LOGIN_IN_PROGRESS:
        update_account_metric(
            username,
            "status",
            status="Browser Login In Progress",
        )
        update_account_metric(
            username,
            "add_history",
            value=(
                "⏸️ Browser Mode workflow skipped because Browser Login "
                "is still waiting for manual completion."
            ),
        )
        return "paused"

    if not _browser_live_port_open(username):
        _mark_browser_login_needed(username, "the live Chromium process is no longer running")
        return "disconnected"

    forced_task = NEXT_TASK_OVERRIDE.pop(username, None)

    if username in CONTROL_PAUSED_ACCOUNTS and not forced_task:
        update_account_metric(
            username,
            "status",
            status="Browser Mode / Auto Off",
        )
        return "paused"

    if not _browser_profile_saved(username):
        update_account_metric(
            username,
            "status",
            status="Browser Login Needed",
        )
        return "disconnected"

    settings = get_account_control_settings(username, conf)
    update_account_metric(
        username,
        "status",
        status="Browser Mode / Active",
    )

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🚀 Browser Mode workflow started"
            + (f": manual {forced_task}" if forced_task else ": Auto")
        ),
    )

    history = load_json(
        conf["history_file"],
        {
            "posted_ids": [],
            "replied_dms": [],
            "replied_comments": [],
            "liked_medias": [],
            "commented_medias": [],
            "followed_users": [],
            "blocked_or_missing": [],
            "competitor_pool": [],
            "followers_next_max_id": None,
            "following_next_max_id": None,
            "dm_reply_memory": {},
            "browser_liked_urls": [],
            "browser_saved_urls": [],
            "browser_pending_uploads": [],
        },
    )

    try:
        if forced_task == "networking":
            _browser_follow_network(
                username,
                history,
                conf,
                manual=True,
            )

        elif forced_task == "hashtags":
            _browser_engage_hashtag(
                username,
                history,
                conf,
                manual=True,
            )

        elif forced_task == "reels_demo":
            _browser_reels_demo(
                username,
                history,
                conf,
            )

        elif forced_task == "repost":
            _browser_post(
                username,
                history,
                folder_pool,
                manual=True,
            )

        elif forced_task == "comments":
            update_account_metric(
                username,
                "add_history",
                value=(
                    "ℹ️ Browser Mode comment replies are not enabled yet; "
                    "Follow, Engage, and Post are available."
                ),
            )

        else:
            pace_mode = _runtime_pace_mode(username)

            if pace_mode in {"overnight", "burst"}:
                _run_browser_active_session(
                    username,
                    history,
                    conf,
                    folder_pool,
                    settings,
                    pace_mode,
                )
            else:
                choices = _browser_auto_choices(
                    username,
                    settings,
                    folder_pool,
                )

                if not choices:
                    _browser_maybe_run_readonly_listeners(username)
                    update_account_metric(
                        username,
                        "add_history",
                        value="ℹ️ Browser Mode Auto pass has no currently eligible outward feature.",
                    )
                    return "ran"

                cycle_index = int(
                    history.get("browser_outward_cycle_index", 0) or 0
                )
                task = choices[cycle_index % len(choices)]
                history["browser_outward_cycle_index"] = cycle_index + 1

                update_account_metric(
                    username,
                    "add_history",
                    value=f"🔄 Browser Mode Auto selected: {task}",
                )

                if task == "networking":
                    _browser_follow_network(
                        username,
                        history,
                        conf,
                        manual=False,
                    )
                elif task == "hashtags":
                    _browser_engage_hashtag(
                        username,
                        history,
                        conf,
                        manual=False,
                    )
                elif task == "repost":
                    _browser_post(
                        username,
                        history,
                        folder_pool,
                        manual=False,
                    )

                _browser_maybe_run_readonly_listeners(username)

    finally:
        save_json(conf["history_file"], history)

        update_account_metric(
            username,
            "add_history",
            value=(
                f"🏁 Browser Mode workflow finished"
                + (f": manual {forced_task}" if forced_task else ": Auto")
            ),
        )

        if username in CONTROL_PAUSED_ACCOUNTS:
            update_account_metric(
                username,
                "status",
                status="Browser Mode / Auto Off",
            )
        else:
            update_account_metric(
                username,
                "status",
                status="Browser Mode / Auto On",
            )

    return "ran"


def run_profile_workflow(username, conf, folder_pool):
    global NEXT_TASK_OVERRIDE

    if username in BROWSER_NATIVE_ACCOUNTS:
        return run_browser_profile_workflow(
            username,
            conf,
            folder_pool,
        )

    if username in CONTROL_DISCONNECTED_ACCOUNTS:
        update_account_metric(username, "status", status="Disconnected")
        return "disconnected"

    forced_task = NEXT_TASK_OVERRIDE.pop(username, None)
    if username in CONTROL_PAUSED_ACCOUNTS and not forced_task:
        update_account_metric(username, "status", status="Connected / Auto Off")
        return "paused"

    cl = CLIENT_CACHE.get(username)
    if cl is None:
        update_account_metric(username, "status", status="Disconnected")
        return "disconnected"

    patch_obsolete_qe_expose(cl, username)
    settings = get_account_control_settings(username, conf)
    update_account_metric(username, "status", status="Active")

    history = load_json(conf["history_file"], {
        "posted_ids": [], "replied_dms": [], "replied_comments": [],
        "liked_medias": [], "commented_medias": [], "followed_users": [],
        "blocked_or_missing": [], "competitor_pool": [],
        "followers_next_max_id": None, "following_next_max_id": None,
        "dm_reply_memory": {},
    })

    actions_performed = 0
    try:
        # Manual actions remain available for non-DM features, but each action
        # still obeys its feature checkbox.
        if forced_task == "comments":
            handle_post_comments(cl, history, username)
        elif forced_task == "networking":
            actions_performed = harvest_and_amplify_networks(
                cl, history, conf, username, actions_performed
            )
        elif forced_task == "hashtags":
            actions_performed = interact_with_hashtags(
                cl, history, conf, username, actions_performed
            )
        elif forced_task == "repost":
            execute_repost_flow(cl, history, username, folder_pool)
        else:
            # DMs/comments are independent listeners. They run ONLY if checked.
            if settings.get("enable_dms", False):
                handle_direct_messages(cl, history, username)
            if settings.get("enable_comments", False):
                handle_post_comments(cl, history, username)

            # Pick one outward activity each workflow from the currently enabled
            # features. Re-read settings first so Control Head changes take effect.
            settings = get_account_control_settings(username, conf)
            choices = []
            if settings.get("enable_posts", False) and folder_pool:
                choices.append("repost")
            if settings.get("enable_follow", False):
                choices.append("networking")
            if settings.get("enable_engage", False):
                choices.append("hashtags")

            if choices:
                cycle_index = int(history.get("outward_cycle_index", 0) or 0)
                task = choices[cycle_index % len(choices)]
                history["outward_cycle_index"] = cycle_index + 1
                if task == "repost":
                    execute_repost_flow(cl, history, username, folder_pool)
                elif task == "networking":
                    actions_performed = harvest_and_amplify_networks(
                        cl, history, conf, username, actions_performed
                    )
                elif task == "hashtags":
                    actions_performed = interact_with_hashtags(
                        cl, history, conf, username, actions_performed
                    )
    finally:
        save_json(conf["history_file"], history)
        if username in CONTROL_DISCONNECTED_ACCOUNTS:
            update_account_metric(username, "status", status="Disconnected")
        elif username in CONTROL_PAUSED_ACCOUNTS:
            update_account_metric(username, "status", status="Connected / Auto Off")
        else:
            update_account_metric(username, "status", status="Connected / Auto On")

    return "ran"






def _control_conf(username):
    username = str(username or "").strip().lstrip("@")
    return CONTROL_ROSTER.get(username)



def _recent_posts_path():
    return DOWNLOAD_ROOT / "recent_posts.json"


def load_recent_posts():
    try:
        p=_recent_posts_path()
        if p.exists():
            data=json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data,list):
                return data
    except Exception:
        pass
    return []


def save_recent_posts(items):
    p=_recent_posts_path()
    tmp=p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[:100],indent=2,ensure_ascii=False),encoding="utf-8")
    tmp.replace(p)


def record_recent_post(username, folder_path, caption, media_id="", permalink=""):
    items=load_recent_posts()
    item={
        "account":username,
        "at":datetime.now().isoformat(timespec="seconds"),
        "folder":str(Path(folder_path).resolve()),
        "caption":str(caption or "")[:500],
        "media_id":str(media_id or ""),
        "permalink":str(permalink or ""),
    }
    items.insert(0,item)
    save_recent_posts(items)
    return item


def open_local_folder(path_value):
    path=Path(str(path_value or "")).expanduser()
    try:
        resolved=path.resolve()
    except Exception:
        resolved=path

    # Only allow opening folders inside the configured media pool.
    try:
        media_root_resolved=get_media_root().resolve()
        if media_root_resolved not in resolved.parents and resolved != media_root_resolved:
            raise ValueError("Folder is outside the configured media root.")
    except Exception:
        raise ValueError("Could not validate folder path.")

    if not resolved.exists() or not resolved.is_dir():
        raise FileNotFoundError(f"Folder no longer exists: {resolved}")

    if os.name=="nt":
        os.startfile(str(resolved))
    elif sys.platform=="darwin":
        subprocess.Popen(["open",str(resolved)])
    else:
        subprocess.Popen(["xdg-open",str(resolved)])
    return {"ok":True,"folder":str(resolved)}



def _write_instagram_accounts_file():
    """
    Persist only non-secret account configuration.
    Sessions/passwords remain outside this file.
    """
    accounts = []
    for username, conf in FAN_ROSTER.items():
        if not isinstance(conf, dict):
            continue
        accounts.append({
            "username": username,
            "enabled": bool(conf.get("enabled", True)),
            "target_hashtags": list(conf.get("target_hashtags") or []),
            "target_accounts": list(conf.get("competitor_accounts") or []),
        })
    IG_ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = IG_ACCOUNTS_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"accounts": accounts}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(IG_ACCOUNTS_FILE)


def _new_instagram_account_conf(username):
    slug = _safe_account_slug(username)
    return {
        "enabled": True,
        "password_env": "",
        "totp_env": "",
        "session_file": f"session_{slug}.json",
        "history_file": f"history_{slug}.json",
        "target_hashtags": [],
        "competitor_accounts": [],
    }


def _control_add_account(username):
    username = str(username or "").strip().lstrip("@")
    username = re.sub(r"[^A-Za-z0-9._]", "", username)
    if not username:
        raise ValueError("Enter a valid Instagram username.")
    if username in CONTROL_ROSTER:
        return {"ok": True, "username": username, "existing": True}
    if len(CONTROL_ROSTER) >= 3:
        raise RuntimeError("The Control Head supports a maximum of 3 configured Instagram accounts.")

    conf = _new_instagram_account_conf(username)
    FAN_ROSTER[username] = conf
    CONTROL_ROSTER[username] = conf
    CONTROL_DISCONNECTED_ACCOUNTS.add(username)
    CONTROL_PAUSED_ACCOUNTS.add(username)
    AUTH_EVENT[username] = "disconnected"
    update_account_metric(username, "status", status="Disconnected / Auto Off")
    _write_instagram_accounts_file()

    return {
        "ok": True,
        "username": username,
        "existing": False,
        "session_file": conf["session_file"],
    }



def _control_remove_account(username):
    """
    Remove an account from the configured dashboard roster.

    Saved session/history/settings files are deliberately preserved so re-adding
    the same username restores its existing local setup.
    """
    username = str(username or "").strip().lstrip("@")
    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")

    # Local disconnect only; do NOT invalidate the Instagram session.
    CLIENT_CACHE.pop(username, None)
    CONTROL_DISCONNECTED_ACCOUNTS.discard(username)
    CONTROL_PAUSED_ACCOUNTS.discard(username)
    CONTROL_FORCE_RUN.discard(username)
    NEXT_TASK_OVERRIDE.pop(username, None)
    LAST_DM_POLL.pop(username, None)
    AUTH_RETRY_AFTER.pop(username, None)
    ACCOUNT_COOLDOWNS.pop(username, None)
    AUTH_EVENT.pop(username, None)

    CONTROL_ROSTER.pop(username, None)
    FAN_ROSTER.pop(username, None)
    _write_instagram_accounts_file()

    return {
        "ok": True,
        "username": username,
        "preserved_local_data": True,
    }


def _control_metrics_payload():
    metrics=load_analytics(); result={}
    for username,conf in CONTROL_ROSTER.items():
        row=dict(metrics.get(username,_new_account_metrics())); session_path=DOWNLOAD_ROOT/conf["session_file"]
        browser_mode=username in BROWSER_NATIVE_ACCOUNTS; browser_live=_browser_live_port_open(username)
        private_connected=username in CLIENT_CACHE and username not in CONTROL_DISCONNECTED_ACCOUNTS
        connected=private_connected or (browser_mode and browser_live)
        daily_follow_used, daily_follow_cap = _daily_follow_attempts_status(username)
        row["control"]={
            "connected":connected,"disconnected":not connected,"paused":username in CONTROL_PAUSED_ACCOUNTS,
            "auto_enabled":connected and username not in CONTROL_PAUSED_ACCOUNTS,
            "browser_mode":browser_mode,"browser_live":browser_live,
            "browser_login_in_progress":username in BROWSER_LOGIN_IN_PROGRESS,
            "browser_profile_saved":_browser_profile_saved(username),"private_api_connected":private_connected,
            "saved_session":session_path.exists(),"session_file":session_path.name,
            "queued_task":NEXT_TASK_OVERRIDE.get(username,""),"auth_event":AUTH_EVENT.get(username,""),
            "write_budget_used":len(_prune_write_window(username)),"write_budget_max":MAX_WRITES_PER_WINDOW,
            "daily_follow_used":daily_follow_used,"daily_follow_cap":daily_follow_cap,
            "write_window_seconds":WRITE_WINDOW_SECONDS,"upload_cooldown_seconds":_effective_pacing(username)["upload"],
            "settings":get_account_control_settings(username,conf),"safety":get_account_safety_state(username),
            "pace_mode":_runtime_pace_mode(username),
            "burst_remaining_seconds":max(
                0,
                int(float(BURST_UNTIL.get(username,0.0) or 0.0)-time.time()),
            ),
        }
        result[username]=row
    return {"accounts":result,"recent_posts":load_recent_posts()[:25],"media":media_root_payload()}




def _control_browser_login(username, timeout_seconds=600):
    username = str(username or "").strip().lstrip("@")
    with ACCOUNT_WORKFLOW_THREADS_LOCK:
        active_worker = ACCOUNT_WORKFLOW_THREADS.get(username)
    if active_worker and active_worker.is_alive():
        raise RuntimeError(
            f"A workflow is still running for @{username}. Wait for it to finish, "
            "then click Browser Login."
        )

    if username in BROWSER_LOGIN_IN_PROGRESS:
        if _browser_live_port_open(username):
            raise RuntimeError(
                f"Browser Login is already waiting for @{username}. "
                "Use the existing visible Chromium window and finish the login there."
            )
        BROWSER_LOGIN_IN_PROGRESS.discard(username)
    BROWSER_LOGIN_IN_PROGRESS.add(username)
    try:
        return _control_browser_login_impl(username, timeout_seconds=timeout_seconds)
    finally:
        BROWSER_LOGIN_IN_PROGRESS.discard(username)


def _control_browser_login_impl(username, timeout_seconds=600):
    """
    Start or reconnect to one live Chromium process for this account.

    The user completes login/challenges manually. Chromium stays open afterward,
    and Browser Mode reconnects to the exact same live browser over localhost
    CDP instead of recreating authenticated state.

    No CAPTCHA/challenge is bypassed.
    """
    username = str(username or "").strip().lstrip("@")
    conf = _control_conf(username)
    if not conf:
        raise ValueError(f"Unknown account @{username}")

    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is required for Browser Login. Install with: "
            "python -m pip install playwright && python -m playwright install chromium"
        )

    session_p = DOWNLOAD_ROOT / conf["session_file"]

    update_account_metric(
        username,
        "status",
        status="Browser Login Open",
    )
    update_account_metric(
        username,
        "add_history",
        value=(
            "🌐 Opening live Chromium. Complete Instagram login and any "
            "verification manually. Leave this Chromium window open; "
            "Browser Mode will reuse this exact session."
        ),
    )

    sessionid = None
    live_authenticated = False

    with sync_playwright() as p:
        browser, context = _browser_launch_live_process(
            p,
            username,
            force_fresh_visible=False,
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.bring_to_front()
        except Exception:
            pass

        update_account_metric(
            username,
            "add_history",
            value=(
                "👀 Browser Login window is open and was brought to the front. "
                "Log into Instagram manually in that Chromium window."
            ),
        )

        try:
            if "instagram.com" not in str(page.url or "").lower():
                page.goto(
                    "https://www.instagram.com/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
        except Exception:
            pass

        # Recover ordinary saved-profile / optional-prompt states before
        # waiting for manual security-sensitive login work.
        try:
            _browser_safe_recover_page(
                page,
                context,
                username,
                reason="Browser Login initial page",
                max_steps=2,
            )
        except Exception:
            pass

        deadline = time.time() + max(120, int(timeout_seconds))
        last_log = 0.0
        last_adaptive_recovery = 0.0

        while time.time() < deadline:
            try:
                cookies = context.cookies("https://www.instagram.com/")
                sessionid = next(
                    (
                        c.get("value")
                        for c in cookies
                        if c.get("name") == "sessionid" and c.get("value")
                    ),
                    None,
                )
            except Exception:
                sessionid = None

            try:
                current_url = str(page.url or "").lower()
            except Exception:
                current_url = ""

            try:
                if _browser_saved_account_resume_visible(page, username):
                    _browser_resume_saved_account(page, context, username)
            except Exception:
                pass

            now = time.time()
            if now - last_adaptive_recovery >= 12:
                try:
                    _browser_safe_recover_page(
                        page,
                        context,
                        username,
                        reason="Browser Login waiting page",
                        max_steps=2,
                    )
                except Exception:
                    pass
                last_adaptive_recovery = now

            live_authenticated = _browser_page_authenticated(page, username)
            if live_authenticated:
                break

            now = time.time()
            if now - last_log >= 20:
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "🌐 Waiting for manual Browser Login to complete in "
                        "the live Chromium window..."
                    ),
                )
                last_log = now

            page.wait_for_timeout(1000)

        if not live_authenticated:
            update_account_metric(username, "status", status="Login Needed")
            raise RuntimeError(
                "Browser Login timed out before the live Instagram page became authenticated. "
                "Finish login/verification in the visible Chromium window."
            )

        finish_seconds = BROWSER_LOGIN_FINISH_SECONDS
        update_account_metric(
            username,
            "status",
            status="Browser Login — Finish Prompts",
        )
        update_account_metric(
            username,
            "add_history",
            value=(
                f"🌐 Live session detected. You have {finish_seconds}s to "
                "finish Save login info / notification / security prompts. "
                "Chromium will remain OPEN afterward."
            ),
        )

        finish_deadline = time.time() + finish_seconds
        next_notice = time.time() + 30
        next_finish_recovery = time.time()

        while time.time() < finish_deadline:
            try:
                cookies = context.cookies("https://www.instagram.com/")
                updated = next(
                    (
                        c.get("value")
                        for c in cookies
                        if c.get("name") == "sessionid" and c.get("value")
                    ),
                    None,
                )
                if updated:
                    sessionid = updated
            except Exception:
                pass

            if time.time() >= next_finish_recovery:
                try:
                    _browser_safe_recover_page(
                        page,
                        context,
                        username,
                        reason="Browser Login finishing prompts",
                        max_steps=2,
                    )
                except Exception:
                    pass
                next_finish_recovery = time.time() + 8

            if time.time() >= next_notice:
                remaining = max(0, int(finish_deadline - time.time()))
                update_account_metric(
                    username,
                    "add_history",
                    value=f"🌐 Live Browser Login: {remaining}s finishing time remaining.",
                )
                next_notice = time.time() + 30

            page.wait_for_timeout(1000)

        try:
            _browser_safe_recover_page(
                page,
                context,
                username,
                reason="Browser Login final verification",
                max_steps=3,
            )
        except Exception:
            pass

        if not _browser_page_authenticated(page, username):
            problem = _browser_page_problem(page)
            if problem:
                update_account_metric(
                    username,
                    "status",
                    status="Browser Verification Needed",
                )
                raise RuntimeError(
                    f"Browser Login ended on a manual/security page: {problem}"
                )
            raise RuntimeError(
                "Browser Login did not finish on Instagram's authenticated "
                "Web shell. The live Chromium window was left open so you can "
                "inspect the remaining page."
            )

        _save_browser_sessionid(username, sessionid)
        _save_full_browser_storage_state(context, username)

        # Do not close the external browser.
        LIVE_CDP_CONTEXT_IDS.discard(id(context))
        BROWSER_HANDLE_BY_CONTEXT.pop(id(context), None)

    update_account_metric(
        username,
        "add_history",
        value=(
            "✅ Live Instagram Chromium session is authenticated and left open. "
            "Browser Mode will reconnect to this exact browser process."
        ),
    )

    private_api_ready = False
    cl = Client()
    _refresh_instagram_app_profile(cl, username)

    try:
        if not sessionid:
            raise RuntimeError("No browser sessionid was exposed; using Browser Mode only.")
        cl.login_by_sessionid(sessionid)

        if verify_authenticated_identity(cl, username, session_p):
            try:
                cl.get_timeline_feed()
                private_api_ready = True
            except Exception:
                private_api_ready = False

        if private_api_ready:
            cl.dump_settings(session_p)
            patch_obsolete_qe_expose(cl, username)
            _deactivate_browser_native_mode(username)

            with CONTROL_LOCK:
                CLIENT_CACHE[username] = cl
                CONTROL_DISCONNECTED_ACCOUNTS.discard(username)
                CONTROL_PAUSED_ACCOUNTS.add(username)
                AUTH_RETRY_AFTER.pop(username, None)
                ACCOUNT_COOLDOWNS.pop(username, None)
                AUTH_EVENT[username] = "browser_login_private_ready"

            update_account_metric(
                username,
                "status",
                status="Connected / Auto Off",
            )
        else:
            _activate_browser_native_mode(
                username,
                (
                    "Live Instagram Web session is authenticated. "
                    "Private API remains unavailable, so Browser Mode will "
                    "reuse the still-open Chromium process."
                ),
            )

    except Exception as exc:
        _activate_browser_native_mode(
            username,
            (
                "Live Instagram Web session is authenticated. "
                "Private API remains unavailable, so Browser Mode will "
                "reuse the still-open Chromium process."
            ),
        )
        update_account_metric(
            username,
            "add_history",
            value=(
                "ℹ️ Private API remains unavailable after Browser Login "
                f"({type(exc).__name__}: {str(exc)[:120]}). "
                "The live Web session remains usable."
            ),
        )

    return {
        "ok": True,
        "mode": "private_api" if private_api_ready else "live_browser",
        "browser_saved": True,
        "live_browser": True,
        "private_api_ready": private_api_ready,
        "message": (
            "Live Instagram browser is authenticated and remains open. "
            + (
                "Private API is also ready."
                if private_api_ready
                else "Browser Mode will reuse this exact live Chromium session."
            )
        ),
    }



def _verify_live_browser_ready(username: str) -> tuple[bool, str]:
    if not _browser_live_port_open(username):
        return False, "live Chromium is not running"
    try:
        _browser_preflight_login(username, headed=True)
        return True, "authenticated live Chromium verified"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:180]}"

def _control_quick_login(username):
    username = str(username or "").strip().lstrip("@")
    conf = _control_conf(username)
    if not conf:
        raise ValueError(f"Unknown account @{username}")

    session_p = DOWNLOAD_ROOT / conf["session_file"]

    if _browser_live_port_open(username):
        live_ready, live_reason = _verify_live_browser_ready(username)
        if live_ready:
            _activate_browser_native_mode(
                username,
                "Quick Login verified the already-running authenticated Instagram Chromium session.",
            )
            update_account_metric(
                username,
                "add_history",
                value="🌐 Quick Login attached to the existing authenticated live Chromium.",
            )
            return {
                "ok": True,
                "mode": "browser_native",
                "browser_mode": True,
                "private_api_ready": False,
            }

        _mark_browser_login_needed(
            username,
            f"live Chromium exists but is not authenticated ({live_reason})",
        )
        raise RuntimeError(
            "Chromium is open, but Instagram is not authenticated in that window. "
            "Finish the visible Continue/login/security prompt."
        )

    if _browser_profile_saved(username):
        update_account_metric(
            username,
            "add_history",
            value=(
                "🌐 Quick Login: live Chromium is closed; reopening the saved "
                "browser profile and attempting safe resume..."
            ),
        )

        live_ready, live_reason = _quick_reopen_live_browser(username)

        if live_ready:
            _activate_browser_native_mode(
                username,
                "Quick Login reopened and verified the authenticated live Instagram Chromium session.",
            )
            update_account_metric(
                username,
                "add_history",
                value=(
                    "✅ Quick Login reopened the saved Chromium profile and "
                    "verified Instagram Web authentication."
                ),
            )
            return {
                "ok": True,
                "mode": "browser_native",
                "browser_mode": True,
                "private_api_ready": False,
            }

        _mark_browser_login_needed(
            username,
            f"Quick Login reopened Chromium but could not authenticate it ({live_reason})",
        )
        raise RuntimeError(
            "Quick Login reopened Chromium but Instagram still needs manual "
            f"attention: {live_reason}. Use Browser Login."
        )

    if not session_p.exists():
        raise RuntimeError(
            "No saved browser profile or private session exists. Use Browser Login."
        )

    cl = Client()
    cl.load_settings(session_p)
    _refresh_instagram_app_profile(cl, username)

    try:
        cl.get_timeline_feed()
    except Exception as exc:
        if is_login_required_error(exc):
            stop_account_for_private_login_required(
                username,
                "Quick Login private timeline validation",
                exc,
                hours=6,
            )
            raise RuntimeError(
                "Saved private-API session is not accepted and there is no reusable "
                "browser profile. Use Browser Login."
            ) from exc
        raise

    if not verify_authenticated_identity(cl, username, session_p):
        raise RuntimeError("Saved session identity does not match this roster account.")

    patch_obsolete_qe_expose(cl, username)
    _deactivate_browser_native_mode(username)

    with CONTROL_LOCK:
        CLIENT_CACHE[username] = cl
        CONTROL_DISCONNECTED_ACCOUNTS.discard(username)
        CONTROL_PAUSED_ACCOUNTS.add(username)
        AUTH_RETRY_AFTER.pop(username, None)
        ACCOUNT_COOLDOWNS.pop(username, None)
        LAST_DM_POLL.pop(username, None)
        AUTH_EVENT[username] = "quick_login"

    update_account_metric(username, "status", status="Connected / Auto Off")
    update_account_metric(
        username,
        "add_history",
        value=(
            "🔓 Quick Login: private API session connected; background automation "
            "remains OFF until Start Automation is pressed."
        ),
    )

    return {
        "ok": True,
        "mode": "saved_session",
        "browser_mode": False,
        "private_api_ready": True,
    }





def _control_password_login(username, password, verification_code=""):
    """
    Explicit credential login initiated from localhost.

    Password and current 2FA code are used only in this request. They are not
    stored in analytics, history, FAN_ROSTER, or the Control Head page.
    A successful instagrapi session is saved for future Quick Login.
    """
    username = str(username or "").strip().lstrip("@")
    conf = _control_conf(username)
    if not conf:
        raise ValueError(f"Unknown account @{username}")

    password = str(password or "")
    verification_code = str(verification_code or "").strip()

    if not password:
        raise ValueError("Password is required for Password Login.")

    session_p = DOWNLOAD_ROOT / conf["session_file"]
    cl = Client()

    # Reuse device/session identity if a previous session exists, then refresh
    # only the Instagram app build metadata so an old JSON cannot advertise an
    # obsolete Instagram version.
    if session_p.exists():
        try:
            cl.load_settings(session_p)
        except Exception:
            pass

    _refresh_instagram_app_profile(cl, username)

    try:
        cl.login(
            username,
            password,
            verification_code=verification_code,
        )
    except Exception as exc:
        if _is_needs_upgrade_error(exc):
            set_auth_cooldown(username, minutes=360)
            update_account_metric(
                username,
                "status",
                status="Private API Login Blocked",
            )
            update_account_metric(
                username,
                "add_history",
                value=(
                    "📱 " + _needs_upgrade_message(username)
                    + " Password-login retries paused for 6 hours."
                ),
            )
            raise RuntimeError(
                _needs_upgrade_message(username)
                + " Use Browser Login on the dashboard. "
                "Password-login retries are paused for 6 hours."
            ) from exc
        raise

    if not verify_authenticated_identity(cl, username, session_p):
        try:
            cl.logout()
        except Exception:
            pass
        raise RuntimeError("Instagram authenticated a different account.")

    cl.dump_settings(session_p)
    patch_obsolete_qe_expose(cl, username)

    with CONTROL_LOCK:
        CLIENT_CACHE[username] = cl
        CONTROL_DISCONNECTED_ACCOUNTS.discard(username)
        CONTROL_PAUSED_ACCOUNTS.add(username)
        AUTH_RETRY_AFTER.pop(username, None)
        ACCOUNT_COOLDOWNS.pop(username, None)
        LAST_DM_POLL.pop(username, None)
        AUTH_EVENT[username] = "control_login"

    update_account_metric(username, "status", status="Connected / Auto Off")
    update_account_metric(
        username, "add_history",
        value="🔐 Password Login succeeded; session saved and connected with background automation OFF."
    )
    return {"ok": True, "mode": "password_login"}


def _control_disconnect(username):
    """
    Safe testing logout:
    stop all use of the account in this process but KEEP the saved session.
    No Instagram logout request is sent.
    """
    username = str(username or "").strip().lstrip("@")
    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")

    with CONTROL_LOCK:
        CLIENT_CACHE.pop(username, None)
        BROWSER_NATIVE_ACCOUNTS.discard(username)
        CONTROL_DISCONNECTED_ACCOUNTS.add(username)
        CONTROL_PAUSED_ACCOUNTS.discard(username)
        CONTROL_FORCE_RUN.discard(username)
        NEXT_TASK_OVERRIDE.pop(username, None)
        LAST_DM_POLL.pop(username, None)
        AUTH_EVENT[username] = "disconnected"

    update_account_metric(username, "status", status="Disconnected")
    update_account_metric(
        username, "add_history",
        value="🔌 Disconnected locally; saved session preserved for Quick Login."
    )
    return {"ok": True}


def _control_full_logout(username):
    """
    Deliberate destructive logout:
    invalidate the active Instagram session when possible and delete its saved
    session file. Future login will require credentials again.
    """
    username = str(username or "").strip().lstrip("@")
    conf = _control_conf(username)
    if not conf:
        raise ValueError(f"Unknown account @{username}")

    with CONTROL_LOCK:
        cl = CLIENT_CACHE.pop(username, None)
        CONTROL_DISCONNECTED_ACCOUNTS.add(username)
        CONTROL_PAUSED_ACCOUNTS.discard(username)
        CONTROL_FORCE_RUN.discard(username)
        NEXT_TASK_OVERRIDE.pop(username, None)
        LAST_DM_POLL.pop(username, None)

    if cl is not None:
        try:
            cl.logout()
        except Exception:
            pass

    session_p = DOWNLOAD_ROOT / conf["session_file"]
    try:
        if session_p.exists():
            session_p.unlink()
    except OSError as exc:
        raise RuntimeError(f"Could not delete saved session: {exc}")

    AUTH_EVENT[username] = "full_logout"
    update_account_metric(username, "status", status="Disconnected")
    update_account_metric(
        username, "add_history",
        value="🔒 Full Logout: Instagram logout requested and saved session deleted."
    )
    return {"ok": True}


def _control_pause(username, pause):
    """
    Stop/start background automation for either instagrapi or Browser Mode.

    Browser Mode ignores private-API auth cooldowns because it runs through the
    authenticated Instagram Web profile. Genuine platform safety backoffs still
    apply to both modes.
    """
    username = str(username or "").strip().lstrip("@")
    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")

    if not pause and username in BROWSER_LOGIN_IN_PROGRESS:
        raise RuntimeError(
            f"Browser Login is still in progress for @{username}. "
            "Finish login in Chromium before starting Auto."
        )

    browser_mode = username in BROWSER_NATIVE_ACCOUNTS
    if (not pause) and browser_mode and not _browser_live_port_open(username):
        _mark_browser_login_needed(username, 'the live Chromium window was closed')
        raise RuntimeError('Browser Mode cannot start because its live Chromium window is closed. Click Browser Login first.')
    private_connected = (
        username in CLIENT_CACHE
        and username not in CONTROL_DISCONNECTED_ACCOUNTS
    )
    connected = private_connected or browser_mode

    if not connected:
        raise RuntimeError(
            "Account is disconnected. Use Quick Login or Browser Login first."
        )

    mode_label = "Browser Mode" if browser_mode else "Connected"

    if pause:
        CONTROL_PAUSED_ACCOUNTS.add(username)
        CONTROL_FORCE_RUN.discard(username)
        NEXT_TASK_OVERRIDE.pop(username, None)
        update_account_metric(
            username,
            "status",
            status=f"{mode_label} / Auto Off",
        )
        update_account_metric(
            username,
            "add_history",
            value=(
                f"⏹️ Background automation stopped; "
                f"{mode_label} remains connected."
            ),
        )
    else:
        # Genuine safety backoffs apply in both modes.
        state = get_account_safety_state(username)
        if state["active"]:
            raise RuntimeError(
                f"Safety Backoff is active until {state['until']}. "
                f"Reason: {state['reason'][:160]}"
            )

        if browser_mode:
            # Clear only the generic/private-API cooldown. The browser workflow
            # will independently stop if Instagram Web shows a restriction,
            # challenge, or "try again later" message.
            ACCOUNT_COOLDOWNS.pop(username, None)
        else:
            cooldown = ACCOUNT_COOLDOWNS.get(username)
            if cooldown and datetime.now() < cooldown:
                raise RuntimeError(
                    f"Account cooldown is active until "
                    f"{cooldown.strftime('%Y-%m-%d %H:%M')}."
                )

        CONTROL_PAUSED_ACCOUNTS.discard(username)
        # Start Automation should not inherit a stale wait from an earlier
        # manual action; request one immediate scheduler pass.
        CONTROL_FORCE_RUN.add(username)
        update_account_metric(
            username,
            "status",
            status=f"{mode_label} / Auto On",
        )
        update_account_metric(
            username,
            "add_history",
            value=(
                f"▶️ Background automation started in {mode_label}."
            ),
        )

    return {
        "ok": True,
        "paused": pause,
        "auto_enabled": not pause,
        "browser_mode": browser_mode,
    }





def _control_queue_task(username, task):
    username = str(username or "").strip().lstrip("@")
    allowed = {"repost", "networking", "hashtags", "comments", "reels_demo"}

    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")

    if username in BROWSER_LOGIN_IN_PROGRESS:
        raise RuntimeError(
            f"Browser Login is still in progress for @{username}. "
            "Finish login in Chromium before running manual tasks."
        )

    browser_mode = username in BROWSER_NATIVE_ACCOUNTS
    if browser_mode and not _browser_live_port_open(username):
        _mark_browser_login_needed(username, 'the live Chromium window was closed before the manual task')
        raise RuntimeError('The live Chromium window is closed. Click Browser Login before running this task.')
    private_connected = (
        username in CLIENT_CACHE
        and username not in CONTROL_DISCONNECTED_ACCOUNTS
    )
    connected = private_connected or browser_mode

    if not connected:
        raise RuntimeError(
            "Account is disconnected. Use Quick Login or Browser Login first."
        )

    state = get_account_safety_state(username)
    if state["active"]:
        raise RuntimeError(
            f"Safety Backoff is active until {state['until']}: "
            f"{state['reason'][:140]}"
        )

    if task not in allowed:
        raise ValueError(f"Unsupported task: {task}")

    if task == "reels_demo" and not browser_mode:
        raise RuntimeError(
            "Reels Demo requires Browser Mode. Use Quick Login or Browser Login."
        )

    if task == "repost":
        pacing_reason = _shared_write_block_reason(username, "upload")
        if pacing_reason:
            raise RuntimeError(
                f"Upload/Repost is pacing-blocked: {pacing_reason}. "
                "If this came from the known old false-positive verifier, "
                "click Clear Upload Cooldown once."
            )

    if browser_mode:
        # Do not let a private-API auth cooldown suppress a Web UI task.
        ACCOUNT_COOLDOWNS.pop(username, None)

    with CONTROL_LOCK:
        NEXT_TASK_OVERRIDE[username] = task
        CONTROL_FORCE_RUN.add(username)

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🎛️ Queued manual task: {task}"
            + (" (Browser Mode)" if browser_mode else "")
            + "; worker should start on the next scheduler tick (~1s)"
        ),
    )

    return {
        "ok": True,
        "task": task,
        "browser_mode": browser_mode,
    }




def _control_send_dm(username, recipient, message):
    username = str(username or "").strip().lstrip("@")
    recipient = str(recipient or "").strip().lstrip("@")
    message = str(message or "").strip()

    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")
    if username in CONTROL_DISCONNECTED_ACCOUNTS:
        raise RuntimeError("Account is disconnected. Quick Login first.")
    if not recipient:
        raise ValueError("Recipient username is required.")
    if not message:
        raise ValueError("Message text is required.")

    cl = CLIENT_CACHE.get(username)
    if cl is None:
        raise RuntimeError("No connected Instagram client for this account.")

    recipient_id = cl.user_id_from_username(recipient)
    if not wait_for_write_slot(username, "dm", max_wait=180):
        raise RuntimeError("DM deferred by pacing budget; try again shortly.")

    result = cl.direct_send(message, user_ids=[recipient_id])
    if not result:
        raise RuntimeError("Instagram did not confirm the DM send.")

    record_write(username, "dm")

    update_account_metric(
        username, "add_history",
        value=f"✉️ Manual DM sent to @{recipient}: {message[:100]}"
    )
    return {"ok": True}



def _control_save_settings(username, payload):
    username=str(username or "").strip().lstrip("@")
    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")
    settings=update_account_control_settings(username,payload)
    return {"ok":True,"settings":settings}


def _json_response(handler, status, payload):
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _read_json_body(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length <= 0:
        return {}
    if length > 65536:
        raise ValueError("Request too large.")
    return json.loads(handler.rfile.read(length).decode("utf-8"))


class DashboardAPIHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path == "/api/metrics":
            _json_response(self, 200, _control_metrics_payload())
            return

        if self.path not in ["/", "/index.html"]:
            self.send_error(404)
            return

        html = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Instagram Automation Control Head</title>
<style>
:root{
  color-scheme:dark;
  --bg:#0f111a;--panel:#181a26;--panel2:#10121a;--border:#2b3044;
  --text:#e0e3f3;--muted:#9299bb;--ok:#72dfbd;--warn:#ffd36c;--bad:#ff7d8b;
}
*{box-sizing:border-box}
body{margin:0;padding:18px;background:var(--bg);color:var(--text);
     font-family:system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;
       gap:14px;flex-wrap:wrap;margin-bottom:16px}
h1{font-size:21px;margin:0}.sub{color:var(--muted);font-size:12px}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(410px,1fr));gap:16px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:15px}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
h2{font-size:17px;margin:0}.badges{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end}
.badge{font-size:10px;padding:4px 7px;border-radius:999px;background:#252a3e}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:12px 0}
.stat{background:var(--panel2);padding:8px;border-radius:7px;text-align:center}
.stat b{display:block;font-size:16px}.stat span{font-size:10px;color:var(--muted)}
.row{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0}
button{background:#282d42;color:var(--text);border:1px solid var(--border);
       border-radius:7px;padding:8px 10px;cursor:pointer}
button:hover{background:#363c58}button:disabled{opacity:.45;cursor:not-allowed}
button.good{border-color:#346f5b}button.warn{border-color:#75602d}
button.danger{border-color:#703b44}
.fieldgrid{display:grid;grid-template-columns:1fr 1fr;gap:7px}
label{display:block;font-size:11px;color:var(--muted);margin:7px 0 3px}
input,textarea,select{width:100%;background:#0d0f17;color:var(--text);border:1px solid var(--border);
               border-radius:7px;padding:8px;font:inherit}
textarea{min-height:70px;resize:vertical}
.section{border-top:1px solid var(--border);margin-top:12px;padding-top:10px}
.logs{height:145px;overflow:auto;background:#0d0f17;border-radius:7px;padding:8px;
      font:11px ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap}
.notice{padding:9px;border-radius:7px;background:#111522;color:var(--muted);font-size:12px;margin-bottom:12px}
.toast{position:fixed;right:15px;bottom:15px;max-width:440px;padding:10px 12px;
       background:#252b40;border:1px solid var(--border);border-radius:8px;display:none;z-index:50}
.small{font-size:11px;color:var(--muted)}
</style>
</head>
<body>
<header>
  <div>
    <h1>🎛️ Instagram Automation Control Head</h1>
    <div class="sub">Boot mode: DISCONNECTED — write actions are shared-paced across separate bot processes after login.</div>
  </div>
  <div class="toolbar">
    <label style="margin:0">
      <input id="autoRefresh" type="checkbox" checked style="width:auto">
      Auto Refresh
    </label>
    <button onclick="refreshNow()">Refresh Now</button>
    <button class="good" onclick="addInstagramAccount()">Add Account</button>
    <span id="refreshState" class="small"></span>
  </div>
</header>

<div class="notice">
  <b>Testing-safe startup:</b> restarting this Python program does not automatically log any account in.
  <b>Quick Login</b> reuses the saved session. <b>Disconnect</b> keeps that session.
  <b>Full Logout</b> deletes it. Logging in leaves automation OFF until you press <b>Start Automation</b>. <b>Remove Account</b> removes the dashboard entry but preserves its local session/history for easy re-add.
</div>

<div class="card" style="margin-bottom:16px">
  <div class="cardhead">
    <div>
      <h2>📁 Media Library</h2>
      <div class="small">
        Select the parent folder that contains your media/post subfolders.
        The setting is saved and used by all configured Instagram accounts.
      </div>
    </div>
    <div class="badges">
      <span id="mediaRootBadge" class="badge">Loading...</span>
    </div>
  </div>

  <div class="section">
    <label>Media folder</label>
    <input
      id="mediaRootInput"
      type="text"
      placeholder="C:\Users\Thomas\social-media-pool"
      onfocus="editing=true"
      oninput="editing=true"
    >
    <div class="row">
      <button class="good" onclick="saveMediaRoot()">Use Folder</button>
      <button onclick="browseMediaRoot()">Browse Folder</button>
      <button onclick="openMediaRoot()">Open Folder</button>
    </div>
    <div id="mediaRootStatus" class="small"></div>
  </div>
</div>

<div id="grid" class="grid"></div>

<div class="card" style="margin-top:16px">
  <div class="cardhead">
    <div>
      <h2>Recent Posts</h2>
      <div class="small">Shows the source folder used for each upload.</div>
    </div>
  </div>
  <div id="recentPosts" class="section"></div>
</div>

<div id="toast" class="toast"></div>

<script>
const CONTROL_HEAD_REFRESH_SECONDS = 10;
let latestData = {};
let editing = false;
let refreshBusy = false;
let nextRefresh = 0;

function esc(v){
  return String(v ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));
}

function toast(msg, bad=false){
  const t=document.getElementById("toast");
  t.textContent=msg;
  t.style.display="block";
  t.style.color=bad ? "var(--bad)" : "var(--text)";
  clearTimeout(window.__toastTimer);
  window.__toastTimer=setTimeout(()=>t.style.display="none",5000);
}

document.addEventListener("focusin", e=>{
  if(e.target.matches("input,textarea,select")) editing=true;
});
document.addEventListener("focusout", e=>{
  const card=e.target.closest?.(".card[data-user]");
  setTimeout(()=>{
    editing=!!document.querySelector("input:focus,textarea:focus,select:focus");
    if(card && !editing) autoSaveBehavior(card.dataset.user,true);
  },0);
});
document.addEventListener("change", e=>{
  const card=e.target.closest?.(".card[data-user]");
  if(card) autoSaveBehavior(card.dataset.user,true);
});

function cardHtml(user,a){
  const c=a.control || {};
  const connected=!!c.connected;
  const paused=!!c.paused;
  const autoEnabled=!!c.auto_enabled;
  const session=!!c.saved_session;
  const status=connected ? "CONNECTED" : "DISCONNECTED";
  const badgeClass=connected ? "ok" : "bad";
  const logs=(a.history_log || []).map(x=>`<div>${esc(x)}</div>`).join("");
  const s=c.settings || {};
  const tags=(s.target_hashtags || []).join(", ");
  const targets=(s.target_accounts || []).join(", ");

  return `
  <div class="card" data-user="${esc(user)}">
    <div class="cardhead">
      <div>
        <h2>@${esc(user)}</h2>
        <div class="small">${esc(c.session_file || "")}</div>
      </div>
      <div class="badges">
        <span class="badge ${badgeClass}">${status}</span>
        <span class="badge ${autoEnabled ? "ok":"warn"}">${autoEnabled ? "AUTO ON":"AUTO OFF"}</span>
        <span class="badge ${session ? "ok":"warn"}">${session ? "SAVED SESSION":"NO SESSION"}</span>
        ${c.safety?.active ? `<span class="badge bad">SAFETY BACKOFF</span>` : ""}
        ${c.queued_task ? `<span class="badge warn">QUEUED: ${esc(c.queued_task)}</span>` : ""}
      </div>
    </div>

    <div class="stats">
      <div class="stat"><b>${Number(a.total_posts||0)}</b><span>Posts</span></div>
      <div class="stat"><b>${Number(a.total_follows||0)}</b><span>Follows</span></div>
      <div class="stat"><b>${Number(a.total_likes||0)}</b><span>Likes</span></div>
    </div>
    <div class="small">Write budget: ${Number(c.write_budget_used||0)}/${Number(c.write_budget_max||0)} per rolling ${Math.round(Number(c.write_window_seconds||0)/60)} min · uploads ≥ ${Math.round(Number(c.upload_cooldown_seconds||0)/60)} min apart</div>
    <div class="small">Daily follows: <b>${Number(c.daily_follow_used||0)}/${Number(c.daily_follow_cap||50)}</b> · hard reset at local midnight</div>
    <div class="small">Pace: <b>${esc(c.pace_mode||s.pace_mode||"normal")}</b>${Number(c.burst_remaining_seconds||0)>0 ? ` · burst remaining ~${Math.ceil(Number(c.burst_remaining_seconds)/60)}m` : ""}</div>
    ${c.safety?.active ? `<div class="small bad">Safety Backoff until ${esc(c.safety.until||"")} · ${esc(c.safety.reason||"")}</div>` : ""}

    <div class="section">
      <div class="row">
        <button class="good" ${!session || connected ? "disabled":""}
          onclick="quickLogin('${esc(user)}')">Quick Login</button>
        <button ${connected ? "":"disabled"} onclick="disconnectAccount('${esc(user)}')">
          Disconnect
        </button>
        <button class="danger" ${(!session && !connected) ? "disabled":""}
          onclick="fullLogout('${esc(user)}')">Full Logout</button>
        <button class="${autoEnabled ? "warn":"good"}" ${connected ? "":"disabled"}
          onclick="pauseAccount('${esc(user)}',${autoEnabled ? "true":"false"})">
          ${autoEnabled ? "Stop Automation":"Start Automation"}
        </button>
        <button class="good" ${connected ? "":"disabled"}
          onclick="burstNow('${esc(user)}')">⚡ Burst Now · 1-2 min</button>
        <button ${connected ? "":"disabled"}
          onclick="setPaceMode('${esc(user)}','overnight')">🌙 Overnight Pace</button>
        <button ${connected ? "":"disabled"}
          onclick="setPaceMode('${esc(user)}','normal')">Normal Pace</button>
        <button class="danger" onclick="removeInstagramAccount('${esc(user)}')">
          Remove Account
        </button>
      </div>

      <div class="fieldgrid">
        <div>
          <label>Password Login</label>
          <input id="pw_${esc(user)}" type="password" autocomplete="current-password"
                 placeholder="password">
        </div>
        <div>
          <label>2FA code (if required)</label>
          <input id="otp_${esc(user)}" type="password" inputmode="numeric"
                 autocomplete="one-time-code" placeholder="123456">
        </div>
      </div>
      <div class="row">
        <button class="good" onclick="browserLogin('${esc(user)}')">Browser Login</button>
        <button onclick="passwordLogin('${esc(user)}')">Password Login & Save Session</button>
      </div>
      <div class="small">
        Browser Login is preferred when Instagram Web works but Password Login returns
        <code>needs_upgrade</code>. Complete all login/challenge steps manually in Chromium. After login is detected, Chromium stays open for 90 seconds by default so you can click Instagram's Save login info prompt.
        The bot then makes one session conversion attempt and tests private-API readiness. If Instagram blocks the private API, the account stays connected in Browser Mode. If only the private hashtag feed is blocked, Engage falls back to GraphQL discovery; writes still require Instagram to accept the authenticated private action. Password/2FA are not saved.
      </div>
    </div>


    <div class="section">
      <b>Behavior Features & Settings</b>
      <div class="fieldgrid">
        <div style="grid-column:1/-1">
          <label>Enabled features</label>
          <div class="row">
            <label><input id="posts_${esc(user)}" type="checkbox" style="width:auto" ${s.enable_posts?"checked":""}> Posts</label>
            <label><input id="follow_${esc(user)}" type="checkbox" style="width:auto" ${s.enable_follow?"checked":""}> Follow</label>
            <label><input id="engage_${esc(user)}" type="checkbox" style="width:auto" ${s.enable_engage?"checked":""}> Hashtag / Engage</label>
            <label><input id="dms_${esc(user)}" type="checkbox" style="width:auto" ${s.enable_dms?"checked":""}> DMs</label>
            <label><input id="comments_${esc(user)}" type="checkbox" style="width:auto" ${s.enable_comments?"checked":""}> Comment Replies</label>
          </div>
          <div class="small">Unchecked features are not polled or executed in background automation.</div>
        </div>
        <div>
          <label>Follow source</label>
          <select id="fsource_${esc(user)}">
            ${["both","followers","following"].map(
              m=>`<option value="${m}" ${s.follow_source===m?"selected":""}>${m}</option>`
            ).join("")}
          </select>
        </div>
        <div><label>Caption char limit</label><input id="caplim_${esc(user)}" type="number" min="80" max="2200" value="${Number(s.caption_char_limit||420)}"></div>
        <div><label>Reply char limit</label><input id="replim_${esc(user)}" type="number" min="20" max="1000" value="${Number(s.reply_char_limit||280)}"></div>
        <div><label>Follow limit / pass</label><input id="followlim_${esc(user)}" type="number" min="1" max="20" value="${Number(s.follow_limit||2)}"></div>
        <div><label>Auto follow batch max (1-10 visible rows)</label><input id="autofollowmax_${esc(user)}" type="number" min="1" max="10" value="${Number(s.auto_follow_batch_max||10)}"></div>
        <div><label>Active-session passes (1-20)</label><input id="sessionpasses_${esc(user)}" type="number" min="1" max="20" value="${Number(s.active_session_max_passes||8)}"></div>
        <div><label>Engage clips / pass (1-20)</label><input id="engageclips_${esc(user)}" type="number" min="1" max="20" value="${Number(s.engage_clips_per_pass||12)}"></div>
        <div><label>Engage discovery scrolls (1-20)</label><input id="engagescrolls_${esc(user)}" type="number" min="1" max="20" value="${Number(s.engage_scroll_steps||8)}"></div>
        <div><label>Engage Like %</label><input id="engagelike_${esc(user)}" type="number" min="0" max="100" value="${Number(s.engage_like_percent??75)}"></div>
        <div><label>Engage Repost %</label><input id="engagerepost_${esc(user)}" type="number" min="0" max="100" value="${Number(s.engage_repost_percent??75)}"></div>
        <div><label>Engage Comment %</label><input id="engagecomment_${esc(user)}" type="number" min="0" max="100" value="${Number(s.engage_comment_percent??10)}"></div>
        <div><label>Engage Follow-author %</label><input id="engagefollow_${esc(user)}" type="number" min="0" max="100" value="${Number(s.engage_follow_percent??25)}"></div>
        <div><label>Video frames to watch</label><input id="frames_${esc(user)}" type="number" min="3" max="9" value="${Number(s.video_frames||5)}"></div>
        <div><label>Writes / rolling window</label><input id="maxwrites_${esc(user)}" type="number" min="1" max="30" value="${Number(s.max_writes_per_window||8)}"></div>
        <div><label>Rolling window seconds</label><input id="window_${esc(user)}" type="number" min="60" max="7200" value="${Number(s.write_window_seconds||900)}"></div>
        <div><label>Min write gap seconds</label><input id="writegap_${esc(user)}" type="number" min="8" max="600" value="${Number(s.write_min_gap_seconds||22)}"></div>
        <div><label>Upload cooldown seconds</label><input id="uploadgap_${esc(user)}" type="number" min="300" max="21600" value="${Number(s.upload_cooldown_seconds||1800)}"></div>
      </div>

      <label>Target accounts (comma/newline separated)</label>
      <textarea id="targets_${esc(user)}">${esc(targets)}</textarea>
      <label>Target hashtags (comma/newline separated)</label>
      <textarea id="tags_${esc(user)}">${esc(tags)}</textarea>

      <label>Persona / attitude prompt</label>
      <textarea id="persona_${esc(user)}" style="min-height:130px">${esc(s.persona_prompt||"")}</textarea>
      <label>Caption behavior prompt</label>
      <textarea id="capprompt_${esc(user)}">${esc(s.caption_prompt||"")}</textarea>
      <label>DM behavior prompt</label>
      <textarea id="dmprompt_${esc(user)}">${esc(s.dm_prompt||"")}</textarea>
      <label>Comment behavior prompt</label>
      <textarea id="commentprompt_${esc(user)}">${esc(s.comment_prompt||"")}</textarea>

      <div class="row">
        <label><input id="reqvision_${esc(user)}" type="checkbox" style="width:auto" ${s.require_video_vision?"checked":""}> Require multi-frame vision for videos</label>
      </div>
    </div>

    <div class="section">
      <b>Manual actions</b>
      <div class="row">
        <button class="good" ${connected ? "":"disabled"} onclick="burstNow('${esc(user)}')">⚡ Burst Now (1-2 min)</button>
        <button class="good" ${connected ? "":"disabled"} onclick="queueTask('${esc(user)}','reels_demo')">🎞️ Reels Demo</button>
        <button ${connected ? "":"disabled"} onclick="queueTask('${esc(user)}','repost')">Upload/Repost</button>
        <button ${connected ? "":"disabled"} onclick="clearUploadCooldown('${esc(user)}')">Clear Upload Cooldown</button>
        <button ${connected ? "":"disabled"} onclick="queueTask('${esc(user)}','comments')">Reply Comments</button>
        <button ${connected ? "":"disabled"} onclick="queueTask('${esc(user)}','networking')">Network</button>
        <button ${connected ? "":"disabled"} onclick="queueTask('${esc(user)}','hashtags')">Hashtags</button>
      </div>
    </div>

    <div class="section">
      <b>Runtime log</b>
      <div class="logs">${logs || "<div>No runtime events yet.</div>"}</div>
    </div>
  </div>`;
}



async function clearUploadCooldown(user){
  const ok=window.confirm(
    `Clear only the upload cooldown for @${user}? ` +
    `Use this only when a previous false-positive upload was recorded. ` +
    `Safety backoffs and other write pacing are not cleared.`
  );
  if(!ok) return;

  try{
    await api("clear_upload_cooldown",{username:user});
    toast(`Upload cooldown cleared for @${user}.`);
    await refreshMetrics();
  }catch(e){toast(e.message,true)}
}

function renderRecentPosts(items){
  const box=document.getElementById("recentPosts");
  if(!box) return;
  if(!items || !items.length){
    box.innerHTML='<div class="small">No recorded uploads yet.</div>';
    return;
  }
  box.innerHTML=items.map((p,i)=>`
    <div style="border-bottom:1px solid var(--border);padding:10px 0">
      <div><b>@${esc(p.account||"")}</b> <span class="small">${esc(p.at||"")}</span></div>
      <div class="small" style="margin:4px 0">${esc(p.caption||"")}</div>
      <div class="small" style="word-break:break-all">${esc(p.folder||"")}</div>
      <div class="row">
        <button onclick='openPostFolder(${JSON.stringify(p.folder||"")})'>Open Folder</button>
        ${p.permalink ? `<button onclick='window.open(${JSON.stringify(p.permalink)},"_blank")'>Open Post</button>` : ""}
      </div>
    </div>
  `).join("");
}

async function openPostFolder(folder){
  try{
    await api("open_folder",{folder});
    toast("Opened source folder.");
  }catch(e){toast(e.message,true)}
}


function renderMediaRoot(media){
  const input=document.getElementById("mediaRootInput");
  const badge=document.getElementById("mediaRootBadge");
  const status=document.getElementById("mediaRootStatus");
  if(!input || !badge || !status) return;

  const m=media || {};
  if(!editing){
    input.value=m.path || "";
  }

  if(m.exists && m.is_dir){
    badge.textContent="Ready";
    badge.className="badge ok";
    status.textContent=
      `${m.path || ""} — ${Number(m.child_folders || 0)} immediate subfolder(s) detected.`;
  }else{
    badge.textContent="Missing";
    badge.className="badge bad";
    status.textContent=
      `${m.path || "(not configured)"} — folder not found.`;
  }
}

async function saveMediaRoot(){
  const input=document.getElementById("mediaRootInput");
  const path=input ? input.value.trim() : "";
  if(!path){
    toast("Enter a media folder path first.",true);
    return;
  }

  try{
    const result=await api("set_media_root",{path});
    editing=false;
    renderMediaRoot(result.media || {});
    await refreshNow(true);
    toast(`Media folder saved: ${(result.media && result.media.path) || path}`);
  }catch(e){
    toast(e.message,true);
  }
}

async function browseMediaRoot(){
  try{
    toast("Opening local folder picker...");
    const result=await api("choose_media_root",{});
    editing=false;

    if(result.cancelled){
      await refreshNow(true);
      toast("Folder selection cancelled.");
      return;
    }

    renderMediaRoot(result.media || {});
    await refreshNow(true);
    toast(`Media folder saved: ${(result.media && result.media.path) || ""}`);
  }catch(e){
    toast(e.message,true);
  }
}

async function openMediaRoot(){
  try{
    await api("open_media_root",{});
    toast("Opened configured media folder.");
  }catch(e){
    toast(e.message,true);
  }
}


function render(data){
  renderMediaRoot((data && data.media) || {});
  latestData=(data && data.accounts) ? data.accounts : (data || {});
  const grid=document.getElementById("grid");
  grid.innerHTML=Object.entries(latestData)
    .map(([u,a])=>cardHtml(u,a))
    .join("");
  for(const [u,a] of Object.entries(latestData)){
    if(a && a.control && a.control.settings){
      const s=a.control.settings;
      behaviorSignatures[u]=JSON.stringify({
        enable_posts:!!s.enable_posts,enable_follow:!!s.enable_follow,
        enable_engage:!!s.enable_engage,enable_dms:!!s.enable_dms,
        enable_comments:!!s.enable_comments,follow_source:s.follow_source,
        caption_char_limit:Number(s.caption_char_limit),reply_char_limit:Number(s.reply_char_limit),
        follow_limit:Number(s.follow_limit),video_frames:Number(s.video_frames),
        max_writes_per_window:Number(s.max_writes_per_window),write_window_seconds:Number(s.write_window_seconds),
        write_min_gap_seconds:Number(s.write_min_gap_seconds),upload_cooldown_seconds:Number(s.upload_cooldown_seconds),
        target_accounts:s.target_accounts||[],target_hashtags:s.target_hashtags||[],
        persona_prompt:s.persona_prompt||'',caption_prompt:s.caption_prompt||'',
        dm_prompt:s.dm_prompt||'',comment_prompt:s.comment_prompt||'',
        require_video_vision:!!s.require_video_vision
      });
    }
  }
}

async function api(action,payload={}){
  const r=await fetch("/api/control",{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action,...payload})
  });
  const data=await r.json().catch(()=>({ok:false,error:"Invalid server response"}));
  if(!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

async function refreshNow(force=false){
  if(refreshBusy) return;
  const auto=document.getElementById("autoRefresh").checked;

  // Never rebuild cards while a user is typing, even if Auto Refresh is on.
  if(!force && (!auto || editing)){
    document.getElementById("refreshState").textContent=
      editing ? "Refresh paused while typing" : "Auto refresh off";
    return;
  }

  refreshBusy=true;
  try{
    if(!editing) await autoSaveVisibleBehavior();
    const r=await fetch("/api/metrics",{cache:"no-store"});
    const data=await r.json();
    render(data);
    renderRecentPosts((data && data.recent_posts) || []);
    document.getElementById("refreshState").textContent=
      "Updated " + new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById("refreshState").textContent="Refresh error";
  }finally{
    refreshBusy=false;
  }
}


async function addInstagramAccount(){
  const username=prompt("Instagram username to add (maximum 3 configured accounts):");
  if(!username) return;
  try{
    const result=await api("add_account",{username});
    await refreshNow(true);
    toast(result.existing ? `@${result.username} is already configured.` : `Added @${result.username}.`);
  }catch(e){toast(e.message,true)}
}

async function removeInstagramAccount(user){
  if(!confirm(`Remove @${user} from the Control Head?\n\nIts saved session/history/settings will be kept locally so re-adding the same username can reuse them.`)) return;
  try{
    await api("remove_account",{username:user});
    await refreshNow(true);
    toast(`Removed @${user} from configured accounts.`);
  }catch(e){toast(e.message,true)}
}

async function quickLogin(user){
  try{
    toast(`Connecting @${user} from saved session...`);
    await api("quick_login",{username:user});
    await refreshNow(true);
    toast(`@${user} connected.`);
  }catch(e){toast(e.message,true)}
}

async function browserLogin(user){
  try{
    toast(`Opening Instagram Browser Login for @${user}... Complete login and any Save login info prompt in Chromium.`);
    const result = await api("browser_login",{username:user});
    editing=false;
    await refreshNow(true);
    if(result && result.private_api_ready === false){
      toast(result.message || `@${user} browser login saved; private API automation is still blocked.`, true);
    }else{
      toast((result && result.message) || `@${user} browser login saved and automation is ready.`);
    }
  }catch(e){
    await refreshNow(true);
    toast(e.message,true);
  }
}

async function passwordLogin(user){
  const pw=document.getElementById(`pw_${user}`).value;
  const otp=document.getElementById(`otp_${user}`).value;
  try{
    toast(`Logging in @${user}...`);
    await api("password_login",{username:user,password:pw,verification_code:otp});
    document.getElementById(`pw_${user}`).value="";
    document.getElementById(`otp_${user}`).value="";
    editing=false;
    await refreshNow(true);
    toast(`@${user} logged in; saved session updated.`);
  }catch(e){toast(e.message,true)}
}

async function disconnectAccount(user){
  try{
    await api("disconnect",{username:user});
    await refreshNow(true);
    toast(`@${user} disconnected; saved session kept.`);
  }catch(e){toast(e.message,true)}
}

async function fullLogout(user){
  if(!confirm(`Full Logout @${user}? This deletes its saved session.`)) return;
  try{
    await api("full_logout",{username:user});
    await refreshNow(true);
    toast(`@${user} fully logged out; saved session deleted.`);
  }catch(e){toast(e.message,true)}
}

async function pauseAccount(user,pause){
  try{
    await api("pause",{username:user,pause});
    await refreshNow(true);
  }catch(e){toast(e.message,true)}
}

async function queueTask(user,task){
  try{
    await api("queue_task",{username:user,task});
    await refreshNow(true);
    toast(`Queued ${task} for @${user}.`);
  }catch(e){toast(e.message,true)}
}


function splitList(v){
  return String(v||"").split(/[\n,]+/).map(x=>x.trim()).filter(Boolean);
}


async function setPaceMode(user,mode){
  try{
    await autoSaveBehavior(user,true);
    await api("set_pace_mode",{username:user,mode});
    toast(`@${user}: ${mode==="overnight" ? "Overnight Pace" : "Normal Pace"} enabled.`);
    await refreshMetrics();
  }catch(e){toast(e.message,true)}
}

async function burstNow(user){
  try{
    await autoSaveBehavior(user,true);
    const r=await api("burst_now",{username:user});
    toast(`Burst started for @${user} (~${Math.ceil(Number(r.burst_seconds||0)/60)} min).`);
    await refreshMetrics();
  }catch(e){toast(e.message,true)}
}

function collectBehavior(user){
  return {
    enable_posts:document.getElementById(`posts_${user}`).checked,
    enable_follow:document.getElementById(`follow_${user}`).checked,
    enable_engage:document.getElementById(`engage_${user}`).checked,
    enable_dms:document.getElementById(`dms_${user}`).checked,
    enable_comments:document.getElementById(`comments_${user}`).checked,
    follow_source:document.getElementById(`fsource_${user}`).value,
    caption_char_limit:Number(document.getElementById(`caplim_${user}`).value),
    reply_char_limit:Number(document.getElementById(`replim_${user}`).value),
    follow_limit:Number(document.getElementById(`followlim_${user}`).value),
    auto_follow_batch_max:Number(document.getElementById(`autofollowmax_${user}`).value),
    active_session_max_passes:Number(document.getElementById(`sessionpasses_${user}`).value),
    engage_clips_per_pass:Number(document.getElementById(`engageclips_${user}`).value),
    engage_scroll_steps:Number(document.getElementById(`engagescrolls_${user}`).value),
    engage_like_percent:Number(document.getElementById(`engagelike_${user}`).value),
    engage_repost_percent:Number(document.getElementById(`engagerepost_${user}`).value),
    engage_comment_percent:Number(document.getElementById(`engagecomment_${user}`).value),
    engage_follow_percent:Number(document.getElementById(`engagefollow_${user}`).value),
    pace_mode:(latestData.accounts?.[user]?.control?.settings?.pace_mode || "normal"),
    video_frames:Number(document.getElementById(`frames_${user}`).value),
    max_writes_per_window:Number(document.getElementById(`maxwrites_${user}`).value),
    write_window_seconds:Number(document.getElementById(`window_${user}`).value),
    write_min_gap_seconds:Number(document.getElementById(`writegap_${user}`).value),
    upload_cooldown_seconds:Number(document.getElementById(`uploadgap_${user}`).value),
    target_accounts:splitList(document.getElementById(`targets_${user}`).value),
    target_hashtags:splitList(document.getElementById(`tags_${user}`).value),
    persona_prompt:document.getElementById(`persona_${user}`).value,
    caption_prompt:document.getElementById(`capprompt_${user}`).value,
    dm_prompt:document.getElementById(`dmprompt_${user}`).value,
    comment_prompt:document.getElementById(`commentprompt_${user}`).value,
    require_video_vision:document.getElementById(`reqvision_${user}`).checked,
  };
}

const behaviorSignatures={};

async function autoSaveBehavior(user,quiet=true){
  const card=document.querySelector(`.card[data-user="${CSS.escape(user)}"]`);
  if(!card) return;
  const settings=collectBehavior(user);
  const sig=JSON.stringify(settings);
  if(behaviorSignatures[user]===sig) return;
  try{
    await api("save_settings",{username:user,settings});
    behaviorSignatures[user]=sig;
    if(!quiet) toast(`Behavior updated for @${user}.`);
  }catch(e){
    if(!quiet) toast(e.message,true);
  }
}

async function autoSaveVisibleBehavior(){
  const users=[...document.querySelectorAll(".card[data-user]")].map(x=>x.dataset.user);
  await Promise.all(users.map(u=>autoSaveBehavior(u,true)));
}

document.getElementById("autoRefresh").addEventListener("change",()=>{
  refreshNow(true);
});

// Poll every 5 seconds, but refreshNow() refuses to redraw while typing or
// when Auto Refresh is switched off.
setInterval(()=>refreshNow(false), CONTROL_HEAD_REFRESH_SECONDS * 1000);
refreshNow(true);
</script>
</body>
</html>"""

        raw = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path != "/api/control":
            self.send_error(404)
            return

        try:
            body = _read_json_body(self)
            action = str(body.get("action", "")).strip()
            username = str(body.get("username", "")).strip().lstrip("@")

            if action == "add_account":
                result = _control_add_account(username)

            elif action == "remove_account":
                result = _control_remove_account(username)

            elif action == "quick_login":
                result = _control_quick_login(username)

            elif action == "browser_login":
                result = _control_browser_login(username)

            elif action == "password_login":
                result = _control_password_login(
                    username,
                    body.get("password", ""),
                    body.get("verification_code", ""),
                )

            elif action == "disconnect":
                result = _control_disconnect(username)

            elif action == "full_logout":
                result = _control_full_logout(username)

            elif action == "pause":
                result = _control_pause(
                    username, bool(body.get("pause", True))
                )

            elif action == "queue_task":
                result = _control_queue_task(
                    username, str(body.get("task", ""))
                )

            elif action == "clear_upload_cooldown":
                result = _control_clear_upload_cooldown(username)

            elif action == "set_pace_mode":
                result = _control_set_pace_mode(
                    username,
                    str(body.get("mode", "normal")),
                )

            elif action == "burst_now":
                result = _control_burst_now(username)

            elif action == "save_settings":
                result = _control_save_settings(
                    username,
                    body.get("settings", {}),
                )

            elif action == "set_media_root":
                result = _control_set_media_root(
                    body.get("path", "")
                )

            elif action == "choose_media_root":
                result = _control_choose_media_root()

            elif action == "open_media_root":
                result = _control_open_media_root()

            elif action == "open_folder":
                result = open_local_folder(body.get("folder", ""))

            else:
                raise ValueError(f"Unknown Control Head action: {action}")

            _json_response(self, 200, result)

        except Exception as exc:
            _json_response(
                self,
                400,
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                },
            )


def _active_roster():
    """
    Return enabled roster entries, optionally pinned to exactly one account with:
        --account=username
    or:
        IG_ACCOUNT=username
    """
    roster = {
        user: conf
        for user, conf in FAN_ROSTER.items()
        if isinstance(conf, dict) and conf.get("enabled", True)
    }

    if not RUN_ACCOUNT:
        return roster

    wanted = RUN_ACCOUNT.casefold()
    match = next(
        (user for user in roster if user.casefold() == wanted),
        None,
    )
    if match is None:
        print(f"❌ Requested account @{RUN_ACCOUNT} is not enabled in FAN_ROSTER.")
        if roster:
            print(
                "Enabled roster accounts: "
                + ", ".join(f"@{user}" for user in roster)
            )
        return {}

    return {match: roster[match]}



def _print_auth_configuration(active_roster):
    parts = []
    for user, conf in active_roster.items():
        if _credential(conf, "password"):
            mode = "credential fallback available"
        elif (DOWNLOAD_ROOT / conf["session_file"]).exists():
            mode = "saved session only"
        else:
            mode = "manual login required"
        parts.append(f"@{user}: {mode}")
    print("Authentication config:")
    for part in parts:
        print("  - " + part)


def _all_accounts_currently_paused(active_roster):
    now = datetime.now()
    active_names = list(active_roster.keys())
    if not active_names:
        return False, None
    deadlines = []
    for user in active_names:
        until = ACCOUNT_COOLDOWNS.get(user)
        if until is None or now >= until:
            return False, None
        deadlines.append(until)
    return True, min(deadlines) if deadlines else None


def responsive_idle_wait(username, conf, seconds):
    """Wait between major workflows while still checking DMs periodically."""
    deadline = time.monotonic() + max(0, seconds)
    next_dm = time.monotonic()
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_dm:
            cl = CLIENT_CACHE.get(username)
            if cl is not None:
                history = load_json(conf["history_file"], {
                    "posted_ids": [], "replied_dms": [], "replied_comments": [],
                    "liked_medias": [], "commented_medias": [], "followed_users": [],
                    "blocked_or_missing": [], "competitor_pool": [],
                    "followers_next_max_id": None, "following_next_max_id": None,
                })
                try:
                    count = handle_direct_messages(cl, history, username, max_replies=3)
                    if count:
                        save_json(conf["history_file"], history)
                except FeedbackRequired:
                    save_json(conf["history_file"], history)
                    raise
                except Exception as exc:
                    update_account_metric(username, "add_history", value=f"⚠️ DM heartbeat error: {str(exc)[:70]}")
            next_dm = now + DM_POLL_SECONDS
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(5.0, remaining))



def poll_all_cached_dms(active_roster, force=False, debug=False):
    now = time.monotonic()

    for username, conf in active_roster.items():
        if username in CONTROL_DISCONNECTED_ACCOUNTS:
            continue
        if username in CONTROL_PAUSED_ACCOUNTS:
            continue
        if not get_account_control_settings(username, conf).get("enable_dms", False):
            continue

        cl = CLIENT_CACHE.get(username)
        if cl is None:
            continue

        last_poll = LAST_DM_POLL.get(username, 0.0)
        if not force and now - last_poll < DM_POLL_SECONDS:
            continue

        LAST_DM_POLL[username] = now
        history = load_json(conf["history_file"], {
            "posted_ids": [], "replied_dms": [], "replied_comments": [],
            "liked_medias": [], "commented_medias": [], "followed_users": [],
            "blocked_or_missing": [], "competitor_pool": [],
            "followers_next_max_id": None, "following_next_max_id": None,
        })

        try:
            replied = handle_direct_messages(
                cl, history, username, max_replies=3, debug=debug
            )
            if replied:
                save_json(conf["history_file"], history)
        except FeedbackRequired as exc:
            apply_account_safety_backoff(
                username,
                f"Instagram FeedbackRequired during DM listener: {exc}",
                level="restricted",
            )
        except Exception as exc:
            observe_platform_signal(username, exc, action="DM listener")
            print(
                f"⚠️ @{username} DM heartbeat exception: "
                f"{type(exc).__name__}: {exc}"
            )
            update_account_metric(
                username,
                "add_history",
                value=(
                    f"⚠️ DM heartbeat exception: "
                    f"{type(exc).__name__}: {str(exc)[:80]}"
                ),
            )




ACCOUNT_WORKFLOW_THREADS = {}
ACCOUNT_WORKFLOW_THREADS_LOCK = threading.RLock()


def _background_account_workflow(username, conf, next_workflow_at):
    update_account_metric(username,"add_history",value="🧵 Worker started.")
    forced_task = NEXT_TASK_OVERRIDE.get(username)
    try:
        if username in BROWSER_NATIVE_ACCOUNTS and not _browser_live_port_open(username):
            _mark_browser_login_needed(username,"the live Chromium window was closed before the worker started")
            result="disconnected"
        else:
            settings=get_account_control_settings(username,conf)
            need_media=forced_task=="repost" or (forced_task is None and bool(settings.get("enable_posts",False)))
            if need_media:
                update_account_metric(username,"add_history",value="🗂️ Worker checking local media library...")
                folder_pool=discover_local_media_folders()
                update_account_metric(username,"add_history",value=f"🗂️ Media library ready: {len(folder_pool)} folder(s).")
            else:
                folder_pool=[]
            result=run_profile_workflow(username,conf,folder_pool)
    except FeedbackRequired as exc:
        apply_account_safety_backoff(username,f"Instagram FeedbackRequired during workflow: {exc}",level="restricted"); result="throttled"
    except Exception as exc:
        observe_platform_signal(username,exc,action="workflow")
        update_account_metric(username,"add_history",value=f"❌ Workflow error: {type(exc).__name__}: {str(exc)[:220]}"); result="error"
    delay, delay_reason = _next_overnight_auto_delay(
        username,
        result,
        manual_run=bool(forced_task),
    )
    update_account_metric(
        username,
        "add_history",
        value=(
            f"🧵 Worker finished with result={result}; "
            f"next Auto eligibility in ~{delay}s "
            f"({delay_reason})."
        ),
    )
    with ACCOUNT_WORKFLOW_THREADS_LOCK:
        next_workflow_at[username]=time.monotonic()+delay; ACCOUNT_WORKFLOW_THREADS.pop(username,None)





def _start_background_account_workflow(username, conf, next_workflow_at):
    with ACCOUNT_WORKFLOW_THREADS_LOCK:
        existing = ACCOUNT_WORKFLOW_THREADS.get(username)
        if existing and existing.is_alive():
            return False
        thread = threading.Thread(
            target=_background_account_workflow,
            args=(username, conf, next_workflow_at),
            daemon=True,
            name=f"instagram-workflow-{username}",
        )
        ACCOUNT_WORKFLOW_THREADS[username] = thread
        thread.start()
        return True


def main():
    global CONTROL_ROSTER

    ensure_data_root()
    active_roster = _active_roster()

    # Hard dashboard cap. An empty roster is valid: the Control Head still
    # starts so the first account can be added from the browser.
    active_roster = dict(list(active_roster.items())[:3])
    CONTROL_ROSTER = active_roster

    CLIENT_CACHE.clear()
    BROWSER_NATIVE_ACCOUNTS.clear()
    BROWSER_LOGIN_IN_PROGRESS.clear()
    LIVE_CDP_CONTEXT_IDS.clear()
    BROWSER_HANDLE_BY_CONTEXT.clear()
    CONTROL_DISCONNECTED_ACCOUNTS.clear()
    CONTROL_DISCONNECTED_ACCOUNTS.update(active_roster.keys())
    CONTROL_PAUSED_ACCOUNTS.clear()
    CONTROL_PAUSED_ACCOUNTS.update(active_roster.keys())
    CONTROL_FORCE_RUN.clear()
    LAST_DM_POLL.clear()
    BURST_UNTIL.clear()
    LAST_BROWSER_DM_CHECK.clear()
    NEXT_BROWSER_DM_CHECK.clear()
    LAST_BROWSER_COMMENT_CHECK.clear()
    NEXT_BROWSER_COMMENT_CHECK.clear()

    for user in active_roster:
        AUTH_EVENT[user] = "disconnected"
        update_account_metric(user, "status", status="Disconnected / Auto Off")

    print(f"Instagram Control Head — Caption + Hashtag Fix: http://{IG_HOST}:{IG_PORT}")
    print(f"Instagram state root: {DOWNLOAD_ROOT}")
    print(f"Instagram media root: {get_media_root()}")
    print("Configured accounts: " + (", ".join(f"@{u}" for u in active_roster) if active_roster else "(none yet — add up to 3 in the Control Head)"))
    print("Dashboard account cap: 3")
    print("Boot mode: DISCONNECTED / AUTO OFF")
    print(f"Daily follow hard cap: {DAILY_FOLLOW_HARD_CAP} attempts per local day")
    print(
        "Auto cadence: Burst = 1-2 min active session; Normal single-pass; Overnight uses "
        f"{ACTIVE_SESSION_MIN_SECONDS//60}-{ACTIVE_SESSION_MAX_SECONDS//60} min active sessions, "
        f"{OVERNIGHT_REST_MIN_SECONDS//60}-{OVERNIGHT_REST_MAX_SECONDS//60} min ordinary rests, "
        f"{OVERNIGHT_LONG_REST_MIN_SECONDS//60}-{OVERNIGHT_LONG_REST_MAX_SECONDS//60} min periodic long rests; "
        f"Auto Follow batches {AUTO_FOLLOW_BATCH_MIN}-{AUTO_FOLLOW_BATCH_MAX} visible rows max; "
        "active-session pass count is configurable per account in the hub."
    )

    server = ThreadingHTTPServer((IG_HOST, IG_PORT), DashboardAPIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    next_workflow_at = {
        user: time.monotonic() + random.randint(15, 35)
        for user in active_roster
    }

    try:
        while True:
            # Listener respects Auto Off and the DMs checkbox.
            poll_all_cached_dms(CONTROL_ROSTER)

            now_mono = time.monotonic()
            for current_user, conf in list(CONTROL_ROSTER.items()):
                if current_user in BROWSER_LOGIN_IN_PROGRESS:
                    continue

                if (
                    current_user in BROWSER_NATIVE_ACCOUNTS
                    and not _browser_live_port_open(current_user)
                ):
                    _mark_browser_login_needed(
                        current_user,
                        "the live Chromium window was closed manually",
                    )
                    continue

                connected = (
                    (
                        current_user in CLIENT_CACHE
                        and current_user not in CONTROL_DISCONNECTED_ACCOUNTS
                    )
                    or (current_user in BROWSER_NATIVE_ACCOUNTS and _browser_live_port_open(current_user))
                )
                if not connected:
                    continue

                safety = get_account_safety_state(current_user)
                if safety["active"]:
                    CONTROL_PAUSED_ACCOUNTS.add(current_user)
                    continue

                forced = current_user in CONTROL_FORCE_RUN

                # Browser Mode manual tasks are independent of private-API auth
                # cooldowns. Genuine safety backoff was already checked above.
                cooldown = ACCOUNT_COOLDOWNS.get(current_user)
                if (
                    cooldown
                    and datetime.now() < cooldown
                    and not (
                        current_user in BROWSER_NATIVE_ACCOUNTS
                        and forced
                    )
                ):
                    continue
                auto_on = current_user not in CONTROL_PAUSED_ACCOUNTS
                if not auto_on and not forced:
                    continue

                if current_user not in next_workflow_at:
                    next_workflow_at[current_user] = now_mono + random.randint(5, 15)

                due = now_mono >= next_workflow_at[current_user]
                if not forced and not due:
                    continue

                if forced:
                    CONTROL_FORCE_RUN.discard(current_user)

                # Start every due account independently. Up to 3 accounts can be
                # working concurrently; shared write pacing still serializes risky
                # outbound writes enough to avoid simultaneous bursts.
                if _start_background_account_workflow(
                    current_user, conf, next_workflow_at
                ):
                    next_workflow_at[current_user] = float("inf")

            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\nControl Head stopped. Saved Instagram sessions were left untouched.")
    finally:
        try:
            server.shutdown()
        except Exception:
            pass





if __name__ == "__main__":
    main()
