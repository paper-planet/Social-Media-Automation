import json
import os
import time
import random
import subprocess
import sys
from getpass import getpass
from datetime import datetime, timezone

import pyotp

from instagrapi import Client
from instagrapi.exceptions import (
    FeedbackRequired,
    LoginRequired,
    ClientError,
)


# ============================================================
# CONFIGURATION
# ============================================================

DOWNLOAD_ROOT = r"D:\ig-reposts-data"

os.makedirs(DOWNLOAD_ROOT, exist_ok=True)


# Instagram accounts to archive
INACTIVE_INSTA_MAINS = [
    "",
    "",
]


# TikTok accounts
# @ is optional because the script cleans it automatically
TIKTOK_MAINS = [
    "@",
    "@",
]


# Instagram scraping/login account
# Password and 2FA secret are intentionally NOT stored in this file.
# Optional environment variables:
# Instagram scraping/login account
# 2FA seed / secret, NOT the current six-digit code
SCRAPER_USER = ""
SCRAPER_PASS = ""
SCRAPER_TOTP = ""


# Instagram exhaustive archive settings
# A page is intentionally small/moderate so the private endpoint is not
# asked for thousands of items in one request. Pagination continues until
# Instagram stops returning a next cursor.
INSTAGRAM_PAGE_SIZE = 12
INSTAGRAM_PAGE_DELAY_MIN = 2.0
INSTAGRAM_PAGE_DELAY_MAX = 5.0
VERIFY_EXISTING_FILES = True


MANIFEST_FILE = os.path.join(
    DOWNLOAD_ROOT,
    "repost_manifest.json"
)

SESSION_FILE = os.path.join(
    DOWNLOAD_ROOT,
    "instagram_session.json"
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def generate_2fa(secret):
    """
    Generate current Instagram TOTP code.
    """

    secret = (secret or "").replace(" ", "").strip()

    if not secret:
        return None

    return pyotp.TOTP(secret).now()


def save_manifest(manifest_data):
    """
    Save manifest atomically.

    Writes to a temporary file first so Ctrl+C or a crash is
    less likely to destroy the existing manifest.
    """

    temp_file = MANIFEST_FILE + ".tmp"

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            manifest_data,
            f,
            indent=4,
            ensure_ascii=False
        )

    os.replace(
        temp_file,
        MANIFEST_FILE
    )


def load_manifest():
    """
    Load existing archive manifest.
    """

    if not os.path.exists(MANIFEST_FILE):
        return {}

    try:

        with open(
            MANIFEST_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except json.JSONDecodeError:

        print(
            "⚠️ Existing manifest is damaged."
        )

        backup = MANIFEST_FILE + ".corrupt"

        os.replace(
            MANIFEST_FILE,
            backup
        )

        print(
            f"📦 Damaged manifest moved to:\n{backup}"
        )

        return {}


def polite_delay(min_seconds=4, max_seconds=9):
    """
    Small pause between requests/downloads.
    """

    delay = random.uniform(
        min_seconds,
        max_seconds
    )

    print(
        f"⏳ Waiting {delay:.1f} seconds..."
    )

    time.sleep(delay)


# ============================================================
# CHRONOLOGICAL ARCHIVE HELPERS
# ============================================================

def _as_utc_datetime(value):
    """Return a timezone-aware UTC datetime when possible."""

    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt


def _safe_name_component(value):
    """Make an account/platform component safe for Windows filenames."""

    text = str(value or "unknown").strip().lstrip("@")
    bad = '<>:"/\\|?*'

    for ch in bad:
        text = text.replace(ch, "_")

    return text or "unknown"


def _chronological_basename(platform, account, media_id, posted_at):
    """Build a filename/folder prefix that sorts chronologically by name."""

    dt = _as_utc_datetime(posted_at)
    if dt is None:
        return None

    stamp = dt.strftime("%Y-%m-%d_%H-%M-%SZ")
    account = _safe_name_component(account)
    platform_tag = "ig" if platform == "instagram" else "tt"

    return f"{stamp}__{platform_tag}__{account}__{media_id}"


def _unique_destination(directory, filename, source_path):
    """Return a collision-safe destination without overwriting another file."""

    candidate = os.path.join(directory, filename)
    source_abs = os.path.abspath(source_path)
    candidate_abs = os.path.abspath(candidate)

    if source_abs == candidate_abs or not os.path.exists(candidate):
        return candidate

    stem, ext = os.path.splitext(filename)
    counter = 2

    while True:
        candidate = os.path.join(
            directory,
            f"{stem}__dup{counter}{ext}"
        )

        if os.path.abspath(candidate) == source_abs or not os.path.exists(candidate):
            return candidate

        counter += 1


def _rename_local_paths_chronologically(
    paths,
    platform,
    account,
    media_id,
    posted_at,
):
    """
    Move downloaded media into a chronological post folder and rename files.

    Single media:
      2021-04-17_14-35-22Z__ig__account__id.jpg

    Carousel/media sets:
      ...__01.jpg
      ...__02.mp4

    Returns (new_paths, changed). Missing paths are left untouched so the
    existing verifier can decide whether they need to be re-downloaded.
    """

    if isinstance(paths, str):
        paths = [paths]

    paths = list(paths or [])
    basename = _chronological_basename(
        platform,
        account,
        media_id,
        posted_at,
    )

    if not paths or not basename:
        return paths, False

    destination_dir = os.path.join(DOWNLOAD_ROOT, basename)
    os.makedirs(destination_dir, exist_ok=True)

    new_paths = []
    changed = False
    old_parent_dirs = set()
    multi = len(paths) > 1

    for index, original_path in enumerate(paths, start=1):
        original_path = str(original_path)

        if not os.path.isfile(original_path):
            new_paths.append(original_path)
            continue

        old_parent_dirs.add(os.path.dirname(os.path.abspath(original_path)))
        _, ext = os.path.splitext(original_path)

        suffix = f"__{index:02d}" if multi else ""
        desired_filename = f"{basename}{suffix}{ext.lower()}"
        destination = _unique_destination(
            destination_dir,
            desired_filename,
            original_path,
        )

        if os.path.abspath(original_path) != os.path.abspath(destination):
            os.replace(original_path, destination)
            changed = True

        new_paths.append(destination)

    # Remove old per-post folders only when they are now empty. Never delete
    # a directory that still contains an untracked file.
    destination_abs = os.path.abspath(destination_dir)
    for old_dir in old_parent_dirs:
        if old_dir == destination_abs:
            continue
        try:
            if os.path.isdir(old_dir) and not os.listdir(old_dir):
                os.rmdir(old_dir)
        except OSError:
            pass

    return new_paths, changed


def _instagram_posted_at(media):
    """Original Instagram posting time supplied by instagrapi."""

    return _as_utc_datetime(getattr(media, "taken_at", None))


def _tiktok_posted_at(video, video_id):
    """
    Return (UTC datetime, source). Prefer yt-dlp metadata.

    TikTok's numeric video IDs encode a Unix timestamp in their high 32 bits,
    so that provides a fast fallback for old cached downloads whose flat
    playlist metadata does not contain timestamp/upload_date.
    """

    for key in ("timestamp", "release_timestamp"):
        raw = video.get(key)
        if raw is not None:
            try:
                return (
                    datetime.fromtimestamp(float(raw), tz=timezone.utc),
                    f"yt-dlp:{key}",
                )
            except (TypeError, ValueError, OSError):
                pass

    upload_date = str(video.get("upload_date") or "").strip()
    if len(upload_date) == 8 and upload_date.isdigit():
        try:
            return (
                datetime.strptime(upload_date, "%Y%m%d").replace(
                    tzinfo=timezone.utc
                ),
                "yt-dlp:upload_date",
            )
        except ValueError:
            pass

    # Fast no-network fallback for normal 64-bit TikTok video IDs.
    try:
        numeric_id = int(str(video_id))
        unix_seconds = numeric_id >> 32
        dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)

        # Plausibility guard prevents a malformed/non-TikTok ID from creating
        # nonsense dates. TikTok/Musical.ly era content should be after 2014.
        now = datetime.now(timezone.utc)
        if datetime(2014, 1, 1, tzinfo=timezone.utc) <= dt <= now:
            return dt, "tiktok_video_id_high32"
    except (TypeError, ValueError, OSError, OverflowError):
        pass

    return None, None


def _iso_utc(dt):
    dt = _as_utc_datetime(dt)
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


# ============================================================
# YT-DLP HELPERS
# ============================================================

def yt_dlp_command():
    """
    Run yt-dlp through this Python installation.

    This avoids problems where Windows cannot find
    yt-dlp.exe through PATH.
    """

    return [
        sys.executable,
        "-m",
        "yt_dlp",
    ]


# ============================================================
# INSTAGRAM LOGIN
# ============================================================

def login_instagram():

    cl = Client()

    print(
        f"🔐 Logging into scraping account "
        f"@{SCRAPER_USER}..."
    )

    # --------------------------------------------------------
    # Reuse previous device/session information
    # --------------------------------------------------------

    if os.path.exists(SESSION_FILE):

        try:

            print(
                "🔑 Loading saved Instagram session..."
            )

            cl.load_settings(
                SESSION_FILE
            )

        except Exception as e:

            print(
                f"⚠️ Could not load session: {e}"
            )

    # --------------------------------------------------------
    # Login
    # --------------------------------------------------------

    try:

        password = SCRAPER_PASS or getpass(
            f"Instagram password for @{SCRAPER_USER}: "
        )

        code = generate_2fa(
            SCRAPER_TOTP
        )

        # If no reusable TOTP seed was supplied through the environment,
        # allow a current six-digit code to be entered interactively.
        if not code:
            typed_code = getpass(
                "Instagram 2FA code (press Enter if not enabled): "
            ).strip()
            code = typed_code or None

        if code:

            print(
                "🔢 2FA verification code ready."
            )

            cl.login(
                SCRAPER_USER,
                password,
                verification_code=code
            )

        else:

            cl.login(
                SCRAPER_USER,
                password
            )

        # Save working settings/session
        cl.dump_settings(
            SESSION_FILE
        )

        print(
            "✅ Instagram login successful."
        )

        return cl

    except LoginRequired as e:

        print(
            f"❌ Instagram requested a new login: {e}"
        )

    except FeedbackRequired as e:

        print(
            "🛑 Instagram temporarily refused the login/request."
        )

        print(
            f"Details: {e}"
        )

    except Exception as e:

        print(
            f"❌ Instagram login failed: {e}"
        )

    return None


# ============================================================
# INSTAGRAM DOWNLOAD
# ============================================================

def _media_expected_file_count(media):
    """Best-effort expected number of files for one Instagram post."""

    if getattr(media, "media_type", None) != 8:
        return 1

    resources = getattr(media, "resources", None) or []

    if resources:
        return len(resources)

    # Some instagrapi versions expose carousel children differently.
    carousel_media = getattr(media, "carousel_media", None) or []

    if carousel_media:
        return len(carousel_media)

    return None


def _valid_file(path):
    """A downloaded file must exist and contain at least one byte."""

    try:
        return (
            bool(path)
            and os.path.isfile(path)
            and os.path.getsize(path) > 0
        )
    except OSError:
        return False


def _find_instagram_manifest_key(manifest_data, media_id):
    """Support both the current ig_<id> key and older raw-ID entries."""

    preferred = f"ig_{media_id}"

    if preferred in manifest_data:
        return preferred

    if media_id in manifest_data:
        return media_id

    return None


def _migrate_instagram_cached_entry(manifest_data, media, target):
    """Rename an already-downloaded Instagram post and repair its manifest."""

    media_id = str(media.id)
    key = _find_instagram_manifest_key(manifest_data, media_id)

    if not key:
        return False

    entry = manifest_data.get(key)
    if not isinstance(entry, dict):
        return False

    posted_at = _instagram_posted_at(media)
    changed = False

    if posted_at is not None:
        new_paths, paths_changed = _rename_local_paths_chronologically(
            entry.get("local_paths") or [],
            "instagram",
            target,
            media_id,
            posted_at,
        )

        if paths_changed or new_paths != (entry.get("local_paths") or []):
            entry["local_paths"] = new_paths
            changed = True

        posted_iso = _iso_utc(posted_at)
        posted_ts = int(posted_at.timestamp())

        if entry.get("posted_at_utc") != posted_iso:
            entry["posted_at_utc"] = posted_iso
            changed = True

        if entry.get("posted_at_timestamp") != posted_ts:
            entry["posted_at_timestamp"] = posted_ts
            changed = True

        if entry.get("timestamp_source") != "instagram:taken_at":
            entry["timestamp_source"] = "instagram:taken_at"
            changed = True

    preferred = f"ig_{media_id}"
    if key != preferred:
        manifest_data[preferred] = entry
        manifest_data.pop(key, None)
        changed = True

    if changed:
        save_manifest(manifest_data)

    return changed


def _manifest_media_is_valid(manifest_data, media, migrate=True):
    """
    Confirm that a cached manifest entry actually points to files that exist.

    For carousel posts, also compare the number of recorded local files with
    the number of carousel children when instagrapi supplies that information.
    """

    media_id = str(media.id)
    key = _find_instagram_manifest_key(
        manifest_data,
        media_id
    )

    if not key:
        return False

    entry = manifest_data.get(key)

    if not isinstance(entry, dict):
        return False

    paths = entry.get("local_paths") or []

    if isinstance(paths, str):
        paths = [paths]

    if not paths:
        return False

    if VERIFY_EXISTING_FILES:
        if not all(_valid_file(path) for path in paths):
            return False

        expected_count = _media_expected_file_count(media)

        if (
            expected_count is not None
            and len(paths) < expected_count
        ):
            return False

    # Migrate old raw Instagram IDs to the prefixed key so future runs are
    # consistent with TikTok's namespaced IDs.
    preferred = f"ig_{media_id}"

    if migrate and key != preferred:
        manifest_data[preferred] = entry
        manifest_data.pop(key, None)
        save_manifest(manifest_data)

    return True


def _remove_bad_manifest_entry(manifest_data, media_id):
    """Remove stale manifest records so the media can be downloaded again."""

    for key in (f"ig_{media_id}", media_id):
        if key in manifest_data:
            manifest_data.pop(key, None)

    save_manifest(manifest_data)


def _scan_all_instagram_media(cl, target_id, target):
    """
    Exhaustively page through a user's Instagram feed.

    Success means Instagram returned no next cursor. A repeated cursor or an
    exception is treated as an incomplete crawl rather than pretending the
    archive is complete.
    """

    all_medias = []
    seen_media_ids = set()
    seen_cursors = set()
    cursor = ""
    page_number = 0

    while True:
        page_number += 1

        print()
        print(
            f"📡 @{target}: requesting Instagram page "
            f"{page_number}..."
        )

        try:
            page, next_cursor = cl.user_medias_paginated_v1(
                target_id,
                amount=INSTAGRAM_PAGE_SIZE,
                end_cursor=cursor,
            )

        except AttributeError:
            # Compatibility fallback for older instagrapi releases. amount=0
            # means no user-imposed cap and user_medias_v1 paginates internally.
            if page_number != 1:
                raise

            print(
                "ℹ️ This instagrapi version does not expose "
                "user_medias_paginated_v1(). Falling back to "
                "user_medias_v1(amount=0)."
            )

            medias = cl.user_medias_v1(
                target_id,
                amount=0,
            )

            for media in medias:
                media_id = str(media.id)

                if media_id not in seen_media_ids:
                    seen_media_ids.add(media_id)
                    all_medias.append(media)

            print(
                f"✅ Fallback crawl returned "
                f"{len(all_medias):,} unique posts."
            )

            return all_medias, True, page_number

        except FeedbackRequired as e:
            print(
                "🛑 Instagram temporarily refused pagination."
            )
            print(f"Details: {e}")
            return all_medias, False, page_number

        except ClientError as e:
            print(
                f"❌ Instagram pagination API error for "
                f"@{target}: {e}"
            )
            return all_medias, False, page_number

        except Exception as e:
            print(
                f"❌ Pagination failed for @{target}: "
                f"{type(e).__name__}: {e}"
            )
            return all_medias, False, page_number

        new_on_page = 0

        for media in page or []:
            media_id = str(media.id)

            if media_id in seen_media_ids:
                continue

            seen_media_ids.add(media_id)
            all_medias.append(media)
            new_on_page += 1

        print(
            f"📋 Page {page_number}: "
            f"{len(page or []):,} returned, "
            f"{new_on_page:,} new, "
            f"{len(all_medias):,} unique total."
        )

        if not next_cursor:
            print(
                "✅ Instagram returned no next cursor. "
                "Feed crawl reached the end."
            )
            return all_medias, True, page_number

        next_cursor = str(next_cursor)

        if next_cursor == cursor or next_cursor in seen_cursors:
            print(
                "⚠️ Instagram repeated a pagination cursor. "
                "Stopping without marking the crawl complete."
            )
            return all_medias, False, page_number

        if cursor:
            seen_cursors.add(cursor)

        cursor = next_cursor

        delay = random.uniform(
            INSTAGRAM_PAGE_DELAY_MIN,
            INSTAGRAM_PAGE_DELAY_MAX,
        )

        print(
            f"⏳ Waiting {delay:.1f}s before the next "
            f"Instagram metadata page..."
        )
        time.sleep(delay)


def _write_instagram_audit(
    target,
    profile_media_count,
    discovered_medias,
    crawl_complete,
    pages_scanned,
    verified_ids,
    failed_ids,
):
    """Write a machine-readable archive audit for this Instagram account."""

    discovered_ids = [
        str(media.id)
        for media in discovered_medias
    ]

    missing_ids = sorted(
        set(discovered_ids) - set(verified_ids)
    )

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "account": target,
        "profile_media_count": profile_media_count,
        "crawl_complete": bool(crawl_complete),
        "pages_scanned": pages_scanned,
        "discovered_unique_posts": len(discovered_ids),
        "verified_cached_posts": len(set(verified_ids)),
        "failed_download_ids": sorted(set(failed_ids)),
        "missing_or_unverified_ids": missing_ids,
    }

    report_path = os.path.join(
        DOWNLOAD_ROOT,
        f"instagram_audit_{target}.json",
    )

    temp_path = report_path + ".tmp"

    with open(
        temp_path,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            report,
            f,
            indent=4,
            ensure_ascii=False,
        )

    os.replace(temp_path, report_path)

    return report_path, report


def download_instagram_account(
    cl,
    target,
    manifest_data
):

    target = target.strip().lstrip("@")

    print()
    print("=" * 60)
    print(
        f"🎯 [INSTAGRAM EXHAUSTIVE SWEEP] "
        f"Crawling the entire feed for @{target}..."
    )

    try:
        print(
            f"🔎 Resolving @{target} using private API..."
        )

        user = cl.user_info_by_username_v1(target)
        target_id = str(user.pk)
        profile_media_count = getattr(
            user,
            "media_count",
            None,
        )

        print(f"✅ Found @{user.username}")
        print(f"🆔 Instagram user ID: {target_id}")

        if profile_media_count is not None:
            print(
                f"🔢 Profile-reported media count: "
                f"{profile_media_count:,}"
            )

    except FeedbackRequired as e:
        print(
            "🛑 Instagram temporarily refused requests."
        )
        print(f"Details: {e}")
        return False

    except ClientError as e:
        print(
            f"❌ Instagram API error for @{target}: {e}"
        )
        return True

    except Exception as e:
        print(
            f"❌ Could not retrieve @{target}: "
            f"{type(e).__name__}: {e}"
        )
        return True

    # --------------------------------------------------------
    # EXHAUSTIVE PAGINATION
    # --------------------------------------------------------

    all_medias, crawl_complete, pages_scanned = (
        _scan_all_instagram_media(
            cl,
            target_id,
            target,
        )
    )

    print()
    print(
        f"📚 @{target}: discovered "
        f"{len(all_medias):,} unique posts across "
        f"{pages_scanned:,} page(s)."
    )

    if not crawl_complete:
        print(
            "⚠️ IMPORTANT: the feed crawl did NOT reach a clean end. "
            "This run will download everything discovered so far, but "
            "the account will NOT be reported as fully verified."
        )

    if (
        profile_media_count is not None
        and profile_media_count > 0
        and len(all_medias) < profile_media_count
    ):
        print(
            f"⚠️ COUNT CHECK: profile reports "
            f"{profile_media_count:,} media but this crawl saw "
            f"{len(all_medias):,} unique posts."
        )
        print(
            "   That can happen with inaccessible/deleted/collab content, "
            "but it means you should not assume the archive is complete "
            "based on count alone."
        )

    # --------------------------------------------------------
    # DOWNLOAD / VERIFY EVERY DISCOVERED POST
    # --------------------------------------------------------

    verified_ids = []
    failed_ids = []

    for index, media in enumerate(
        all_medias,
        start=1,
    ):
        media_id = str(media.id)
        manifest_id = f"ig_{media_id}"

        # If this post was downloaded by an older version of the script,
        # rename it now using the original Instagram posting timestamp and
        # update local_paths before cached-file verification runs.
        migrated = _migrate_instagram_cached_entry(
            manifest_data,
            media,
            target,
        )
        if migrated:
            print(
                f"🗓️ [{index}/{len(all_medias)}] "
                f"Chronological name applied: {media_id}"
            )

        if _manifest_media_is_valid(
            manifest_data,
            media,
        ):
            print(
                f"⏭️ [{index}/{len(all_medias)}] "
                f"Verified cached: {media_id}"
            )
            verified_ids.append(media_id)
            continue

        # A manifest record exists but points to missing/empty/incomplete files.
        if _find_instagram_manifest_key(
            manifest_data,
            media_id,
        ):
            print(
                f"♻️ [{index}/{len(all_medias)}] "
                f"Cache record is incomplete/broken; "
                f"re-downloading {media_id}"
            )
            _remove_bad_manifest_entry(
                manifest_data,
                media_id,
            )

        print()
        print(
            f"📥 [{index}/{len(all_medias)}] "
            f"Instagram media {media_id}"
        )

        posted_at = _instagram_posted_at(media)
        chronological_name = _chronological_basename(
            "instagram",
            target,
            media_id,
            posted_at,
        )

        post_dir = os.path.join(
            DOWNLOAD_ROOT,
            chronological_name or f"ig_{target}_{media_id}",
        )

        os.makedirs(
            post_dir,
            exist_ok=True,
        )

        try:
            if media.media_type == 1:
                print("🖼️ Downloading photo...")
                downloaded = cl.photo_download(
                    media.id,
                    folder=post_dir,
                )
                paths = [str(downloaded)]

            elif media.media_type == 2:
                print("🎥 Downloading video/reel...")
                downloaded = cl.video_download(
                    media.id,
                    folder=post_dir,
                )
                paths = [str(downloaded)]

            elif media.media_type == 8:
                print("🖼️🖼️ Downloading carousel...")
                downloaded = cl.album_download(
                    media.id,
                    folder=post_dir,
                )
                paths = [
                    str(path)
                    for path in downloaded
                ]

            else:
                print(
                    f"⚠️ Unknown media type: "
                    f"{media.media_type}"
                )
                failed_ids.append(media_id)
                continue

            expected_count = _media_expected_file_count(media)

            if not paths or not all(
                _valid_file(path)
                for path in paths
            ):
                raise RuntimeError(
                    "download returned missing or empty local file(s)"
                )

            if (
                expected_count is not None
                and len(paths) < expected_count
            ):
                raise RuntimeError(
                    f"expected {expected_count} file(s) for this post "
                    f"but only found {len(paths)}"
                )

            # Normalize newly-downloaded filenames immediately, using the same
            # naming convention as migrated cached posts.
            paths, _ = _rename_local_paths_chronologically(
                paths,
                "instagram",
                target,
                media_id,
                posted_at,
            )

            caption = media.caption_text or ""

            manifest_data[manifest_id] = {
                "platform": "instagram",
                "source_account": target,
                "media_id": media_id,
                "media_type": media.media_type,
                "shortcode": getattr(
                    media,
                    "code",
                    None,
                ),
                "original_caption": caption,
                "posted_at_utc": _iso_utc(posted_at),
                "posted_at_timestamp": (
                    int(posted_at.timestamp())
                    if posted_at is not None
                    else None
                ),
                "timestamp_source": (
                    "instagram:taken_at"
                    if posted_at is not None
                    else None
                ),
                "local_paths": paths,
                "verified_nonempty": True,
            }

            save_manifest(manifest_data)

            # Verify from the manifest immediately after saving.
            if not _manifest_media_is_valid(
                manifest_data,
                media,
            ):
                raise RuntimeError(
                    "post-download verification failed"
                )

            verified_ids.append(media_id)

            print(
                f"✅ Downloaded + verified media {media_id}"
            )

            for path in paths:
                print(f"   💾 {path}")

            polite_delay()

        except FeedbackRequired as e:
            print(
                "🛑 Instagram temporarily refused further requests."
            )
            print(f"Details: {e}")
            failed_ids.append(media_id)

            report_path, _ = _write_instagram_audit(
                target,
                profile_media_count,
                all_medias,
                crawl_complete,
                pages_scanned,
                verified_ids,
                failed_ids,
            )

            print(f"🧾 Partial audit written to: {report_path}")
            return False

        except Exception as e:
            print(
                f"⚠️ Download/verification failed for "
                f"{media_id}: {type(e).__name__}: {e}"
            )
            failed_ids.append(media_id)

    # --------------------------------------------------------
    # FINAL ACCOUNT AUDIT
    # --------------------------------------------------------

    # Re-check every discovered post from disk/manifest, rather than trusting
    # only the success list accumulated above.
    verified_ids = []

    for media in all_medias:
        if _manifest_media_is_valid(
            manifest_data,
            media,
        ):
            verified_ids.append(str(media.id))

    report_path, report = _write_instagram_audit(
        target,
        profile_media_count,
        all_medias,
        crawl_complete,
        pages_scanned,
        verified_ids,
        failed_ids,
    )

    print()
    print("-" * 60)
    print(f"🧾 Instagram audit: @{target}")
    print(
        f"   Feed crawl reached end: "
        f"{'YES' if crawl_complete else 'NO'}"
    )
    print(
        f"   Unique posts discovered: "
        f"{report['discovered_unique_posts']:,}"
    )
    print(
        f"   Posts verified on disk: "
        f"{report['verified_cached_posts']:,}"
    )
    print(
        f"   Missing/unverified: "
        f"{len(report['missing_or_unverified_ids']):,}"
    )
    print(f"   Audit file: {report_path}")

    archive_verified = (
        crawl_complete
        and not report["missing_or_unverified_ids"]
    )

    if archive_verified:
        print(
            f"✅ @{target} archive verified for every post "
            f"returned by the complete cursor crawl."
        )
    else:
        print(
            f"⚠️ @{target} is NOT yet fully verified. "
            f"Run the script again later; verified files will be "
            f"skipped and missing items retried."
        )

    return True


# ============================================================
# TIKTOK DOWNLOAD
# ============================================================

def _migrate_tiktok_cached_entry(
    manifest_data,
    video,
    clean_user,
    video_id,
):
    """Rename an existing TikTok download and repair its manifest entry."""

    global_id = f"tt_{video_id}"
    entry = manifest_data.get(global_id)

    if not isinstance(entry, dict):
        return False, False

    paths = entry.get("local_paths") or []
    if isinstance(paths, str):
        paths = [paths]

    # Do not let a stale manifest record cause a missing file to be skipped.
    if not paths or not all(_valid_file(path) for path in paths):
        return False, False

    posted_at, timestamp_source = _tiktok_posted_at(video, video_id)
    changed = False

    if posted_at is not None:
        new_paths, paths_changed = _rename_local_paths_chronologically(
            paths,
            "tiktok",
            clean_user,
            video_id,
            posted_at,
        )

        if paths_changed or new_paths != paths:
            entry["local_paths"] = new_paths
            changed = True

        posted_iso = _iso_utc(posted_at)
        posted_ts = int(posted_at.timestamp())

        if entry.get("posted_at_utc") != posted_iso:
            entry["posted_at_utc"] = posted_iso
            changed = True

        if entry.get("posted_at_timestamp") != posted_ts:
            entry["posted_at_timestamp"] = posted_ts
            changed = True

        if entry.get("timestamp_source") != timestamp_source:
            entry["timestamp_source"] = timestamp_source
            changed = True

    if changed:
        save_manifest(manifest_data)

    return True, changed


def download_tiktok_feed(
    username,
    manifest_data
):

    clean_user = (
        username
        .strip()
        .lstrip("@")
    )

    profile_url = (
        f"https://www.tiktok.com/"
        f"@{clean_user}"
    )

    print()
    print(
        "=" * 60
    )

    print(
        f"🎵 [TIKTOK SWEEP] "
        f"Crawling public timeline "
        f"for @{clean_user}..."
    )

    print(
        f"🔗 {profile_url}"
    )

    # --------------------------------------------------------
    # Get playlist metadata only
    # --------------------------------------------------------

    cmd_scan = (
        yt_dlp_command()
        +
        [
            "--flat-playlist",
            "--dump-single-json",
            profile_url,
        ]
    )

    try:

        result = subprocess.run(
            cmd_scan,
            capture_output=True,
            text=True,
            check=True
        )

        playlist = json.loads(
            result.stdout
        )

        video_entries = (
            playlist.get("entries")
            or []
        )

        print(
            f"📋 Found "
            f"{len(video_entries)} "
            f"TikTok clips."
        )

    except subprocess.CalledProcessError as e:

        print(
            f"❌ yt-dlp scan failed "
            f"for @{clean_user}"
        )

        print(
            e.stderr
        )

        return

    except json.JSONDecodeError as e:

        print(
            f"❌ yt-dlp returned invalid JSON: {e}"
        )

        return

    except Exception as e:

        print(
            f"❌ TikTok scan failed: {e}"
        )

        return

    # --------------------------------------------------------
    # DOWNLOAD EACH VIDEO
    # --------------------------------------------------------

    for index, video in enumerate(
        video_entries,
        start=1
    ):

        video_id = str(
            video.get("id") or ""
        ).strip()

        if not video_id:
            continue

        global_id = (
            f"tt_{video_id}"
        )

        if global_id in manifest_data:
            cached_ok, migrated = _migrate_tiktok_cached_entry(
                manifest_data,
                video,
                clean_user,
                video_id,
            )

            if cached_ok:
                if migrated:
                    print(
                        f"🗓️ [{index}/{len(video_entries)}] "
                        f"Chronological TikTok name applied: {video_id}"
                    )

                print(
                    f"⏭️ [{index}/{len(video_entries)}] "
                    f"TikTok {video_id} already cached + verified."
                )
                continue

            print(
                f"♻️ [{index}/{len(video_entries)}] "
                f"TikTok cache record is missing/broken; "
                f"re-downloading {video_id}"
            )
            manifest_data.pop(global_id, None)
            save_manifest(manifest_data)

        # Correct TikTok video URL
        video_url = (
            f"https://www.tiktok.com/"
            f"@{clean_user}/video/{video_id}"
        )

        posted_at, timestamp_source = _tiktok_posted_at(
            video,
            video_id,
        )
        chronological_name = _chronological_basename(
            "tiktok",
            clean_user,
            video_id,
            posted_at,
        )

        post_dir = os.path.join(
            DOWNLOAD_ROOT,
            chronological_name or f"tiktok_{clean_user}_{video_id}"
        )

        os.makedirs(
            post_dir,
            exist_ok=True
        )

        output_template = os.path.join(
            post_dir,
            "%(id)s.%(ext)s"
        )

        print()
        print(
            f"📥 [{index}/{len(video_entries)}] "
            f"Downloading TikTok {video_id}"
        )

        # ----------------------------------------------------
        # Download and ask yt-dlp to tell us final pathname
        # ----------------------------------------------------

        cmd_download = (
            yt_dlp_command()
            +
            [
                "--no-playlist",

                "--merge-output-format",
                "mp4",

                "--print",
                "after_move:filepath",

                "-o",
                output_template,

                video_url,
            ]
        )

        try:

            result = subprocess.run(
                cmd_download,
                capture_output=True,
                text=True,
                check=True
            )

            # yt-dlp prints final path because of --print
            possible_paths = []

            for line in result.stdout.splitlines():

                line = line.strip()

                if (
                    line
                    and
                    os.path.isfile(line)
                ):

                    possible_paths.append(
                        line
                    )

            # Fallback: inspect directory
            if not possible_paths:

                for filename in os.listdir(
                    post_dir
                ):

                    candidate = os.path.join(
                        post_dir,
                        filename
                    )

                    if os.path.isfile(candidate):

                        possible_paths.append(
                            candidate
                        )

            if not possible_paths:

                print(
                    f"⚠️ yt-dlp completed but "
                    f"no file was found for "
                    f"{video_id}"
                )

                continue

            possible_paths, _ = _rename_local_paths_chronologically(
                possible_paths,
                "tiktok",
                clean_user,
                video_id,
                posted_at,
            )

            caption = (
                video.get("title")
                or
                video.get("description")
                or
                ""
            )

            manifest_data[
                global_id
            ] = {

                "platform": "tiktok",

                "source_account":
                    f"@{clean_user}",

                "media_id": video_id,

                "media_type": 2,

                "original_caption":
                    caption,

                "source_url":
                    video_url,

                "posted_at_utc":
                    _iso_utc(posted_at),

                "posted_at_timestamp": (
                    int(posted_at.timestamp())
                    if posted_at is not None
                    else None
                ),

                "timestamp_source":
                    timestamp_source,

                "local_paths":
                    possible_paths,

            }

            save_manifest(
                manifest_data
            )

            print(
                f"✅ TikTok {video_id} cached."
            )

            for path in possible_paths:

                print(
                    f"   💾 {path}"
                )

            polite_delay()

        except subprocess.CalledProcessError as e:

            print(
                f"⚠️ TikTok download failed "
                f"for {video_id}"
            )

            if e.stderr:

                print(
                    e.stderr
                )

        except Exception as e:

            print(
                f"⚠️ TikTok error "
                f"for {video_id}: {e}"
            )



# ============================================================
# CHECKPOINTED / STREAMING INSTAGRAM CRAWL
# ============================================================
# These later definitions intentionally replace the older scan-then-download
# implementation above.  Each Instagram page is downloaded/verified BEFORE
# its next cursor is checkpointed.  If the program is interrupted, the last
# completely processed page is durable and the next run can resume from the
# saved cursor instead of restarting the metadata crawl at page 1.


def _instagram_checkpoint_path(target):
    safe_target = _safe_name_component(target)
    return os.path.join(
        DOWNLOAD_ROOT,
        f"instagram_checkpoint_{safe_target}.json",
    )


def _load_instagram_checkpoint(target, target_id):
    path = _instagram_checkpoint_path(target)

    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️ Could not read Instagram checkpoint: {e}")
        return None

    if not isinstance(data, dict):
        return None

    if str(data.get("account") or "") != str(target):
        return None

    if str(data.get("instagram_user_id") or "") != str(target_id):
        print(
            "⚠️ Existing Instagram checkpoint belongs to a different "
            "user ID; starting this account from page 1."
        )
        return None

    next_cursor = data.get("next_cursor")
    if not next_cursor:
        return None

    discovered_ids = data.get("discovered_ids") or []
    if not isinstance(discovered_ids, list):
        discovered_ids = []

    data["discovered_ids"] = [str(x) for x in discovered_ids if x]
    data["pages_completed"] = int(data.get("pages_completed") or 0)
    data["next_cursor"] = str(next_cursor)
    return data


def _save_instagram_checkpoint(
    target,
    target_id,
    next_cursor,
    pages_completed,
    discovered_ids,
):
    path = _instagram_checkpoint_path(target)
    temp_path = path + ".tmp"

    data = {
        "version": 1,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "account": target,
        "instagram_user_id": str(target_id),
        "pages_completed": int(pages_completed),
        "next_cursor": str(next_cursor or ""),
        "discovered_ids": list(dict.fromkeys(str(x) for x in discovered_ids)),
    }

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    os.replace(temp_path, path)
    return path


def _clear_instagram_checkpoint(target):
    path = _instagram_checkpoint_path(target)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        print(f"⚠️ Could not remove completed checkpoint: {e}")


def _manifest_entry_files_valid(entry):
    if not isinstance(entry, dict):
        return False

    paths = entry.get("local_paths") or []
    if isinstance(paths, str):
        paths = [paths]

    return bool(paths) and all(_valid_file(path) for path in paths)


def _verified_instagram_ids_from_manifest(manifest_data, discovered_ids):
    verified = []
    for media_id in discovered_ids:
        key = _find_instagram_manifest_key(manifest_data, str(media_id))
        if key and _manifest_entry_files_valid(manifest_data.get(key)):
            verified.append(str(media_id))
    return verified


def _write_instagram_checkpoint_audit(
    target,
    profile_media_count,
    discovered_ids,
    crawl_complete,
    pages_scanned,
    manifest_data,
    failed_ids,
):
    discovered_ids = list(dict.fromkeys(str(x) for x in discovered_ids))
    verified_ids = _verified_instagram_ids_from_manifest(
        manifest_data,
        discovered_ids,
    )
    missing_ids = sorted(set(discovered_ids) - set(verified_ids))

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "account": target,
        "profile_media_count": profile_media_count,
        "crawl_complete": bool(crawl_complete),
        "pages_scanned": int(pages_scanned),
        "discovered_unique_posts": len(discovered_ids),
        "verified_cached_posts": len(set(verified_ids)),
        "failed_download_ids": sorted(set(str(x) for x in failed_ids)),
        "missing_or_unverified_ids": missing_ids,
    }

    report_path = os.path.join(
        DOWNLOAD_ROOT,
        f"instagram_audit_{target}.json",
    )
    temp_path = report_path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=4, ensure_ascii=False)

    os.replace(temp_path, report_path)
    return report_path, report


def _process_instagram_media(
    cl,
    target,
    media,
    manifest_data,
    display_index=None,
):
    """
    Rename/verify an existing Instagram item or download it if needed.

    Returns:
      (verified_ok, stop_instagram_phase)
    """
    media_id = str(media.id)
    manifest_id = f"ig_{media_id}"
    prefix = f"[{display_index}] " if display_index is not None else ""

    migrated = _migrate_instagram_cached_entry(
        manifest_data,
        media,
        target,
    )
    if migrated:
        print(f"🗓️ {prefix}Chronological name applied: {media_id}")

    if _manifest_media_is_valid(manifest_data, media):
        print(f"⏭️ {prefix}Verified cached: {media_id}")
        return True, False

    if _find_instagram_manifest_key(manifest_data, media_id):
        print(
            f"♻️ {prefix}Cache record is incomplete/broken; "
            f"re-downloading {media_id}"
        )
        _remove_bad_manifest_entry(manifest_data, media_id)

    print()
    print(f"📥 {prefix}Instagram media {media_id}")

    posted_at = _instagram_posted_at(media)
    chronological_name = _chronological_basename(
        "instagram",
        target,
        media_id,
        posted_at,
    )
    post_dir = os.path.join(
        DOWNLOAD_ROOT,
        chronological_name or f"ig_{target}_{media_id}",
    )
    os.makedirs(post_dir, exist_ok=True)

    try:
        if media.media_type == 1:
            print("🖼️ Downloading photo...")
            downloaded = cl.photo_download(media.id, folder=post_dir)
            paths = [str(downloaded)]

        elif media.media_type == 2:
            print("🎥 Downloading video/reel...")
            downloaded = cl.video_download(media.id, folder=post_dir)
            paths = [str(downloaded)]

        elif media.media_type == 8:
            print("🖼️🖼️ Downloading carousel...")
            downloaded = cl.album_download(media.id, folder=post_dir)
            paths = [str(path) for path in downloaded]

        else:
            print(f"⚠️ Unknown media type: {media.media_type}")
            return False, False

        expected_count = _media_expected_file_count(media)

        if not paths or not all(_valid_file(path) for path in paths):
            raise RuntimeError(
                "download returned missing or empty local file(s)"
            )

        if expected_count is not None and len(paths) < expected_count:
            raise RuntimeError(
                f"expected {expected_count} file(s) for this post "
                f"but only found {len(paths)}"
            )

        paths, _ = _rename_local_paths_chronologically(
            paths,
            "instagram",
            target,
            media_id,
            posted_at,
        )

        caption = media.caption_text or ""
        manifest_data[manifest_id] = {
            "platform": "instagram",
            "source_account": target,
            "media_id": media_id,
            "media_type": media.media_type,
            "shortcode": getattr(media, "code", None),
            "original_caption": caption,
            "posted_at_utc": _iso_utc(posted_at),
            "posted_at_timestamp": (
                int(posted_at.timestamp())
                if posted_at is not None
                else None
            ),
            "timestamp_source": (
                "instagram:taken_at"
                if posted_at is not None
                else None
            ),
            "local_paths": paths,
            "verified_nonempty": True,
        }
        save_manifest(manifest_data)

        if not _manifest_media_is_valid(manifest_data, media):
            raise RuntimeError("post-download verification failed")

        print(f"✅ Downloaded + verified media {media_id}")
        for path in paths:
            print(f"   💾 {path}")

        polite_delay()
        return True, False

    except FeedbackRequired as e:
        print("🛑 Instagram temporarily refused further requests.")
        print(f"Details: {e}")
        return False, True

    except Exception as e:
        print(
            f"⚠️ Download/verification failed for {media_id}: "
            f"{type(e).__name__}: {e}"
        )
        return False, False


def download_instagram_account(cl, target, manifest_data):
    """
    Stream an Instagram feed page-by-page with durable cursor checkpoints.

    A page's next cursor is saved only AFTER every item on that page has been
    handled.  If Ctrl+C, a timeout, or a crash occurs while processing a page,
    the next run re-fetches that page; already-verified files are simply
    skipped, so no completed download is lost.
    """
    target = target.strip().lstrip("@")

    print()
    print("=" * 60)
    print(
        f"🎯 [INSTAGRAM CHECKPOINTED SWEEP] "
        f"Crawling + archiving @{target} page-by-page..."
    )

    try:
        print(f"🔎 Resolving @{target} using private API...")
        user = cl.user_info_by_username_v1(target)
        target_id = str(user.pk)
        profile_media_count = getattr(user, "media_count", None)

        print(f"✅ Found @{user.username}")
        print(f"🆔 Instagram user ID: {target_id}")
        if profile_media_count is not None:
            print(
                f"🔢 Profile-reported media count: "
                f"{profile_media_count:,}"
            )

    except FeedbackRequired as e:
        print("🛑 Instagram temporarily refused requests.")
        print(f"Details: {e}")
        return False
    except ClientError as e:
        print(f"❌ Instagram API error for @{target}: {e}")
        return True
    except Exception as e:
        print(
            f"❌ Could not retrieve @{target}: "
            f"{type(e).__name__}: {e}"
        )
        return True

    checkpoint = _load_instagram_checkpoint(target, target_id)

    if checkpoint:
        cursor = checkpoint["next_cursor"]
        pages_completed = checkpoint["pages_completed"]
        discovered_ids = checkpoint["discovered_ids"]
        seen_media_ids = set(discovered_ids)
        print()
        print(
            f"♻️ RESUMING @{target} from saved checkpoint: "
            f"{pages_completed:,} page(s) already completed, "
            f"{len(discovered_ids):,} post IDs recorded."
        )
        print(
            f"   Next request will continue after completed "
            f"page {pages_completed:,}."
        )
    else:
        cursor = ""
        pages_completed = 0
        discovered_ids = []
        seen_media_ids = set()
        print("🆕 No usable pagination checkpoint; starting at page 1.")

    seen_cursors = set()
    failed_ids = []
    crawl_complete = False

    while True:
        request_page_number = pages_completed + 1

        print()
        print(
            f"📡 @{target}: requesting Instagram page "
            f"{request_page_number}..."
        )

        try:
            page, next_cursor = cl.user_medias_paginated_v1(
                target_id,
                amount=INSTAGRAM_PAGE_SIZE,
                end_cursor=cursor,
            )

        except KeyboardInterrupt:
            print()
            print(
                f"🛟 Interrupted. Checkpoint preserved through "
                f"completed page {pages_completed:,}."
            )
            if pages_completed:
                print(
                    "   Run this same script again and it will resume "
                    "from the saved cursor."
                )
            raise

        except AttributeError:
            if pages_completed != 0 or cursor:
                print(
                    "⚠️ This instagrapi version cannot resume cursor "
                    "pagination. Starting its built-in full crawl instead."
                )

            medias = cl.user_medias_v1(target_id, amount=0)
            page = list(medias or [])
            next_cursor = None
            request_page_number = 1

        except FeedbackRequired as e:
            print("🛑 Instagram temporarily refused pagination.")
            print(f"Details: {e}")
            break

        except ClientError as e:
            print(
                f"❌ Instagram pagination API error for @{target}: {e}"
            )
            break

        except Exception as e:
            print(
                f"❌ Pagination failed for @{target}: "
                f"{type(e).__name__}: {e}"
            )
            print(
                f"🛟 Checkpoint remains at completed page "
                f"{pages_completed:,}."
            )
            break

        page = list(page or [])
        new_on_page = 0

        # Process the page before advancing its cursor. If interrupted here,
        # this same page is fetched again next time and verified items are skipped.
        for item_number, media in enumerate(page, start=1):
            media_id = str(media.id)
            if media_id not in seen_media_ids:
                seen_media_ids.add(media_id)
                discovered_ids.append(media_id)
                new_on_page += 1

            ok, stop_phase = _process_instagram_media(
                cl,
                target,
                media,
                manifest_data,
                display_index=(
                    f"page {request_page_number}, "
                    f"item {item_number}/{len(page)}"
                ),
            )

            if not ok:
                failed_ids.append(media_id)

            if stop_phase:
                report_path, _ = _write_instagram_checkpoint_audit(
                    target,
                    profile_media_count,
                    discovered_ids,
                    False,
                    pages_completed,
                    manifest_data,
                    failed_ids,
                )
                print(f"🧾 Partial audit written to: {report_path}")
                return False

        pages_completed = request_page_number

        print(
            f"📋 Page {pages_completed}: {len(page):,} returned, "
            f"{new_on_page:,} newly discovered this run, "
            f"{len(discovered_ids):,} unique IDs recorded."
        )

        if not next_cursor:
            crawl_complete = True
            _clear_instagram_checkpoint(target)
            print(
                "✅ Instagram returned no next cursor. "
                "Feed crawl reached the end."
            )
            break

        next_cursor = str(next_cursor)

        if next_cursor == cursor or next_cursor in seen_cursors:
            print(
                "⚠️ Instagram repeated a pagination cursor. "
                "Stopping without marking the crawl complete."
            )
            break

        if cursor:
            seen_cursors.add(cursor)

        cursor = next_cursor
        checkpoint_path = _save_instagram_checkpoint(
            target,
            target_id,
            cursor,
            pages_completed,
            discovered_ids,
        )
        print(
            f"💾 Checkpoint saved after page {pages_completed} "
            f"({len(discovered_ids):,} IDs)."
        )

        delay = random.uniform(
            INSTAGRAM_PAGE_DELAY_MIN,
            INSTAGRAM_PAGE_DELAY_MAX,
        )
        print(
            f"⏳ Waiting {delay:.1f}s before the next "
            f"Instagram metadata page..."
        )
        time.sleep(delay)

    report_path, report = _write_instagram_checkpoint_audit(
        target,
        profile_media_count,
        discovered_ids,
        crawl_complete,
        pages_completed,
        manifest_data,
        failed_ids,
    )

    print()
    print("-" * 60)
    print(f"🧾 Instagram audit: @{target}")
    print(
        f"   Feed crawl reached end: "
        f"{'YES' if crawl_complete else 'NO'}"
    )
    print(
        f"   Unique post IDs recorded: "
        f"{report['discovered_unique_posts']:,}"
    )
    print(
        f"   Posts verified on disk: "
        f"{report['verified_cached_posts']:,}"
    )
    print(
        f"   Missing/unverified: "
        f"{len(report['missing_or_unverified_ids']):,}"
    )
    print(f"   Audit file: {report_path}")

    if (
        profile_media_count is not None
        and profile_media_count > 0
        and len(discovered_ids) < profile_media_count
    ):
        print(
            f"⚠️ COUNT CHECK: profile reports {profile_media_count:,} "
            f"media while the crawl recorded {len(discovered_ids):,}."
        )
        print(
            "   Deleted/inaccessible/collaboration content can make those "
            "numbers differ, so the profile count alone is not proof of a miss."
        )

    archive_verified = (
        crawl_complete
        and not report["missing_or_unverified_ids"]
    )

    if archive_verified:
        print(
            f"✅ @{target} archive verified for every post returned "
            f"by the complete cursor crawl."
        )
    else:
        print(
            f"⚠️ @{target} is NOT yet fully verified. "
            f"Run the script again later; verified files will be skipped "
            f"and missing items retried."
        )

    # Continue to the next Instagram account unless Instagram explicitly
    # blocked the download phase above. An incomplete pagination crawl keeps
    # its checkpoint so a later run can resume it.
    return True


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "🚀 Starting social-media backup..."
    )

    print(
        f"📂 Download directory:\n"
        f"{DOWNLOAD_ROOT}"
    )

    manifest_data = load_manifest()

    print(
        f"📚 Existing manifest entries: "
        f"{len(manifest_data)}"
    )

    # ========================================================
    # INSTAGRAM
    # ========================================================

    cl = login_instagram()

    if cl:

        for target in INACTIVE_INSTA_MAINS:

            keep_running = (
                download_instagram_account(
                    cl,
                    target,
                    manifest_data
                )
            )

            if not keep_running:

                print()
                print(
                    "🛑 Stopping Instagram phase."
                )

                break

    else:

        print()
        print(
            "⚠️ Instagram login unavailable."
        )

        print(
            "➡️ Continuing with TikTok."
        )

    # ========================================================
    # TIKTOK
    # ========================================================

    for tt_target in TIKTOK_MAINS:

        download_tiktok_feed(
            tt_target,
            manifest_data
        )

    # ========================================================
    # COMPLETE
    # ========================================================

    save_manifest(
        manifest_data
    )

    print()
    print(
        "=" * 60
    )

    print(
        "🏁 Backup pass finished. Review the Instagram audit "
        "files above for verification status."
    )

    print(
        f"📦 Total manifest entries: "
        f"{len(manifest_data)}"
    )

    print(
        f"📁 Data directory:\n"
        f"{DOWNLOAD_ROOT}"
    )


if __name__ == "__main__":
    main()
