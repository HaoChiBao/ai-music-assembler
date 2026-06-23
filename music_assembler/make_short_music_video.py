"""CLI: random ~1h15m–1h30m MP3 mix + random post-processed still + bottom-left song
titles → MP4 in music-video/ (plus a tracklist .txt)."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from music_assembler import __version__
from music_assembler.assemble_options import add_duration_arguments, resolve_duration_bounds
from music_assembler.bottom_text_overlay import resolve_font_key
from music_assembler.config import AssemblerConfig, AssemblerPaths, TextOverlayStyle
from music_assembler.ffmpeg_util import FFmpegNotFoundError, find_ffmpeg, find_ffprobe
from music_assembler.pipeline import assemble

# Target total mix length: 1 hour 15 min .. 1 hour 30 min (not mm:ss).
DEFAULT_SONGS_DIR = Path("music")
DEFAULT_OUTPUT_DIR = Path("music-video")
DEFAULT_IMAGES_DIR = Path("post-processed")
DEFAULT_TITLE_FONT_SIZE = 46
DEFAULT_TITLE_FONT_WEIGHT = 400


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="make-short-music-video",
        description=(
            "Random MP3s from music/ until the mix is about 1h15m–1h30m long; same logical song title "
            "never plays back-to-back. Renders a random post-processed/ image as the background with the "
            "current song title in the bottom-left corner → MP4 in music-video/, plus a YouTube tracklist "
            ".txt. Run from the project root."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--category",
        default=None,
        metavar="NAME",
        help=(
            "Genre/category subfolder under music/, post-processed/, and music-video/ "
            "(e.g. korean). Default: use those dirs at repo root."
        ),
    )
    p.add_argument(
        "--folder",
        default=None,
        metavar="NAME",
        help="Shorthand for --category (same subfolder for music and backgrounds).",
    )
    p.add_argument(
        "--music-folder",
        default=None,
        metavar="NAME",
        help="Subfolder under music/ only (default: --category or --folder).",
    )
    p.add_argument(
        "--images-folder",
        default=None,
        metavar="NAME",
        help="Subfolder under post-processed/ only (default: --category or --folder).",
    )
    add_duration_arguments(p)
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
        help=(
            "If given, also render a thumbnail: this text drawn in large letters *behind* "
            "the subject of the background image (use \\n for multiple lines)."
        ),
    )
    p.add_argument(
        "--thumbnail-bottom-text",
        default=None,
        metavar="TEXT",
        help=(
            "Caption drawn on TOP of the thumbnail, centered along the bottom (over the "
            "subject). Works with or without --thumbnail-text (use \\n for multiple lines)."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = Path.cwd().resolve()

    try:
        find_ffmpeg()
        find_ffprobe()
    except FFmpegNotFoundError as e:
        print(e, file=sys.stderr)
        return 2

    basename = datetime.now().strftime("mv_%Y%m%d_%H%M%S")
    font_key = resolve_font_key(project_root, None, weight=DEFAULT_TITLE_FONT_WEIGHT)

    category = (args.category or args.folder or "").strip("/") or None
    music_sub = (args.music_folder or category or "").strip("/") or None
    images_sub = (args.images_folder or category or "").strip("/") or None

    songs_dir = project_root / DEFAULT_SONGS_DIR
    images_dir = project_root / DEFAULT_IMAGES_DIR
    output_dir = project_root / DEFAULT_OUTPUT_DIR
    if music_sub:
        songs_dir = songs_dir / music_sub
    if images_sub:
        images_dir = images_dir / images_sub
    if category:
        output_dir = output_dir / category

    duration = resolve_duration_bounds(
        duration_sec=args.duration,
        variance_sec=args.variance,
        min_sec=args.min_sec,
        max_sec=args.max_sec,
    )

    cfg = AssemblerConfig(
        paths=AssemblerPaths(
            songs_dir=songs_dir.resolve(),
            images_dir=images_dir.resolve(),
            output_dir=output_dir.resolve(),
            project_root=project_root,
        ),
        duration=duration,
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

    result = assemble(
        cfg,
        image_filename=None,
        output_basename=basename,
        progress=True,
        thumbnail_background_text=thumbnail_text,
        thumbnail_bottom_text=thumbnail_bottom_text,
        move_used_image=True,
    )
    print(f"Wrote folder: {result['output_dir']}")
    for k in ("frame_png", "audio_mp3", "video_mp4", "tracklist_txt"):
        print(f"  {k}: {result[k]}")
    if result.get("thumbnail_png"):
        print(f"  thumbnail_png: {result['thumbnail_png']}")
    print(f"  background: {result['background']}")
    if result.get("used_image"):
        print(f"  moved used image -> {result['used_image']}")
    print(f"  tracks in mix: {len(result['playlist'])}")
    dur = result["final_audio_duration_sec"]
    print(f"  audio duration: {dur / 60:.1f} min ({dur:.0f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
