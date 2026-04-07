"""CLI: assemble random MP3 mix + titled still image → MP4."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from music_assembler import __version__
from music_assembler.config import (
    AssemblerConfig,
    AssemblerPaths,
    DurationBounds,
    TextOverlayStyle,
    list_font_keys,
)
from music_assembler.ffmpeg_util import FFmpegNotFoundError, find_ffmpeg, find_ffprobe
from music_assembler.pipeline import assemble


def _parse_rgba(s: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) == 3:
        return (int(parts[0]), int(parts[1]), int(parts[2]), 255)
    if len(parts) == 4:
        return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
    raise argparse.ArgumentTypeError("Expected R,G,B or R,G,B,A")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="assemble-music-video",
        description="Build a long random MP3 mix and mux it with a static titled image as MP4.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--songs-dir", type=Path, default=None, help="Folder containing MP3 files.")
    p.add_argument(
        "--images-dir",
        type=Path,
        default=Path("post-processed"),
        help="Folder for 16:9 background images (default: post-processed).",
    )
    p.add_argument(
        "--image",
        default=None,
        help="Background filename inside images-dir (default: random pick).",
    )
    p.add_argument("--text", default=None, help="Overlay text (use \\n for line breaks). Required for assembly.")
    p.add_argument("--output-dir", type=Path, default=Path("output"), help="Where to write mix, frame, and video.")
    p.add_argument("--basename", default="session", help="Prefix for output files.")
    p.add_argument("--min-sec", type=float, default=75 * 60, help="Minimum mix length in seconds (default: 4500 = 75 min).")
    p.add_argument("--max-sec", type=float, default=105 * 60, help="Maximum mix length in seconds (default: 6300 = 105 min).")
    p.add_argument("--seed", type=int, default=None, help="Random seed for song order and random image pick.")
    p.add_argument("--font", default="arial", help="Font key: arial, helvetica, georgia, times, sf_pro, or a file stem in fonts/.")
    p.add_argument("--font-size", type=int, default=72)
    p.add_argument(
        "--font-weight",
        type=int,
        default=None,
        metavar="N",
        help="Optional: pick a file in fonts/ by weight (300 = Light, 400 = Regular, 700 = Bold).",
    )
    p.add_argument("--fill", type=_parse_rgba, default=(255, 255, 255, 255))
    p.add_argument("--stroke", type=_parse_rgba, default=(0, 0, 0, 255))
    p.add_argument(
        "--stroke-width",
        type=int,
        default=2,
        help="Outline thickness (8-direction rings; lower = thinner). Default: 2.",
    )
    p.add_argument(
        "--embolden",
        type=int,
        default=0,
        help="Simulated font weight on the title (0 = lightest). Default: 0.",
    )
    p.add_argument("--h-align", choices=("left", "center", "right"), default="center")
    p.add_argument("--v-align", choices=("top", "center", "bottom"), default="center")
    p.add_argument("--video-width", type=int, default=1920)
    p.add_argument("--video-height", type=int, default=1080)
    p.add_argument(
        "--list-fonts",
        action="store_true",
        help="Print available font keys and exit (no FFmpeg required).",
    )
    p.add_argument(
        "--progress",
        action="store_true",
        help="Print percent complete on stderr during audio concat, trim, and video encode.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = Path.cwd()

    if args.list_fonts:
        for k in list_font_keys(project_root):
            print(k)
        return 0

    if args.songs_dir is None or args.text is None:
        parser.error("--songs-dir and --text are required unless you pass --list-fonts.")

    try:
        find_ffmpeg()
        find_ffprobe()
    except FFmpegNotFoundError as e:
        print(e, file=sys.stderr)
        return 2

    text = args.text.replace("\\n", "\n")
    cfg = AssemblerConfig(
        paths=AssemblerPaths(
            songs_dir=args.songs_dir,
            images_dir=args.images_dir,
            output_dir=args.output_dir,
            project_root=project_root,
        ),
        duration=DurationBounds(min_sec=args.min_sec, max_sec=args.max_sec),
        text=TextOverlayStyle(
            font_key=args.font,
            font_size_px=args.font_size,
            font_weight=args.font_weight,
            fill_color=args.fill,
            stroke_color=args.stroke,
            stroke_width=args.stroke_width,
            embolden=args.embolden,
            horizontal=args.h_align,
            vertical=args.v_align,
        ),
        video_width=args.video_width,
        video_height=args.video_height,
        seed=args.seed,
    )

    result = assemble(
        cfg,
        overlay_text=text,
        image_filename=args.image,
        output_basename=args.basename,
        progress=args.progress,
    )
    print("Wrote:")
    for k in ("audio_mp3", "frame_png", "video_mp4"):
        print(f"  {k}: {result[k]}")
    print(f"  tracks in mix: {len(result['playlist'])}")
    print(f"  audio duration (s): {result['final_audio_duration_sec']:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
