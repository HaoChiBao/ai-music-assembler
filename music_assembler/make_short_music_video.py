"""CLI: random ~1h15m–1h30m MP3 mix + random post-processed still + caption → MP4 in music-video/."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

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
DEFAULT_FONT_SIZE = 80
DEFAULT_FONT_WEIGHT = 300
DEFAULT_MARGIN_PX = 40
DEFAULT_MARGIN_BOTTOM_PX = 70


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="make-short-music-video",
        description=(
            "Random MP3s from music/ until the mix is about 1h15m–1h30m long; same logical song title "
            "never plays back-to-back (see audio.logical_track_name). Caption on a random post-processed/ "
            "image → MP4 in music-video/. Run from the project root. Only .mp3 files are used; .txt etc. are ignored."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--text",
        "-t",
        required=True,
        help="Caption on the still (use \\n for line breaks).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = Path.cwd().resolve()

    try:
        find_ffmpeg()
        find_ffprobe()
    except FFmpegNotFoundError as e:
        print(e, file=sys.stderr)
        return 2

    text = args.text.replace("\\n", "\n")
    basename = datetime.now().strftime("mv_%Y%m%d_%H%M%S")
    font_key = resolve_font_key(project_root, None, weight=DEFAULT_FONT_WEIGHT)
    margin_bottom = DEFAULT_MARGIN_BOTTOM_PX  # v_align bottom

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
            font_size_px=DEFAULT_FONT_SIZE,
            font_weight=DEFAULT_FONT_WEIGHT,
            fill_color=(255, 255, 255, 255),
            stroke_color=(0, 0, 0, 255),
            stroke_width=0,
            embolden=0,
            horizontal="center",
            vertical="bottom",
            margin_px=DEFAULT_MARGIN_PX,
            margin_bottom_px=margin_bottom,
        ),
        video_width=1920,
        video_height=1080,
        seed=None,
    )

    result = assemble(
        cfg,
        overlay_text=text,
        image_filename=None,
        output_basename=basename,
        progress=True,
    )
    print("Wrote:")
    for k in ("audio_mp3", "frame_png", "video_mp4"):
        print(f"  {k}: {result[k]}")
    print(f"  tracks in mix: {len(result['playlist'])}")
    dur = result["final_audio_duration_sec"]
    print(f"  audio duration: {dur / 60:.1f} min ({dur:.0f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
