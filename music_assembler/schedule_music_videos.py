"""CLI: mass-upload the pending videos in the registry, scheduled to publish over time.

Reads ``pending`` entries written by ``generate-music-videos``, uploads each to YouTube,
and (by default) schedules them to go public at staggered times (``--start`` +
``--interval-hours`` per video). On success each entry is flipped to ``uploaded`` in the
registry with its YouTube id + scheduled publish time, and its title is recorded so it's
never reused.

Run from the project root. Needs ``pip install ".[youtube]"`` and a Google OAuth client
secret (Desktop app). The first run opens a browser to authorize the channel.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from music_assembler import __version__
from music_assembler.progress_bars import MultiProgress
from music_assembler.video_registry import DEFAULT_REGISTRY_FILE, VideoRegistry

# Kept here so argparse and dry-run never import google-api-python-client (slow startup).
DEFAULT_CATEGORY_ID = "10"
DEFAULT_PRIVACY = "private"
VALID_PRIVACY = ("private", "unlisted", "public")
DEFAULT_USED_TITLES_FILE = Path("youtube_used_titles.txt")

DEFAULT_TOKEN_FILE = Path("youtube_token.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="schedule-music-videos",
        description=(
            "Upload all pending videos from the registry, scheduled to publish at staggered "
            "times, then mark them uploaded. Run from the project root."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_FILE,
        help=f"Registry txt file of videos to upload (default: {DEFAULT_REGISTRY_FILE}).",
    )
    p.add_argument(
        "--start",
        default=None,
        metavar="'YYYY-MM-DD HH:MM'",
        help="Local time the FIRST video should publish (default: tomorrow 09:00 local).",
    )
    p.add_argument(
        "--interval-hours",
        type=float,
        default=24.0,
        help="Hours between each scheduled publish time (default: 24).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only upload the first N pending videos (default: all).",
    )
    p.add_argument(
        "--no-schedule",
        action="store_true",
        help="Upload immediately with --privacy instead of scheduling (default: schedule public).",
    )
    p.add_argument(
        "--privacy",
        choices=VALID_PRIVACY,
        default=DEFAULT_PRIVACY,
        help=f"Privacy when --no-schedule is used (default: {DEFAULT_PRIVACY}).",
    )
    p.add_argument(
        "--category-id",
        default=DEFAULT_CATEGORY_ID,
        help=f"YouTube category id (default: {DEFAULT_CATEGORY_ID} = Music).",
    )
    p.add_argument("--tags", default=None, help="Comma-separated tags applied to every video.")
    p.add_argument("--made-for-kids", action="store_true", help="Mark videos as made for kids.")
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
        help=f"Where to cache the OAuth token (default: {DEFAULT_TOKEN_FILE}).",
    )
    p.add_argument("--oauth-port", type=int, default=8080, help="Local OAuth redirect port (default: 8080).")
    p.add_argument(
        "--used-titles-file",
        type=Path,
        default=DEFAULT_USED_TITLES_FILE,
        help=f"Log of used titles to append to on success (default: {DEFAULT_USED_TITLES_FILE}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the upload/schedule plan without uploading anything.",
    )
    p.add_argument(
        "--upload-retries",
        type=int,
        default=3,
        metavar="N",
        help="Total upload attempts per video for transient errors (default: 3).",
    )
    p.add_argument(
        "--retry-delay",
        type=float,
        default=30.0,
        metavar="SEC",
        help="Base seconds to wait between retries; multiplied by attempt number (default: 30).",
    )
    return p


def _parse_start(value: str | None) -> datetime:
    """Return a timezone-aware local datetime for the first publish time."""
    local_tz = datetime.now().astimezone().tzinfo
    if not value:
        tomorrow = (datetime.now() + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        return tomorrow.replace(tzinfo=local_tz)
    text = value.strip().replace(" ", "T")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz)
    return dt


def _to_rfc3339_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    print("schedule-music-videos: starting…", file=sys.stderr, flush=True)
    load_dotenv(find_dotenv(usecwd=True))
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = Path.cwd().resolve()

    print(f"Reading registry {args.registry}…", file=sys.stderr, flush=True)
    registry = VideoRegistry(args.registry.resolve())
    pending = registry.pending()
    if args.limit is not None:
        pending = pending[: max(0, args.limit)]
    if not pending:
        print(f"No pending videos in {registry.path}. Nothing to do.")
        return 0

    try:
        start_dt = _parse_start(args.start)
    except ValueError as e:
        print(f"error: could not parse --start: {e}", file=sys.stderr)
        return 2

    # Build the schedule (one publish time per pending video).
    plan: list[tuple] = []
    for i, entry in enumerate(pending):
        publish_dt = start_dt + timedelta(hours=args.interval_hours * i)
        publish_at = "" if args.no_schedule else _to_rfc3339_utc(publish_dt)
        plan.append((entry, publish_dt, publish_at))

    print(f"{len(plan)} pending video(s) in {registry.path}:")
    for entry, publish_dt, publish_at in plan:
        when = "now (no schedule)" if args.no_schedule else publish_dt.strftime("%Y-%m-%d %H:%M %Z")
        title = entry.title or "(no title)"
        print(f"  {entry.id}  ->  publish {when}\n      title: {title}")

    if args.dry_run:
        print("\n--dry-run set: nothing uploaded.")
        return 0

    print(
        f"\nUploading {len(plan)} video(s) to YouTube "
        f"(loading Google client — may take a few seconds)…",
        file=sys.stderr,
        flush=True,
    )
    from music_assembler.youtube_metadata import record_used_title
    from music_assembler.youtube_upload import find_client_secret, upload_video_with_retry

    try:
        client_secret = find_client_secret(args.client_secret, project_root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    used_titles_path = args.used_titles_file.resolve()
    token_path = args.token_file.resolve()

    uploaded = 0
    failed = 0
    labels = [entry.id for entry, _, _ in plan]
    results: dict[int, tuple[str, str]] = {}  # index -> (youtube_id, when_str)

    with MultiProgress(labels) as bars:
        for i, (entry, _publish_dt, publish_at) in enumerate(plan):
            video_path = Path(entry.video)
            if not video_path.is_file():
                bars.update(i, 0.0, f"FAILED: video not found")
                failed += 1
                continue

            title = entry.title or entry.id
            description = ""
            if entry.description and Path(entry.description).is_file():
                description = Path(entry.description).read_text(encoding="utf-8").strip()
            thumb = Path(entry.thumbnail) if entry.thumbnail and Path(entry.thumbnail).is_file() else None

            bars.update(i, 0.0, "uploading…")

            def on_progress(p: float, *, idx: int = i) -> None:
                bars.update(idx, p * 100.0, "uploading…")

            def on_retry(attempt: int, attempts: int, err: BaseException, *, idx: int = i) -> None:
                bars.update(idx, 0.0, f"retry {attempt}/{attempts} ({err})…")

            try:
                response = upload_video_with_retry(
                    video_path,
                    max_attempts=args.upload_retries,
                    retry_delay_sec=args.retry_delay,
                    on_retry=on_retry,
                    title=title,
                    description=description,
                    client_secret=client_secret,
                    token_path=token_path,
                    privacy=args.privacy,
                    category_id=args.category_id,
                    tags=tags,
                    made_for_kids=args.made_for_kids,
                    thumbnail_path=thumb,
                    publish_at=publish_at or None,
                    oauth_port=args.oauth_port,
                    on_progress=on_progress,
                )
            except Exception as e:
                bars.update(i, bars.pcts[i], f"FAILED: {e}")
                failed += 1
                continue

            youtube_id = response.get("id", "")
            registry.mark_uploaded(entry.id, youtube_id=youtube_id, publish_at=publish_at)
            if title:
                record_used_title(title, used_titles_path)
            uploaded += 1
            when = "immediately" if args.no_schedule else f"scheduled {publish_at}"
            msg = "done"
            if response.get("_thumbnail_warning"):
                msg = "done (thumbnail skipped)"
            bars.update(i, 100.0, msg)
            results[i] = (youtube_id, when)

    print(file=sys.stderr)
    for i, (entry, _, _) in enumerate(plan):
        if i in results:
            youtube_id, when = results[i]
            print(f"  {entry.id}: https://youtu.be/{youtube_id}  ({when})")

    print(f"\nUploaded {uploaded}/{len(plan)} ({failed} failed). Registry updated: {registry.path}")
    return 0 if uploaded > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
