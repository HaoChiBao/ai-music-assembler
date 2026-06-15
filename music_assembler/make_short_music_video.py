"""CLI: random ~1h15m–1h30m MP3 mix + random post-processed still + bottom-left song
titles → MP4 in music-video/ (plus a tracklist .txt)."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from music_assembler import __version__
from music_assembler.bottom_text_overlay import resolve_font_key
from music_assembler.config import AssemblerConfig, AssemblerPaths, DurationBounds, TextOverlayStyle
from music_assembler.ffmpeg_util import FFmpegNotFoundError, find_ffmpeg, find_ffprobe
from music_assembler.pipeline import assemble

# Target total mix length: 1 hour 15 min .. 1 hour 30 min (not mm:ss).
DEFAULT_MIN_SEC = 75 * 60.0  # 1h15m = 4500 s
DEFAULT_MAX_SEC = 90 * 60.0  # 1h30m = 5400 s
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

    result = assemble(
        cfg,
        image_filename=None,
        output_basename=basename,
        progress=True,
        thumbnail_background_text=thumbnail_text,
    )
    print(f"Wrote folder: {result['output_dir']}")
    for k in ("frame_png", "audio_mp3", "video_mp4", "tracklist_txt"):
        print(f"  {k}: {result[k]}")
    if result.get("thumbnail_png"):
        print(f"  thumbnail_png: {result['thumbnail_png']}")
    print(f"  background: {result['background']}")
    print(f"  tracks in mix: {len(result['playlist'])}")
    dur = result["final_audio_duration_sec"]
    print(f"  audio duration: {dur / 60:.1f} min ({dur:.0f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
