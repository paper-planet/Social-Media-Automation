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
#   IG_SCRAPER_USER
#   IG_SCRAPER_PASS
#   IG_SCRAPER_TOTP
SCRAPER_USER = os.environ.get("IG_SCRAPER_USER", "")
SCRAPER_PASS = os.environ.get("IG_SCRAPER_PASS", "")
SCRAPER_TOTP = os.environ.get("IG_SCRAPER_TOTP", "")


# Instagram exhaustive archive settings
# A page is intentionally small/moderate so the private endpoint is not
# asked for thousands of items in one request. Pagination continues until
# Instagram stops returning a next cursor.
INSTAGRAM_PAGE_SIZE = 33
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

        post_dir = os.path.join(
            DOWNLOAD_ROOT,
            f"ig_{target}_{media_id}",
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

            print(
                f"⏭️ [{index}/{len(video_entries)}] "
                f"TikTok {video_id} already cached."
            )

            continue

        # Correct TikTok video URL
        video_url = (
            f"https://www.tiktok.com/"
            f"@{clean_user}/video/{video_id}"
        )

        post_dir = os.path.join(
            DOWNLOAD_ROOT,
            f"tiktok_{clean_user}_{video_id}"
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
