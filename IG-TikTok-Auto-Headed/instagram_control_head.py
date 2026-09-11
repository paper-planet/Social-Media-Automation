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
MEDIA_ROOT = Path(os.environ.get("IG_MEDIA_ROOT", str(Path.home() / "social-media-pool"))).expanduser()
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
AUTH_RETRY_AFTER = {}  # username -> datetime; prevents rapid login loops

# One-account instances can keep DMs responsive without making posting/actions frequent.
DM_POLL_SECONDS = max(60, int(os.environ.get("IG_DM_POLL_SECONDS", "90")))
WORKFLOW_SLEEP_MIN = max(DM_POLL_SECONDS, int(os.environ.get("IG_WORKFLOW_SLEEP_MIN", "420")))
WORKFLOW_SLEEP_MAX = max(WORKFLOW_SLEEP_MIN, int(os.environ.get("IG_WORKFLOW_SLEEP_MAX", "1200")))
OLLAMA_MODEL = os.environ.get("IG_OLLAMA_MODEL", "llama3.1").strip() or "llama3.1"
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
        if status: db[username]["status"] = status
        if key in ["total_posts", "total_follows", "total_likes"]: db[username][key] += increment
        elif key == "cooldown_until": db[username][key] = str(value)
        elif key == "add_history":
            db[username]["history_log"].insert(0, f"[{datetime.now().strftime('%H:%M:%S')}] {value}")
            db[username]["history_log"] = db[username]["history_log"][:15]
        elif key == "sync_followers":
            today = datetime.now().strftime("%Y-%m-%d")
            timeline = db[username]["growth_timeline"]
            if timeline and timeline[-1]["date"] == today: timeline[-1]["followers"] = value
            else:
                timeline.append({"date": today, "followers": value})
                if len(timeline) > 30: timeline.pop(0)
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


def discover_local_media_folders():
    all_folders = []
    if not MEDIA_ROOT.exists():
        return all_folders
    try:
        for item in MEDIA_ROOT.iterdir():
            if not item.is_dir():
                continue
            parts = item.name.split('_', 2)
            source = parts[1] if len(parts) > 1 and parts[1] else item.name
            all_folders.append({"id": item.name, "path": item, "source": source})
    except OSError as exc:
        print(f"⚠️ Media folder scan failed: {exc}")
    return all_folders




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



def wait_for_write_slot(username, action_type="write", max_wait=180):
    start = time.monotonic()
    last_reason = ""

    while time.monotonic() - start < max_wait:
        allowed, reason = account_writes_allowed(username)
        if not allowed:
            update_account_metric(
                username, "add_history",
                value=f"🛡️ {action_type} blocked: {reason}"
            )
            return False

        ok, reason = _reserve_shared_write_slot(username, action_type)
        if ok:
            return True
        last_reason = reason

        if "Safety Backoff" in reason or "Account cooldown" in reason:
            break
        time.sleep(2.0)

    update_account_metric(
        username,
        "add_history",
        value=f"⏳ Deferred {action_type}: {last_reason or 'shared pacing budget'}"
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
    path = _browser_profile_dir(username)
    return path.exists() and path.is_dir()


def _activate_browser_native_mode(username: str, reason: str = "") -> None:
    username = str(username or "").strip().lstrip("@")
    with CONTROL_LOCK:
        BROWSER_NATIVE_ACCOUNTS.add(username)
        CLIENT_CACHE.pop(username, None)
        CONTROL_DISCONNECTED_ACCOUNTS.discard(username)
        CONTROL_PAUSED_ACCOUNTS.add(username)
        CONTROL_FORCE_RUN.discard(username)
        AUTH_EVENT[username] = "browser_native"

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


def _browser_check_ready(page, context, username: str) -> None:
    if not _browser_context_logged_in(context):
        raise RuntimeError(
            "Saved Instagram browser profile is no longer logged in. "
            "Use Browser Login again."
        )

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
            "Use Browser Login and complete the prompt manually."
        )


def _browser_launch(p, username: str, *, headed: bool | None = None):
    if headed is None:
        headed = os.environ.get(
            "IG_BROWSER_AUTOMATION_HEADED", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}

    return p.chromium.launch_persistent_context(
        str(_browser_profile_dir(username)),
        headless=not headed,
        viewport={"width": 1280, "height": 900},
    )


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


def _browser_follow_network(username, history, config, manual=False) -> int:
    settings = get_account_control_settings(username, config)
    if not settings.get("enable_follow", False):
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

    limit = min(
        int(settings.get("follow_limit", 1)),
        int(settings.get("max_writes_per_workflow", MAX_WRITES_PER_WORKFLOW)),
    )
    limit = max(1, limit)

    update_account_metric(
        username,
        "add_history",
        value=f"🌐 Browser Follow: @{target} {source}; target={limit}",
    )

    followed = 0
    attempted = set()

    with sync_playwright() as p:
        context = _browser_launch(p, username)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(
                f"https://www.instagram.com/{target}/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(1300)
            _browser_check_ready(page, context, username)

            href = f"/{target}/{source}/"
            trigger = page.locator(f"a[href='{href}']").first
            if not trigger.count():
                trigger = page.locator(
                    f"a[href*='/{target}/{source}/']"
                ).first
            if not trigger.count():
                _browser_click_text_button(
                    page,
                    rf"^{source}$",
                )
            else:
                trigger.click(timeout=5000)

            page.wait_for_timeout(1200)
            _browser_check_ready(page, context, username)

            dialog = page.locator("[role='dialog']").last
            root = dialog if dialog.count() else page

            idle_rounds = 0
            while followed < limit and idle_rounds < 8:
                buttons = root.get_by_role(
                    "button",
                    name=re.compile(r"^Follow$", re.I),
                )
                count = buttons.count()

                candidate = None
                candidate_name = ""

                for i in range(min(count, 30)):
                    button = buttons.nth(i)
                    try:
                        if not button.is_visible(timeout=200):
                            continue
                    except Exception:
                        continue

                    row = button.locator("xpath=ancestor::div[.//a][1]")
                    hrefs = []
                    try:
                        hrefs = row.locator("a[href^='/']").evaluate_all(
                            """els => els.map(e => e.getAttribute('href') || '')"""
                        )
                    except Exception:
                        pass

                    for candidate_href in hrefs:
                        match = re.fullmatch(r"/([^/?#]+)/?", str(candidate_href))
                        if match:
                            possible = match.group(1)
                            if possible.lower() not in {
                                target.lower(), "accounts", "explore"
                            }:
                                candidate_name = possible
                                break

                    key = candidate_name or f"row_{i}"
                    if key in attempted:
                        continue
                    attempted.add(key)
                    candidate = button
                    break

                if candidate is None:
                    idle_rounds += 1
                    try:
                        root.evaluate(
                            "(el) => el.scrollBy(0, Math.max(600, el.clientHeight * 0.8))"
                        )
                    except Exception:
                        try:
                            root.locator("div").last.scroll_into_view_if_needed(
                                timeout=1000
                            )
                        except Exception:
                            pass
                    page.wait_for_timeout(900)
                    continue

                idle_rounds = 0

                if not wait_for_write_slot(username, "follow", max_wait=180):
                    break

                try:
                    candidate.click(timeout=5000)
                except Exception:
                    candidate.evaluate("(el) => el.click()")

                page.wait_for_timeout(900)

                problem = _browser_page_problem(page)
                if problem:
                    apply_account_safety_backoff(
                        username,
                        f"Instagram Web follow restriction/challenge: {problem}",
                        level="restricted",
                        hours=4,
                    )
                    break

                # Re-read visible row/button state. If the exact Follow button
                # is gone or changed to Following/Requested, count it.
                confirmed = False
                try:
                    txt = re.sub(
                        r"\s+",
                        " ",
                        candidate.inner_text(timeout=500) or "",
                    ).strip()
                    confirmed = bool(
                        re.fullmatch(
                            r"(Following|Requested)",
                            txt,
                            re.I,
                        )
                    )
                except Exception:
                    # A detached button usually means the row rerendered.
                    confirmed = True

                if not confirmed:
                    # Do not count ambiguous UI as success.
                    update_account_metric(
                        username,
                        "add_history",
                        value=(
                            f"⚠️ Browser Follow for "
                            f"@{candidate_name or 'candidate'} was not confirmed."
                        ),
                    )
                    continue

                record_write(username, "follow")
                followed += 1
                update_account_metric(
                    username,
                    "total_follows",
                    increment=1,
                )
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        f"👤 Browser Follow confirmed: "
                        f"@{candidate_name or 'candidate'} ({followed}/{limit})"
                    ),
                )

        finally:
            try:
                context.close()
            except Exception:
                pass

    return followed


def _browser_engage_hashtag(username, history, config) -> int:
    settings = get_account_control_settings(username, config)
    if not settings.get("enable_engage", False):
        return 0

    tags = _normalize_hashtag_list(
        settings["target_hashtags"]
        or list(config.get("target_hashtags") or [])
    )
    if not tags:
        update_account_metric(
            username,
            "add_history",
            value="⚠️ No valid target hashtags configured.",
        )
        return 0

    tag = random.choice(tags).lstrip("#")
    max_actions = max(
        1,
        min(
            int(settings.get("max_writes_per_workflow", MAX_WRITES_PER_WORKFLOW)),
            6,
        ),
    )
    history.setdefault("browser_liked_urls", [])
    history.setdefault("browser_saved_urls", [])

    update_account_metric(
        username,
        "add_history",
        value=f"🌐 Browser Engage: loading #{tag}",
    )

    confirmed_likes = 0

    with sync_playwright() as p:
        context = _browser_launch(p, username)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(
                f"https://www.instagram.com/explore/tags/{tag}/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(1600)
            _browser_check_ready(page, context, username)

            hrefs = page.locator(
                "a[href*='/p/'], a[href*='/reel/']"
            ).evaluate_all(
                """els => els.map(e => e.href || e.getAttribute('href') || '')
                             .filter(Boolean)"""
            )
            links = []
            for href in hrefs:
                clean = str(href).split("?", 1)[0]
                if clean not in links:
                    links.append(clean)

            if not links:
                update_account_metric(
                    username,
                    "add_history",
                    value=f"⚠️ Browser Engage found no posts under #{tag}.",
                )
                return 0

            for href in links[:max_actions]:
                if href in history["browser_liked_urls"]:
                    continue

                page.goto(
                    href,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(1100)
                _browser_check_ready(page, context, username)

                # Like
                like_svg = _browser_find_svg_action(
                    page,
                    ("Like",),
                )
                if like_svg is not None:
                    if not wait_for_write_slot(
                        username,
                        "like",
                        max_wait=180,
                    ):
                        break

                    _browser_clickable_from_svg(like_svg).click(
                        timeout=5000
                    )
                    page.wait_for_timeout(700)

                    unlike_svg = _browser_find_svg_action(
                        page,
                        ("Unlike",),
                    )
                    if unlike_svg is not None:
                        record_write(username, "like")
                        history["browser_liked_urls"].append(href)
                        update_account_metric(
                            username,
                            "total_likes",
                            increment=1,
                        )
                        confirmed_likes += 1
                        update_account_metric(
                            username,
                            "add_history",
                            value=f"❤️ Browser Like confirmed: {href}",
                        )
                    else:
                        update_account_metric(
                            username,
                            "add_history",
                            value=f"⚠️ Browser Like was not confirmed: {href}",
                        )

                problem = _browser_page_problem(page)
                if problem:
                    apply_account_safety_backoff(
                        username,
                        f"Instagram Web engagement restriction/challenge: {problem}",
                        level="restricted",
                        hours=4,
                    )
                    break

                # Save/Favorite is part of the original engagement behavior.
                if href not in history["browser_saved_urls"]:
                    save_svg = _browser_find_svg_action(
                        page,
                        ("Save",),
                    )
                    if save_svg is not None:
                        if not wait_for_write_slot(
                            username,
                            "save",
                            max_wait=180,
                        ):
                            break

                        before = ""
                        try:
                            before = save_svg.get_attribute("aria-label") or ""
                        except Exception:
                            pass

                        _browser_clickable_from_svg(save_svg).click(
                            timeout=5000
                        )
                        page.wait_for_timeout(700)

                        after_remove = _browser_find_svg_action(
                            page,
                            ("Remove", "Unsave"),
                        )
                        current_save = _browser_find_svg_action(
                            page,
                            ("Save",),
                        )
                        confirmed_save = after_remove is not None or current_save is None

                        if confirmed_save:
                            record_write(username, "save")
                            history["browser_saved_urls"].append(href)
                            update_account_metric(
                                username,
                                "add_history",
                                value=f"🔖 Browser Save confirmed: {href}",
                            )
                        else:
                            update_account_metric(
                                username,
                                "add_history",
                                value=f"⚠️ Browser Save was not confirmed: {href}",
                            )

                page.wait_for_timeout(
                    random.randint(900, 1800)
                )

        finally:
            try:
                context.close()
            except Exception:
                pass

    history["browser_liked_urls"] = history["browser_liked_urls"][-5000:]
    history["browser_saved_urls"] = history["browser_saved_urls"][-5000:]
    return confirmed_likes


def _browser_prepare_post(username, history, folder_pool):
    settings = get_account_control_settings(username)
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
        folder for folder in folder_pool
        if folder["id"] not in history["posted_ids"]
        and folder["id"] not in pending_ids
    ]
    random.shuffle(pool)

    valid_exts = {".jpg", ".jpeg", ".png", ".mp4"}
    selected = None
    media_files = []

    for folder in pool:
        files = sorted(
            p for p in folder["path"].iterdir()
            if p.is_file() and p.suffix.lower() in valid_exts
        )
        if files:
            selected = folder
            media_files = files
            break

    if not selected:
        return None

    text_context = ""
    for txt in sorted(
        p for p in selected["path"].iterdir()
        if p.is_file() and p.suffix.lower() in {".txt", ".caption", ".md"}
    )[:3]:
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

    analysis = analyze_media_for_caption(
        media_files,
        sidecar_text=text_context,
        source_account=selected.get("source", ""),
        frame_count=settings["video_frames"],
        require_video_vision=settings["require_video_vision"],
    )
    if analysis.get("vision_required_failed"):
        return None

    semantic_context = analysis["context"]
    caption = generate_rage_bait_caption(
        semantic_context,
        perspective=analysis["perspective"],
        account_username=username,
    )
    if not caption:
        return None

    hashtag_prompt = f"""
Generate EXACTLY 5 relevant Instagram hashtags based ONLY on this media analysis:

{semantic_context}

Output only 5 space-separated hashtags.
""".strip()

    try:
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": settings["persona_prompt"],
                },
                {
                    "role": "user",
                    "content": hashtag_prompt,
                },
            ],
            options={"temperature": 0.45, "top_p": 0.9},
        )
        raw_tags = _clean_ollama_output(
            response.get("message", {}).get("content", "")
        )
    except Exception:
        raw_tags = ""

    tags = []
    for token in raw_tags.replace("\n", " ").split():
        token = token.strip(" ,.;:!?")
        if token and not token.startswith("#"):
            token = "#" + token
        if re.fullmatch(r"#[A-Za-z0-9_]+", token) and token not in tags:
            tags.append(token)
        if len(tags) == 5:
            break

    for fallback in (
        "#video",
        "#reels",
        "#creator",
        "#explore",
        "#daily",
    ):
        if len(tags) >= 5:
            break
        if fallback not in tags:
            tags.append(fallback)

    tag_line = " ".join(tags[:5])
    total_limit = int(settings["caption_char_limit"])
    caption = _clip_chars(
        caption,
        max(20, total_limit - len(tag_line) - 2),
    )
    full_caption = f"{caption}\n\n{tag_line}".strip()

    videos = [
        p for p in media_files
        if p.suffix.lower() == ".mp4"
    ]
    photos = [
        p for p in media_files
        if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    ]

    if videos:
        upload_files = [videos[0]]
    else:
        upload_files = photos[:10]

    if not upload_files:
        return None

    return {
        "folder": selected,
        "files": upload_files,
        "caption": full_caption,
    }


def _browser_post(username, history, folder_pool) -> bool:
    settings = get_account_control_settings(username)
    if not settings.get("enable_posts", False):
        return False

    prepared = _browser_prepare_post(
        username,
        history,
        folder_pool,
    )
    if not prepared:
        update_account_metric(
            username,
            "add_history",
            value="⚠️ Browser Post found no prepared media.",
        )
        return False

    selected = prepared["folder"]
    files = prepared["files"]
    full_caption = prepared["caption"]

    if not wait_for_write_slot(
        username,
        "upload",
        max_wait=180,
    ):
        update_account_metric(
            username,
            "add_history",
            value="⏳ Browser Post deferred by pacing/safety state.",
        )
        return False

    with sync_playwright() as p:
        context = _browser_launch(p, username)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            before_links = _browser_collect_profile_media_links(
                page,
                username,
            )
            _browser_check_ready(page, context, username)

            page.goto(
                "https://www.instagram.com/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(1000)
            _browser_check_ready(page, context, username)

            opened = False
            for label in ("New post", "Create"):
                svg = _browser_find_svg_action(
                    page,
                    (label,),
                )
                if svg is not None:
                    _browser_clickable_from_svg(svg).click(
                        timeout=5000
                    )
                    opened = True
                    break

            if not opened:
                opened = _browser_click_text_button(
                    page,
                    r"^Create$",
                )

            if not opened:
                raise RuntimeError(
                    "Instagram Web Create/Post control was not found."
                )

            page.wait_for_timeout(700)

            # Current web UI can show a Create menu before the composer.
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

            file_input = page.locator("input[type='file']").first
            deadline = time.time() + 10
            while time.time() < deadline and not file_input.count():
                page.wait_for_timeout(400)
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
                    f"{', '.join(p.name for p in files)}"
                ),
            )

            page.wait_for_timeout(1600)

            # Instagram normally has one or two Next screens.
            for _ in range(3):
                if _browser_click_text_button(
                    page,
                    r"^Next$",
                    timeout=5000,
                ):
                    page.wait_for_timeout(1000)
                else:
                    break

            # Caption input varies across A/B layouts.
            caption_box = None
            for selector in (
                "textarea[aria-label*='caption' i]",
                "textarea[placeholder*='caption' i]",
                "[contenteditable='true'][aria-label*='caption' i]",
                "div[role='textbox'][contenteditable='true']",
            ):
                try:
                    loc = page.locator(selector).last
                    if loc.count() and loc.is_visible(timeout=500):
                        caption_box = loc
                        break
                except Exception:
                    pass

            if caption_box is None:
                raise RuntimeError(
                    "Instagram Web caption editor was not found."
                )

            try:
                caption_box.fill(full_caption)
            except Exception:
                caption_box.focus()
                page.keyboard.insert_text(full_caption)

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
                value="⬆️ Browser Post submitted; verifying profile...",
            )

            confirmed = False
            final_link = ""
            deadline = time.time() + 75

            while time.time() < deadline:
                page.wait_for_timeout(3000)

                problem = _browser_page_problem(page)
                if problem:
                    raise RuntimeError(
                        f"Instagram Web upload interrupted: {problem}"
                    )

                current = _browser_collect_profile_media_links(
                    page,
                    username,
                )
                new_links = current - before_links
                if new_links:
                    confirmed = True
                    final_link = sorted(new_links)[-1]
                    break

            if confirmed:
                record_write(username, "upload")
                if selected["id"] not in history["posted_ids"]:
                    history["posted_ids"].append(
                        selected["id"]
                    )
                update_account_metric(
                    username,
                    "total_posts",
                    increment=1,
                )
                record_recent_post(
                    username=username,
                    folder_path=selected["path"],
                    caption=full_caption,
                    media_id="browser",
                    permalink=final_link,
                )
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        f"✅ Browser Post confirmed on profile: "
                        f"{final_link}"
                    ),
                )
                return True

            history.setdefault(
                "browser_pending_uploads",
                [],
            ).append(
                {
                    "folder_id": selected["id"],
                    "created_ts": time.time(),
                    "files": [str(p) for p in files],
                }
            )
            history["browser_pending_uploads"] = (
                history["browser_pending_uploads"][-30:]
            )
            update_account_metric(
                username,
                "add_history",
                value=(
                    "⏳ Browser Post was submitted but no new profile URL "
                    "appeared in the verification window. It was not counted "
                    "and the folder is held for 24h to reduce duplicate risk."
                ),
            )
            return False

        finally:
            try:
                context.close()
            except Exception:
                pass


def run_browser_profile_workflow(username, conf, folder_pool):
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

    settings = get_account_control_settings(
        username,
        conf,
    )
    update_account_metric(
        username,
        "status",
        status="Browser Mode / Active",
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
            )
        elif forced_task == "repost":
            _browser_post(
                username,
                history,
                folder_pool,
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
            choices = []
            if settings.get("enable_follow", False):
                choices.append("networking")
            if settings.get("enable_engage", False):
                choices.append("hashtags")
            if settings.get("enable_posts", False) and folder_pool:
                choices.append("repost")

            if choices:
                cycle_index = int(
                    history.get(
                        "browser_outward_cycle_index",
                        0,
                    )
                    or 0
                )
                task = choices[
                    cycle_index % len(choices)
                ]
                history["browser_outward_cycle_index"] = (
                    cycle_index + 1
                )

                if task == "networking":
                    _browser_follow_network(
                        username,
                        history,
                        conf,
                    )
                elif task == "hashtags":
                    _browser_engage_hashtag(
                        username,
                        history,
                        conf,
                    )
                elif task == "repost":
                    _browser_post(
                        username,
                        history,
                        folder_pool,
                    )

    finally:
        save_json(
            conf["history_file"],
            history,
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
        media_root_resolved=MEDIA_ROOT.resolve()
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
    metrics = load_analytics()
    result = {}

    for username, conf in CONTROL_ROSTER.items():
        row = dict(metrics.get(username, _new_account_metrics()))
        session_path = DOWNLOAD_ROOT / conf["session_file"]
        browser_mode = username in BROWSER_NATIVE_ACCOUNTS
        private_connected = (
            username in CLIENT_CACHE
            and username not in CONTROL_DISCONNECTED_ACCOUNTS
        )
        connected = private_connected or browser_mode

        row["control"] = {
            "connected": connected,
            "disconnected": not connected,
            "paused": username in CONTROL_PAUSED_ACCOUNTS,
            "auto_enabled": (
                connected
                and username not in CONTROL_PAUSED_ACCOUNTS
            ),
            "browser_mode": browser_mode,
            "browser_profile_saved": _browser_profile_saved(username),
            "private_api_connected": private_connected,
            "saved_session": session_path.exists(),
            "session_file": session_path.name,
            "queued_task": NEXT_TASK_OVERRIDE.get(username, ""),
            "auth_event": AUTH_EVENT.get(username, ""),
            "write_budget_used": len(_prune_write_window(username)),
            "write_budget_max": MAX_WRITES_PER_WINDOW,
            "write_window_seconds": WRITE_WINDOW_SECONDS,
            "upload_cooldown_seconds": _effective_pacing(username)["upload"],
            "settings": get_account_control_settings(username, conf),
            "safety": get_account_safety_state(username),
        }
        result[username] = row

    return {
        "accounts": result,
        "recent_posts": load_recent_posts()[:25],
    }



def _control_browser_login(username, timeout_seconds=600):
    """
    Browser bootstrap for accounts whose Instagram Web login works while
    private-API password login returns needs_upgrade.

    The user completes all login/challenge steps manually. Once Instagram
    supplies a session cookie, Chromium remains open for a finishing window so
    post-login prompts such as "Save login info" can be completed before the
    persistent browser profile is closed and flushed to disk.

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
    profile_dir = DOWNLOAD_ROOT / f"browser_{username.replace('.', '_')}"

    update_account_metric(username, "status", status="Browser Login Open")
    update_account_metric(
        username,
        "add_history",
        value=(
            "🌐 Browser Login opened. Complete Instagram login and any "
            "verification manually in Chromium."
        ),
    )

    sessionid = None

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            "https://www.instagram.com/",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        deadline = time.time() + max(120, int(timeout_seconds))
        last_log = 0.0

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

            if sessionid:
                break

            now = time.time()
            if now - last_log >= 20:
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        "🌐 Waiting for manual Browser Login to complete; "
                        "finish any Instagram challenge in Chromium."
                    ),
                )
                last_log = now

            page.wait_for_timeout(1200)

        if not sessionid:
            try:
                context.close()
            except Exception:
                pass
            update_account_metric(username, "status", status="Login Needed")
            raise RuntimeError(
                "Browser Login timed out before Instagram supplied a sessionid cookie."
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
                f"🌐 Browser session detected. Chromium will stay open for "
                f"{finish_seconds}s so you can click Save login info / finish "
                "post-login prompts. Do not close the window manually."
            ),
        )

        finish_deadline = time.time() + finish_seconds
        next_notice = time.time() + 30

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

            if time.time() >= next_notice:
                remaining = max(0, int(finish_deadline - time.time()))
                update_account_metric(
                    username,
                    "add_history",
                    value=(
                        f"🌐 Browser Login finishing window: {remaining}s "
                        "remaining for Instagram prompts."
                    ),
                )
                next_notice = time.time() + 30

            page.wait_for_timeout(1000)

        try:
            page.wait_for_timeout(1200)
        except Exception:
            pass

        try:
            context.close()
        except Exception:
            pass

    if not sessionid:
        update_account_metric(username, "status", status="Login Needed")
        raise RuntimeError(
            "Instagram browser session disappeared before it could be saved."
        )

    # Preserve a separate browser-session copy without printing the cookie.
    browser_cookie_file = DOWNLOAD_ROOT / (
        f"browser_session_{username.replace('.', '_')}.json"
    )
    try:
        browser_cookie_file.write_text(
            json.dumps({"sessionid": sessionid}),
            encoding="utf-8",
        )
    except Exception:
        pass

    update_account_metric(
        username,
        "add_history",
        value=(
            "💾 Browser profile/session saved locally. Testing whether "
            "Instagram also accepts it for private API automation..."
        ),
    )

    cl = Client()
    _refresh_instagram_app_profile(cl, username)

    try:
        cl.login_by_sessionid(sessionid)
    except Exception as exc:
        if _is_needs_upgrade_error(exc) or is_login_required_error(exc):
            set_auth_cooldown(username, minutes=360)
            _activate_browser_native_mode(
                username,
                "Instagram Web login is valid, but private API session conversion is blocked.",
            )
            update_account_metric(
                username,
                "add_history",
                value=(
                    "💾 Browser login is saved, but Instagram rejected the "
                    "private-API session conversion. Browser profile preserved."
                ),
            )
            return {
                "ok": True,
                "mode": "browser_saved_private_blocked",
                "browser_saved": True,
                "private_api_ready": False,
                "message": (
                    "Browser login saved successfully. Instagram is still "
                    "blocking private-API automation for this account."
                ),
            }

        message = str(exc)
        low = message.lower()
        if "429" in low or "too many requests" in low or "please wait" in low:
            set_auth_cooldown(username, minutes=360)
            _activate_browser_native_mode(
                username,
                "Browser login saved; private API conversion is temporarily throttled.",
            )
            return {
                "ok": True,
                "mode": "browser_saved_api_cooldown",
                "browser_saved": True,
                "private_api_ready": False,
                "message": (
                    "Browser login saved successfully. Instagram throttled the "
                    "private-API conversion attempt; automation remains off."
                ),
            }

        _activate_browser_native_mode(
            username,
            "Browser login saved; private API conversion failed.",
        )
        update_account_metric(
            username,
            "add_history",
            value=(
                "💾 Browser session remains saved, but instagrapi conversion "
                f"failed: {type(exc).__name__}: {message[:180]}"
            ),
        )
        return {
            "ok": True,
            "mode": "browser_saved_conversion_failed",
            "browser_saved": True,
            "private_api_ready": False,
            "message": (
                "Browser login saved, but it could not be converted into an "
                "instagrapi session."
            ),
        }

    if not verify_authenticated_identity(cl, username, session_p):
        try:
            cl.logout()
        except Exception:
            pass
        update_account_metric(
            username,
            "status",
            status="Browser Saved / Identity Mismatch",
        )
        return {
            "ok": True,
            "mode": "browser_saved_identity_mismatch",
            "browser_saved": True,
            "private_api_ready": False,
            "message": (
                "Browser login was saved, but the converted private session "
                "did not verify as the configured account."
            ),
        }

    cl.dump_settings(session_p)
    patch_obsolete_qe_expose(cl, username)

    capabilities = probe_browser_import_capabilities(
        cl,
        username,
        conf,
    )

    if not capabilities.get("timeline"):
        set_auth_cooldown(username, minutes=360)
        update_account_metric(
            username,
            "status",
            status="Browser Saved / Private API Blocked",
        )
        update_account_metric(
            username,
            "add_history",
            value=(
                "💾 Browser login/profile saved successfully. Identity conversion "
                "succeeded, but Instagram still returned LoginRequired for a "
                "core private timeline read. Browser login is preserved; "
                "private-API automation remains OFF."
            ),
        )

        _activate_browser_native_mode(
            username,
            "Browser identity is valid, but Instagram rejects the private timeline endpoint. "
            "Follow, Engage, and Post will use Instagram Web.",
        )

        return {
            "ok": True,
            "mode": "browser_saved_private_blocked",
            "browser_saved": True,
            "private_api_ready": False,
            "capabilities": capabilities,
            "message": (
                "Browser login saved. Instagram Web is usable, but private-API "
                "automation is still blocked for this account."
            ),
        }

    capability_note = (
        "private hashtag feed ready"
        if capabilities.get("hashtag_private")
        else (
            "private hashtag feed blocked; GraphQL hashtag discovery available"
            if capabilities.get("hashtag_graphql")
            else "hashtag discovery unavailable"
        )
    )

    _deactivate_browser_native_mode(username)

    with CONTROL_LOCK:
        CLIENT_CACHE[username] = cl
        CONTROL_DISCONNECTED_ACCOUNTS.discard(username)
        CONTROL_PAUSED_ACCOUNTS.add(username)
        AUTH_RETRY_AFTER.pop(username, None)
        ACCOUNT_COOLDOWNS.pop(username, None)
        LAST_DM_POLL.pop(username, None)
        AUTH_EVENT[username] = "browser_login_saved"

    update_account_metric(
        username,
        "status",
        status="Connected / Auto Off",
    )
    update_account_metric(
        username,
        "add_history",
        value=(
            "🌐 Browser Login saved and private-API readiness passed. "
            f"Readiness probe: {capability_note}. Background automation "
            "remains OFF until Start Automation is pressed."
        ),
    )

    return {
        "ok": True,
        "mode": "browser_login",
        "browser_saved": True,
        "private_api_ready": True,
        "capabilities": capabilities,
        "message": "Browser login saved and private API automation is ready.",
    }


def _control_quick_login(username):
    """
    Explicit one-click login using an existing saved instagrapi session.

    Nothing calls this automatically at boot.
    """
    username = str(username or "").strip().lstrip("@")
    conf = _control_conf(username)
    if not conf:
        raise ValueError(f"Unknown account @{username}")

    session_p = DOWNLOAD_ROOT / conf["session_file"]
    if not session_p.exists():
        raise RuntimeError(
            "No saved session exists for this account. Use Password Login once."
        )

    cl = Client()
    cl.load_settings(session_p)
    _refresh_instagram_app_profile(cl, username)

    # This is the first Instagram network call after the user explicitly presses
    # Quick Login.
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
                "Saved browser-imported session is still valid for Web, but "
                "Instagram rejected the private timeline endpoint with "
                "LoginRequired. Quick Login cannot enable automation yet."
            ) from exc
        raise

    if not verify_authenticated_identity(cl, username, session_p):
        raise RuntimeError(
            "Saved session identity does not match this roster account."
        )

    patch_obsolete_qe_expose(cl, username)

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
        username, "add_history",
        value="🔓 Quick Login: session connected; background automation remains OFF until Start Automation is pressed."
    )
    return {"ok": True, "mode": "saved_session"}


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
    Safety backoff cannot be manually overridden until its timer expires.
    """
    username = str(username or "").strip().lstrip("@")
    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")

    connected = (
        (
            username in CLIENT_CACHE
            and username not in CONTROL_DISCONNECTED_ACCOUNTS
        )
        or username in BROWSER_NATIVE_ACCOUNTS
    )
    if not connected:
        raise RuntimeError(
            "Account is disconnected. Use Quick Login or Browser Login first."
        )

    mode_label = (
        "Browser Mode"
        if username in BROWSER_NATIVE_ACCOUNTS
        else "Connected"
    )

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
        state = get_account_safety_state(username)
        if state["active"]:
            raise RuntimeError(
                f"Safety Backoff is active until {state['until']}. "
                f"Reason: {state['reason'][:160]}"
            )

        cooldown = ACCOUNT_COOLDOWNS.get(username)
        if cooldown and datetime.now() < cooldown:
            raise RuntimeError(
                f"Account cooldown is active until "
                f"{cooldown.strftime('%Y-%m-%d %H:%M')}."
            )

        CONTROL_PAUSED_ACCOUNTS.discard(username)
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
        "browser_mode": username in BROWSER_NATIVE_ACCOUNTS,
    }




def _control_queue_task(username, task):
    username = str(username or "").strip().lstrip("@")
    allowed = {"repost", "networking", "hashtags", "comments"}

    if username not in CONTROL_ROSTER:
        raise ValueError(f"Unknown account @{username}")

    connected = (
        (
            username in CLIENT_CACHE
            and username not in CONTROL_DISCONNECTED_ACCOUNTS
        )
        or username in BROWSER_NATIVE_ACCOUNTS
    )
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

    with CONTROL_LOCK:
        NEXT_TASK_OVERRIDE[username] = task
        CONTROL_FORCE_RUN.add(username)

    update_account_metric(
        username,
        "add_history",
        value=(
            f"🎛️ Queued manual task: {task}"
            + (
                " (Browser Mode)"
                if username in BROWSER_NATIVE_ACCOUNTS
                else ""
            )
        ),
    )
    return {
        "ok": True,
        "task": task,
        "browser_mode": username in BROWSER_NATIVE_ACCOUNTS,
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
        <button ${connected ? "":"disabled"} onclick="queueTask('${esc(user)}','repost')">Upload/Repost</button>
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

function render(data){
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


            elif action == "save_settings":
                result = _control_save_settings(
                    username,
                    body.get("settings", {}),
                )

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
    try:
        folder_pool = discover_local_media_folders()
        result = run_profile_workflow(username, conf, folder_pool)

    except FeedbackRequired as exc:
        apply_account_safety_backoff(
            username,
            f"Instagram FeedbackRequired during workflow: {exc}",
            level="restricted",
        )
        result = "throttled"

    except Exception as exc:
        observe_platform_signal(username, exc, action="workflow")
        update_account_metric(
            username, "add_history",
            value=f"Workflow error: {type(exc).__name__}: {str(exc)[:100]}"
        )
        result = "error"

    delay = (
        random.randint(WORKFLOW_SLEEP_MIN, WORKFLOW_SLEEP_MAX)
        if result == "ran"
        else WORKFLOW_RETRY_IDLE_SECONDS
    )

    with ACCOUNT_WORKFLOW_THREADS_LOCK:
        next_workflow_at[username] = time.monotonic() + delay
        ACCOUNT_WORKFLOW_THREADS.pop(username, None)



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
    CONTROL_DISCONNECTED_ACCOUNTS.clear()
    CONTROL_DISCONNECTED_ACCOUNTS.update(active_roster.keys())
    CONTROL_PAUSED_ACCOUNTS.clear()
    CONTROL_PAUSED_ACCOUNTS.update(active_roster.keys())
    CONTROL_FORCE_RUN.clear()
    LAST_DM_POLL.clear()

    for user in active_roster:
        AUTH_EVENT[user] = "disconnected"
        update_account_metric(user, "status", status="Disconnected / Auto Off")

    print(f"Instagram Control Head — browser native fallback: http://{IG_HOST}:{IG_PORT}")
    print(f"Instagram state root: {DOWNLOAD_ROOT}")
    print(f"Instagram media root: {MEDIA_ROOT}")
    print("Configured accounts: " + (", ".join(f"@{u}" for u in active_roster) if active_roster else "(none yet — add up to 3 in the Control Head)"))
    print("Dashboard account cap: 3")
    print("Boot mode: DISCONNECTED / AUTO OFF")

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
                connected = (
                    (
                        current_user in CLIENT_CACHE
                        and current_user not in CONTROL_DISCONNECTED_ACCOUNTS
                    )
                    or current_user in BROWSER_NATIVE_ACCOUNTS
                )
                if not connected:
                    continue

                safety = get_account_safety_state(current_user)
                if safety["active"]:
                    CONTROL_PAUSED_ACCOUNTS.add(current_user)
                    continue

                cooldown = ACCOUNT_COOLDOWNS.get(current_user)
                if cooldown and datetime.now() < cooldown:
                    continue

                forced = current_user in CONTROL_FORCE_RUN
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
