import json
import os
import time
import random
import subprocess
import sys

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

DOWNLOAD_ROOT = r"C:\"

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
SCRAPER_USER = ""
SCRAPER_PASS = ""

# 2FA seed / secret, NOT the current six-digit code
SCRAPER_TOTP = ""


# Number of Instagram posts to request per account
INSTAGRAM_MEDIA_LIMIT = 150


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

        code = generate_2fa(
            SCRAPER_TOTP
        )

        if code:

            print(
                f"🔢 Generated 2FA code: {code}"
            )

            cl.login(
                SCRAPER_USER,
                SCRAPER_PASS,
                verification_code=code
            )

        else:

            cl.login(
                SCRAPER_USER,
                SCRAPER_PASS
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

def download_instagram_account(
    cl,
    target,
    manifest_data
):

    target = target.strip().lstrip("@")

    print()
    print(
        "=" * 60
    )

    print(
        f"🎯 [INSTAGRAM SWEEP] "
        f"Crawling feed history for @{target}..."
    )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Explicitly use PRIVATE MOBILE API.
    #
    # Do not use:
    #
    #     user_id_from_username()
    #     user_medias()
    #
    # because those high-level methods may fall back to
    # public GraphQL in some circumstances.
    # --------------------------------------------------------

    try:

        print(
            f"🔎 Resolving @{target} "
            f"using private API..."
        )

        user = cl.user_info_by_username_v1(
            target
        )

        target_id = str(
            user.pk
        )

        print(
            f"✅ Found @{user.username}"
        )

        print(
            f"🆔 Instagram user ID: {target_id}"
        )

        print(
            f"📡 Requesting up to "
            f"{INSTAGRAM_MEDIA_LIMIT} posts..."
        )

        all_medias = cl.user_medias_v1(
            target_id,
            amount=INSTAGRAM_MEDIA_LIMIT
        )

        print(
            f"📋 Instagram returned "
            f"{len(all_medias)} posts."
        )

    except FeedbackRequired as e:

        print(
            "🛑 Instagram temporarily refused requests."
        )

        print(
            f"Details: {e}"
        )

        return False

    except ClientError as e:

        print(
            f"❌ Instagram API error "
            f"for @{target}: {e}"
        )

        return True

    except Exception as e:

        print(
            f"❌ Could not retrieve "
            f"@{target}: {type(e).__name__}: {e}"
        )

        return True

    # --------------------------------------------------------
    # DOWNLOAD POSTS
    # --------------------------------------------------------

    for index, media in enumerate(
        all_medias,
        start=1
    ):

        media_id = str(
            media.id
        )

        manifest_id = (
            f"ig_{media_id}"
        )

        # Support old manifest entries that used raw IG IDs
        if (
            manifest_id in manifest_data
            or
            media_id in manifest_data
        ):

            print(
                f"⏭️ [{index}/{len(all_medias)}] "
                f"Already cached: {media_id}"
            )

            continue

        print()
        print(
            f"📥 [{index}/{len(all_medias)}] "
            f"Instagram media {media_id}"
        )

        post_dir = os.path.join(
            DOWNLOAD_ROOT,
            f"ig_{target}_{media_id}"
        )

        os.makedirs(
            post_dir,
            exist_ok=True
        )

        try:

            # ------------------------------------------------
            # PHOTO
            # ------------------------------------------------

            if media.media_type == 1:

                print(
                    "🖼️ Downloading photo..."
                )

                downloaded = cl.photo_download(
                    media.id,
                    folder=post_dir
                )

                paths = [
                    str(downloaded)
                ]

            # ------------------------------------------------
            # VIDEO / REEL
            # ------------------------------------------------

            elif media.media_type == 2:

                print(
                    "🎥 Downloading video/reel..."
                )

                downloaded = cl.video_download(
                    media.id,
                    folder=post_dir
                )

                paths = [
                    str(downloaded)
                ]

            # ------------------------------------------------
            # CAROUSEL / ALBUM
            # ------------------------------------------------

            elif media.media_type == 8:

                print(
                    "🖼️🖼️ Downloading carousel..."
                )

                downloaded = cl.album_download(
                    media.id,
                    folder=post_dir
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

                continue

            caption = (
                media.caption_text
                or ""
            )

            manifest_data[
                manifest_id
            ] = {

                "platform": "instagram",

                "source_account": target,

                "media_id": media_id,

                "media_type": media.media_type,

                "shortcode": getattr(
                    media,
                    "code",
                    None
                ),

                "original_caption": caption,

                "local_paths": paths,

            }

            save_manifest(
                manifest_data
            )

            print(
                f"✅ Cached media {media_id}"
            )

            for path in paths:

                print(
                    f"   💾 {path}"
                )

            polite_delay()

        except FeedbackRequired as e:

            print(
                "🛑 Instagram temporarily "
                "refused further requests."
            )

            print(
                f"Details: {e}"
            )

            return False

        except Exception as e:

            print(
                f"⚠️ Download failed for "
                f"{media_id}: "
                f"{type(e).__name__}: {e}"
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
        "🏁 Master Data Convergence complete."
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
