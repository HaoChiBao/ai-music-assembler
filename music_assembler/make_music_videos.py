"""CLI: build N music videos in parallel, each with its own progress bar.

Each video gets a distinct random background (so backgrounds aren't reused within the
batch), is rendered into its own ``music-video/<base>/`` folder, gets a Gemini-generated
title/description saved alongside it, and is recorded as ``pending`` in a registry txt
file. Upload them later with ``schedule-music-videos``.

Run from the project root. Metadata needs ``GEMINI_API_KEY`` (.env); pass ``--no-metadata``
to skip title/description generation.
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from music_assembler import __version__
from music_assembler.bottom_text_overlay import resolve_font_key
from music_assembler.config import AssemblerConfig, AssemblerPaths, DurationBounds, TextOverlayStyle
from music_assembler.ffmpeg_util import FFmpegNotFoundError, find_ffmpeg, find_ffprobe
from music_assembler.pipeline import assemble
from music_assembler.progress_bars import MultiProgress
from music_assembler.video_registry import (
    DEFAULT_REGISTRY_FILE,
    VideoEntry,
    VideoRegistry,
)
from music_assembler.youtube_metadata import (
    DEFAULT_PROMPT_FILE,
    DEFAULT_USED_TITLES_FILE,
    load_used_titles,
)

DEFAULT_MIN_SEC = 75 * 60.0
DEFAULT_MAX_SEC = 90 * 60.0
DEFAULT_SONGS_DIR = Path("music")
DEFAULT_OUTPUT_DIR = Path("music-video")
DEFAULT_IMAGES_DIR = Path("post-processed")
DEFAULT_TITLE_FONT_SIZE = 46
DEFAULT_TITLE_FONT_WEIGHT = 400
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate-music-videos",
        description=(
            "Build N music-mix videos in parallel (each with a live progress bar), generate "
            "YouTube metadata for each, and record them as pending in a registry for later "
            "scheduled upload. Run from the project root."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "-n",
        "--count",
        type=int,
        required=True,
        help="How many videos to generate.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel workers (default: min(count, 3)). Higher uses more CPU/RAM.",
    )
    p.add_argument(
        "--thumbnail-text",
        default=None,
        metavar="TEXT",
        help="Render a thumbnail for each video with this text behind the subject (use \\n for lines).",
    )
    p.add_argument(
        "--thumbnail-bottom-text",
        default=None,
        metavar="TEXT",
        help=(
            "Caption drawn on TOP of each thumbnail, centered along the bottom. Works with or "
            "without --thumbnail-text (use \\n for lines)."
        ),
    )
    p.add_argument(
        "--title-font-size",
        type=int,
        default=DEFAULT_TITLE_FONT_SIZE,
        help=f"Bottom-left song-title font size in px (default: {DEFAULT_TITLE_FONT_SIZE}).",
    )
    p.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_FILE,
        help=f"Registry txt file to append pending videos to (default: {DEFAULT_REGISTRY_FILE}).",
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
        help=f"Log of previously used titles to avoid reusing (default: {DEFAULT_USED_TITLES_FILE}).",
    )
    p.add_argument(
        "--no-metadata",
        action="store_true",
        help="Skip Gemini title/description generation (registry titles will be blank).",
    )
    return p


def _list_backgrounds(images_dir: Path) -> list[Path]:
    if not images_dir.is_dir():
        raise FileNotFoundError(f"Images directory does not exist: {images_dir}")
    return sorted(p for p in images_dir.iterdir() if p.suffix in IMAGE_EXTS and p.is_file())


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = Path.cwd().resolve()

    if args.count < 1:
        print("error: --count must be >= 1", file=sys.stderr)
        return 2

    try:
        find_ffmpeg()
        find_ffprobe()
    except FFmpegNotFoundError as e:
        print(e, file=sys.stderr)
        return 2

    images_dir = (project_root / DEFAULT_IMAGES_DIR).resolve()
    try:
        backgrounds = _list_backgrounds(images_dir)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not backgrounds:
        print(f"error: no background images found in {images_dir}", file=sys.stderr)
        return 2

    count = args.count
    if count > len(backgrounds):
        print(
            f"warning: only {len(backgrounds)} backgrounds available; reducing count "
            f"from {count} to {len(backgrounds)} so each video uses a distinct image.",
            file=sys.stderr,
        )
        count = len(backgrounds)

    chosen_bgs = random.sample(backgrounds, count)
    workers = args.workers or min(count, 3)
    workers = max(1, min(workers, count))

    font_key = resolve_font_key(project_root, None, weight=DEFAULT_TITLE_FONT_WEIGHT)
    thumbnail_text = args.thumbnail_text.replace("\\n", "\n") if args.thumbnail_text else None
    thumbnail_bottom_text = (
        args.thumbnail_bottom_text.replace("\\n", "\n") if args.thumbnail_bottom_text else None
    )
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # One job per video: a unique basename + a pre-assigned distinct background.
    jobs: list[tuple[int, str, Path]] = []
    for i in range(count):
        basename = f"mv_{run_stamp}_{i:02d}"
        jobs.append((i, basename, chosen_bgs[i]))

    def make_cfg() -> AssemblerConfig:
        return AssemblerConfig(
            paths=AssemblerPaths(
                songs_dir=(project_root / DEFAULT_SONGS_DIR).resolve(),
                images_dir=images_dir,
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

    labels = [f"{base}" for _, base, _ in jobs]
    print(f"Building {count} videos with {workers} parallel workers…", file=sys.stderr)

    results: dict[int, dict] = {}
    errors: dict[int, str] = {}

    # Metadata is generated inside each assemble() (right after the tracklist, before the
    # encode) so failures abort early. A shared lock + shared list let parallel workers
    # reserve unique titles atomically, so no two videos in the batch share a title.
    want_metadata = not args.no_metadata
    used_titles = load_used_titles(args.used_titles_file.resolve())
    meta_lock = threading.Lock()
    metadata_prompt = args.metadata_prompt.resolve()

    with MultiProgress(labels) as bars:

        def run_one(job: tuple[int, str, Path]) -> tuple[int, dict | None, str | None]:
            idx, base, bg = job

            def on_progress(pct: float, msg: str) -> None:
                bars.update(idx, pct, msg)

            try:
                res = assemble(
                    make_cfg(),
                    image_filename=bg.name,
                    output_basename=base,
                    thumbnail_background_text=thumbnail_text,
                    thumbnail_bottom_text=thumbnail_bottom_text,
                    generate_metadata=want_metadata,
                    metadata_provider=args.metadata_provider,
                    metadata_prompt_path=metadata_prompt,
                    metadata_used_titles=used_titles,
                    metadata_lock=meta_lock,
                    move_used_image=True,
                    on_progress=on_progress,
                )
                return idx, res, None
            except Exception as e:  # isolate per-video failures
                bars.update(idx, bars.pcts[idx], f"FAILED: {e}")
                return idx, None, str(e)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(run_one, job) for job in jobs]
            for fut in as_completed(futures):
                idx, res, err = fut.result()
                if res is not None:
                    results[idx] = res
                else:
                    errors[idx] = err or "unknown error"

    # Record each built video in the registry (metadata was already generated + written
    # inside assemble()).
    registry = VideoRegistry(args.registry.resolve())
    made = 0
    for idx, base, _bg in jobs:
        res = results.get(idx)
        if res is None:
            continue
        run_dir = Path(res["output_dir"])
        video_mp4 = Path(res["video_mp4"])
        thumb = res.get("thumbnail_png")
        meta = res.get("youtube_metadata")
        description_txt = res.get("description_txt")

        registry.append(
            VideoEntry(
                id=base,
                dir=str(run_dir),
                video=str(video_mp4),
                thumbnail=str(thumb) if thumb else "",
                title=meta.title if meta else "",
                description=str(description_txt) if description_txt else "",
            )
        )
        made += 1

    print(file=sys.stderr)
    print(f"Done: {made}/{count} videos built and recorded in {registry.path}")
    if errors:
        print(f"{len(errors)} failed:", file=sys.stderr)
        for idx in sorted(errors):
            print(f"  {jobs[idx][1]}: {errors[idx]}", file=sys.stderr)
    print("Next: schedule + upload them with `schedule-music-videos`.")
    return 0 if made > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
