"""CLI: build a music-mix video (same as make-short-music-video) and upload it to YouTube.

Flow: mix audio + write tracklist → generate a YouTube title and description with OpenAI
(prompt in prompts/youtube_metadata.txt, tracklist appended for chapters) → render the
thumbnail + encode the video → upload to YouTube. Metadata is generated BEFORE the slow
encode, so a bad key / API error fails fast. Use ``--no-upload`` to build + preview only.

Run from the project root. Needs ``OPENAI_API_KEY`` (.env) for metadata (or pass
``--metadata-provider gemini`` to use ``GEMINI_API_KEY``) and a Google OAuth client secret
(Desktop app) for the upload. Install upload deps with: pip install ".[youtube]"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from music_assembler import __version__
from music_assembler.bottom_text_overlay import resolve_font_key
from music_assembler.config import AssemblerConfig, AssemblerPaths, DurationBounds, TextOverlayStyle
from music_assembler.ffmpeg_util import FFmpegNotFoundError, find_ffmpeg, find_ffprobe
from music_assembler.pipeline import assemble
from music_assembler.youtube_metadata import (
    DEFAULT_PROMPT_FILE,
    DEFAULT_USED_TITLES_FILE,
    load_used_titles,
    record_used_title,
)
from music_assembler.youtube_upload import (
    DEFAULT_CATEGORY_ID,
    DEFAULT_PRIVACY,
    VALID_PRIVACY,
    find_client_secret,
    upload_video,
)

DEFAULT_MIN_SEC = 75 * 60.0
DEFAULT_MAX_SEC = 90 * 60.0
DEFAULT_SONGS_DIR = Path("music")
DEFAULT_OUTPUT_DIR = Path("music-video")
DEFAULT_IMAGES_DIR = Path("post-processed")
DEFAULT_TITLE_FONT_SIZE = 46
DEFAULT_TITLE_FONT_WEIGHT = 400
DEFAULT_TOKEN_FILE = Path("youtube_token.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="upload-music-video",
        description=(
            "Build a ~1h15m–1h30m music-mix video from music/ + a random post-processed/ still, "
            "generate a YouTube title/description with Gemini, and upload it. Run from the project root."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--title-font-size",
        type=int,
        default=DEFAULT_TITLE_FONT_SIZE,
        help=f"Bottom-left song-title font size in px (default: {DEFAULT_TITLE_FONT_SIZE}).",
    )
    p.add_argument(
        "--thumbnail-text",
        default=None,
        metavar="TEXT",
        help="Render a thumbnail with this text behind the subject, and set it as the video thumbnail.",
    )
    p.add_argument(
        "--thumbnail-bottom-text",
        default=None,
        metavar="TEXT",
        help=(
            "Caption drawn on TOP of the thumbnail, centered along the bottom. Works with or "
            "without --thumbnail-text (use \\n for multiple lines)."
        ),
    )
    p.add_argument(
        "--privacy",
        choices=VALID_PRIVACY,
        default=DEFAULT_PRIVACY,
        help=f"Upload privacy status (default: {DEFAULT_PRIVACY}).",
    )
    p.add_argument(
        "--category-id",
        default=DEFAULT_CATEGORY_ID,
        help=f"YouTube category id (default: {DEFAULT_CATEGORY_ID} = Music).",
    )
    p.add_argument(
        "--tags",
        default=None,
        help="Comma-separated video tags (e.g. 'lofi,chill,study').",
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
        help="Google OAuth client secret JSON (Desktop app). Default: client_secret*.json in the project root.",
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
        help=(
            "Local port for the OAuth redirect, i.e. http://localhost:<port>/ (default: 8080). "
            "For a Web application client this must match an Authorized redirect URI "
            "(register http://localhost:8080/ and http://127.0.0.1:8080/, with trailing slash)."
        ),
    )
    p.add_argument(
        "--metadata-prompt",
        type=Path,
        default=DEFAULT_PROMPT_FILE,
        help=f"Prompt file for title/description (default: {DEFAULT_PROMPT_FILE}).",
    )
    p.add_argument(
        "--metadata-provider",
        choices=("auto", "openai", "gemini"),
        default="openai",
        help="Model provider for the title/description (default: openai).",
    )
    p.add_argument(
        "--used-titles-file",
        type=Path,
        default=DEFAULT_USED_TITLES_FILE,
        help=(
            "File logging previously used titles so they're never reused. The new title is "
            f"appended after a successful upload (default: {DEFAULT_USED_TITLES_FILE})."
        ),
    )
    p.add_argument(
        "--no-upload",
        action="store_true",
        help="Build the video and print the generated metadata, but do not upload.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = Path.cwd().resolve()

    try:
        find_ffmpeg()
        find_ffprobe()
    except FFmpegNotFoundError as e:
        print(e, file=sys.stderr)
        return 2

    # Resolve the client secret up front (unless skipping upload) so we fail fast.
    client_secret: Path | None = None
    if not args.no_upload:
        try:
            client_secret = find_client_secret(args.client_secret, project_root)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    basename = datetime.now().strftime("mv_%Y%m%d_%H%M%S")
    font_key = resolve_font_key(project_root, None, weight=DEFAULT_TITLE_FONT_WEIGHT)

    cfg = AssemblerConfig(
        paths=AssemblerPaths(
            songs_dir=(project_root / DEFAULT_SONGS_DIR).resolve(),
            images_dir=(project_root / DEFAULT_IMAGES_DIR).resolve(),
            output_dir=(project_root / DEFAULT_OUTPUT_DIR).resolve(),
            project_root=project_root,
        ),
        duration=DurationBounds(min_sec=DEFAULT_MIN_SEC, max_sec=DEFAULT_MAX_SEC),
        text=TextOverlayStyle(
            font_key=font_key,
            font_size_px=args.title_font_size,
            font_weight=DEFAULT_TITLE_FONT_WEIGHT,
            fill_color=(255, 255, 255, 235),
        ),
        video_width=1920,
        video_height=1080,
        seed=None,
    )

    thumbnail_text = args.thumbnail_text.replace("\\n", "\n") if args.thumbnail_text else None
    thumbnail_bottom_text = (
        args.thumbnail_bottom_text.replace("\\n", "\n") if args.thumbnail_bottom_text else None
    )

    used_titles_path = args.used_titles_file.resolve()
    used_titles = load_used_titles(used_titles_path)

    # Metadata is generated INSIDE assemble (right after the tracklist), so a bad key /
    # API error aborts before the slow encode instead of after it.
    try:
        result = assemble(
            cfg,
            image_filename=None,
            output_basename=basename,
            progress=True,
            thumbnail_background_text=thumbnail_text,
            thumbnail_bottom_text=thumbnail_bottom_text,
            generate_metadata=True,
            metadata_provider=args.metadata_provider,
            metadata_prompt_path=args.metadata_prompt.resolve(),
            metadata_used_titles=used_titles,
            move_used_image=True,
        )
    except (RuntimeError, OSError, ValueError) as e:
        print(f"error: build failed (no video uploaded): {e}", file=sys.stderr)
        return 1

    video_mp4 = result["video_mp4"]
    thumbnail_png = result.get("thumbnail_png")
    meta = result["youtube_metadata"]

    print(f"Wrote folder: {result['output_dir']}")
    for k in ("frame_png", "audio_mp3", "video_mp4", "tracklist_txt"):
        print(f"  {k}: {result[k]}")
    if thumbnail_png:
        print(f"  thumbnail_png: {thumbnail_png}")
    if result.get("title_txt"):
        print(f"  title_txt: {result['title_txt']}")
        print(f"  description_txt: {result['description_txt']}")

    print("\n--- YouTube metadata ---")
    print(f"Title: {meta.title}")
    print("Description:")
    print(meta.description)
    print("------------------------\n")

    if args.no_upload:
        print("--no-upload set: skipping upload.")
        return 0

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None

    def on_progress(p: float) -> None:
        print(f"\r  uploading… {p * 100:5.1f}%", end="", flush=True, file=sys.stderr)

    print("Uploading to YouTube (a browser may open for authorization on first run)…")
    try:
        response = upload_video(
            Path(video_mp4),
            title=meta.title,
            description=meta.description,
            client_secret=client_secret,  # type: ignore[arg-type]
            token_path=args.token_file.resolve(),
            privacy=args.privacy,
            category_id=args.category_id,
            tags=tags,
            made_for_kids=args.made_for_kids,
            thumbnail_path=Path(thumbnail_png) if thumbnail_png else None,
            oauth_port=args.oauth_port,
            on_progress=on_progress,
        )
        print(file=sys.stderr)
    except Exception as e:
        print(f"\nerror: upload failed: {e}", file=sys.stderr)
        return 1

    record_used_title(meta.title, used_titles_path)

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
