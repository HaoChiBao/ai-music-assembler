"""End-to-end: random mix MP3 → titled still → MP4."""

from __future__ import annotations

import random
import sys
from pathlib import Path

from music_assembler.audio import build_random_mix
from music_assembler.config import AssemblerConfig
from music_assembler.image_text import render_image_with_text
from music_assembler.video import still_image_to_video


def pick_background_image(images_dir: Path, name: str | None, seed: int | None) -> Path:
    if not images_dir.is_dir():
        raise FileNotFoundError(f"Images directory does not exist: {images_dir}")
    exts = (".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP")
    files = [p for p in images_dir.iterdir() if p.suffix in exts and p.is_file()]
    if not files:
        raise ValueError(f"No image files found under {images_dir}")
    if name:
        chosen = images_dir / name
        if not chosen.is_file():
            raise FileNotFoundError(f"Image not found: {chosen}")
        return chosen
    rng = random.Random(seed)
    return rng.choice(files)


def assemble(
    cfg: AssemblerConfig,
    *,
    overlay_text: str,
    image_filename: str | None = None,
    output_basename: str | None = None,
    progress: bool = False,
) -> dict[str, Path]:
    """
    1) Mix random MP3s into output/mix.mp3 (or basename).
    2) Render text onto chosen background → output/frame.png.
    3) Mux → output/video.mp4.

    When ``progress`` is True, print percentage lines to stderr (0–100%) for long runs.
    """
    out = cfg.paths.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    project_root = cfg.paths.project_root.resolve()

    base = output_basename or "session"
    audio_mp3 = out / f"{base}_mix.mp3"
    frame_png = out / f"{base}_frame.png"
    video_mp4 = out / f"{base}_video.mp4"

    emit = None
    if progress:

        def _emit(pct: float, msg: str) -> None:
            print(f"\r  [{pct:5.1f}%] {msg}", end="", flush=True, file=sys.stderr)

        emit = _emit

    playlist, final_dur = build_random_mix(
        cfg.paths.songs_dir,
        audio_mp3,
        cfg.duration.min_sec,
        cfg.duration.max_sec,
        seed=cfg.seed,
        on_progress=emit,
    )

    if progress and emit:
        emit(51.0, "Choosing background…")
    bg = pick_background_image(cfg.paths.images_dir, image_filename, cfg.seed)

    if progress and emit:
        emit(53.0, "Rendering caption…")
    render_image_with_text(bg, overlay_text, frame_png, cfg.text, project_root=project_root)

    if progress and emit:
        emit(55.0, "Encoding video…")

    def video_progress(p: float) -> None:
        if emit:
            emit(55.0 + p * 0.45, "Encoding video…")

    still_image_to_video(
        frame_png,
        audio_mp3,
        video_mp4,
        video_width=cfg.video_width,
        video_height=cfg.video_height,
        on_progress=video_progress if progress else None,
    )

    if progress and emit:
        emit(100.0, "Complete")
        print(file=sys.stderr)

    return {
        "audio_mp3": audio_mp3,
        "frame_png": frame_png,
        "video_mp4": video_mp4,
        "playlist": playlist,
        "final_audio_duration_sec": final_dur,
    }
