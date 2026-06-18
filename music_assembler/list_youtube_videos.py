"""CLI: list videos on the authorized YouTube channel (optionally scheduled only)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from music_assembler import __version__
from music_assembler.youtube_channel import YouTubeVideoInfo, list_channel_videos
from music_assembler.youtube_upload import find_client_secret

DEFAULT_TOKEN_FILE = Path("youtube_token.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="list-youtube-videos",
        description=(
            "List titles (and scheduled publish times) for videos on the OAuth-authorized "
            "YouTube channel. Run from the project root."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--scheduled-only",
        action="store_true",
        help="Only show private videos with a future publishAt (YouTube scheduled uploads).",
    )
    p.add_argument(
        "--client-secret",
        type=Path,
        default=None,
        help="Google OAuth client secret JSON (Desktop app). Default: client_secret*.json in root.",
    )
    p.add_argument(
        "--token-file",
        type=Path,
        default=DEFAULT_TOKEN_FILE,
        help=f"Cached OAuth token (default: {DEFAULT_TOKEN_FILE}).",
    )
    p.add_argument("--oauth-port", type=int, default=8080, help="Local OAuth redirect port (default: 8080).")
    return p


def _print_video(video: YouTubeVideoInfo, *, scheduled_only: bool) -> None:
    if scheduled_only:
        when = video.publish_at_local()
        when_str = when.strftime("%Y-%m-%d %H:%M %Z") if when else "(unknown time)"
        print(video.title)
        print(f"  scheduled: {when_str}")
        print(f"  {video.url}")
        return
    extra = video.privacy_status or "unknown"
    if video.is_scheduled and video.publish_at_local():
        when_str = video.publish_at_local().strftime("%Y-%m-%d %H:%M %Z")
        extra = f"scheduled {when_str}"
    print(f"{video.title}  [{extra}]")
    print(f"  {video.url}")


def main(argv: list[str] | None = None) -> int:
    print("list-youtube-videos: starting…", file=sys.stderr, flush=True)
    load_dotenv(find_dotenv(usecwd=True))
    args = build_parser().parse_args(argv)
    project_root = Path.cwd().resolve()

    try:
        client_secret = find_client_secret(args.client_secret, project_root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    label = "scheduled" if args.scheduled_only else "channel"
    print(f"Fetching {label} videos from YouTube…", file=sys.stderr, flush=True)

    try:
        videos = list_channel_videos(
            client_secret,
            args.token_file.resolve(),
            scheduled_only=args.scheduled_only,
            oauth_port=args.oauth_port,
        )
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not videos:
        if args.scheduled_only:
            print("No scheduled videos found on this channel.")
        else:
            print("No videos found on this channel.")
        return 0

    if args.scheduled_only:
        print(f"{len(videos)} scheduled video(s):\n")
    else:
        print(f"{len(videos)} video(s):\n")

    for video in videos:
        _print_video(video, scheduled_only=args.scheduled_only)
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
