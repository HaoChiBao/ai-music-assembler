"""CLI: upload a single existing video file to YouTube with placeholder metadata.

A minimal way to test the OAuth flow + YouTube upload without running the full
pipeline or generating metadata. Example:

    python3 -m music_assembler.upload_test path/to/video.mp4 --privacy unlisted

Needs the upload extra (``pip install ".[youtube]"``) and a Google OAuth client
secret (Desktop app recommended) in the project root or via --client-secret.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from music_assembler import __version__
from music_assembler.youtube_upload import (
    DEFAULT_CATEGORY_ID,
    DEFAULT_PRIVACY,
    VALID_PRIVACY,
    find_client_secret,
    upload_video,
)

DEFAULT_TOKEN_FILE = Path("youtube_token.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="upload-video-test",
        description="Upload one existing video file to YouTube with placeholder title/description (for testing).",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("video", type=Path, help="Path to the video file to upload (e.g. a .mp4).")
    p.add_argument(
        "--title",
        default=None,
        help="Video title (default: a timestamped placeholder).",
    )
    p.add_argument(
        "--description",
        default="Placeholder description — test upload from ai-music-assembler.",
        help="Video description (default: placeholder text).",
    )
    p.add_argument(
        "--privacy",
        choices=VALID_PRIVACY,
        default="unlisted",
        help="Upload privacy status (default: unlisted for testing).",
    )
    p.add_argument(
        "--thumbnail",
        type=Path,
        default=None,
        help="Optional image to set as the custom thumbnail.",
    )
    p.add_argument(
        "--tags",
        default=None,
        help="Comma-separated tags (e.g. 'test,lofi').",
    )
    p.add_argument(
        "--category-id",
        default=DEFAULT_CATEGORY_ID,
        help=f"YouTube category id (default: {DEFAULT_CATEGORY_ID} = Music).",
    )
    p.add_argument(
        "--made-for-kids",
        action="store_true",
        help="Mark the video as made for kids (default: not).",
    )
    p.add_argument(
        "--client-secret",
        type=Path,
        default=None,
        help="Google OAuth client secret JSON. Default: client_secret*.json in the project root.",
    )
    p.add_argument(
        "--token-file",
        type=Path,
        default=DEFAULT_TOKEN_FILE,
        help=f"Where to cache the OAuth token (default: {DEFAULT_TOKEN_FILE}).",
    )
    p.add_argument(
        "--oauth-port",
        type=int,
        default=8080,
        help="Local port for the OAuth redirect, http://localhost:<port>/ (default: 8080).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = Path.cwd().resolve()

    video = args.video.expanduser().resolve()
    if not video.is_file():
        print(f"error: video not found: {video}", file=sys.stderr)
        return 2

    try:
        client_secret = find_client_secret(args.client_secret, project_root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    title = args.title or f"Test upload {datetime.now():%Y-%m-%d %H:%M:%S}"
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    thumbnail = args.thumbnail.expanduser().resolve() if args.thumbnail else None
    if thumbnail and not thumbnail.is_file():
        print(f"error: thumbnail not found: {thumbnail}", file=sys.stderr)
        return 2

    def on_progress(p: float) -> None:
        print(f"\r  uploading… {p * 100:5.1f}%", end="", flush=True, file=sys.stderr)

    print(f"Uploading {video.name} as '{title}' ({args.privacy})…")
    print("(a browser may open for authorization on first run)")
    try:
        response = upload_video(
            video,
            title=title,
            description=args.description,
            client_secret=client_secret,
            token_path=args.token_file.resolve(),
            privacy=args.privacy,
            category_id=args.category_id,
            tags=tags,
            made_for_kids=args.made_for_kids,
            thumbnail_path=thumbnail,
            oauth_port=args.oauth_port,
            on_progress=on_progress,
        )
        print(file=sys.stderr)
    except Exception as e:
        print(f"\nerror: upload failed: {e}", file=sys.stderr)
        return 1

    video_id = response.get("id")
    print(f"Uploaded: https://youtu.be/{video_id}  (privacy: {args.privacy})")
    if response.get("_thumbnail_warning"):
        print(
            "note: custom thumbnail was not set (channel may need verification): "
            f"{response['_thumbnail_warning']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
