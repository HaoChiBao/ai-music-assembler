"""End-to-end: random mix MP3 → background still + per-song bottom-left titles → MP4.

Each run writes a self-contained folder ``output_dir/<base>/`` holding the still
(``frame.png``), the audio mix, the music video, the tracklist transcript, and —
when ``thumbnail_background_text`` is given — a thumbnail with that text drawn
*behind* the subject of the background image.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image

from music_assembler.audio import build_random_mix, build_track_segments
from music_assembler.bottom_text_overlay import resolve_font_key
from music_assembler.config import AssemblerConfig
from music_assembler.music_video import render_music_video, resolve_title_font, write_tracklist
from music_assembler.text_behind_subject import render_text_behind_subject


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
    image_filename: str | None = None,
    output_basename: str | None = None,
    progress: bool = False,
    thumbnail_background_text: str | None = None,
    thumbnail_font_weight: int = 700,
    thumbnail_segmenter: str = "rembg",
    # Highest-quality subject outline: BiRefNet (best hair/edges) + alpha matting, then
    # the add-text-behind-subject hair blend — erode the cutout halo (shrink) and soften
    # the edge with a Gaussian feather so hair blends into the text/background naturally.
    thumbnail_rembg_model: str = "birefnet-general",
    thumbnail_alpha_matting: bool = True,
    thumbnail_feather_px: float = 2.5,
    thumbnail_shrink_px: int = 1,
) -> dict[str, object]:
    """
    Build one music video into its own folder ``output_dir/<base>/``:

    1) Mix random MP3s into ``<base>_mix.mp3``.
    2) Pick a background image, save it as ``frame.png``, and write a YouTube tracklist.
    3) Encode ``<base>_video.mp4``: the still + the current song title (bottom-left).
    4) If ``thumbnail_background_text`` is given, render ``<base>_thumbnail.png``: the
       same still with that text drawn *behind* the segmented subject. The subject is
       outlined at the highest quality (BiRefNet ``birefnet-general`` + alpha matting),
       then the mask edge is eroded + feathered so hair blends in naturally.

    The song-title style (font, size, color) comes from ``cfg.text``. When ``progress``
    is True, print percentage lines to stderr (0–100%) for long runs.
    """
    out = cfg.paths.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    project_root = cfg.paths.project_root.resolve()

    base = output_basename or "session"
    run_dir = out / base
    run_dir.mkdir(parents=True, exist_ok=True)
    audio_mp3 = run_dir / f"{base}_mix.mp3"
    video_mp4 = run_dir / f"{base}_video.mp4"
    tracklist_txt = run_dir / f"{base}_tracklist.txt"
    frame_png = run_dir / "frame.png"
    thumbnail_png = run_dir / f"{base}_thumbnail.png"

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
    # Persist the chosen still as frame.png so the folder is self-contained; this is
    # also the exact image used by both the video and the thumbnail.
    Image.open(bg).convert("RGB").save(frame_png, format="PNG")

    if progress and emit:
        emit(52.0, "Writing tracklist…")
    segments = build_track_segments(playlist, final_dur)
    write_tracklist(tracklist_txt, segments)

    thumbnail_out: Path | None = None
    if thumbnail_background_text:
        if progress and emit:
            emit(53.0, "Rendering thumbnail…")
        thumb_font_key = resolve_font_key(project_root, None, weight=thumbnail_font_weight)
        try:
            render_text_behind_subject(
                frame_png,
                thumbnail_background_text,
                thumbnail_png,
                font_key=thumb_font_key,
                font_weight=thumbnail_font_weight,
                segmenter=thumbnail_segmenter,
                rembg_model=thumbnail_rembg_model,
                alpha_matting=thumbnail_alpha_matting,
                feather_px=thumbnail_feather_px,
                mask_shrink_px=thumbnail_shrink_px,
                project_root=project_root,
            )
            thumbnail_out = thumbnail_png
        except (RuntimeError, OSError) as e:
            # Don't fail the whole video if segmentation isn't available; warn and continue.
            print(f"\nwarning: thumbnail not created: {e}", file=sys.stderr)

    title_font = resolve_title_font(cfg.text.font_key, project_root, cfg.text.font_weight)

    if progress and emit:
        emit(55.0, "Encoding video…")

    def video_progress(p: float) -> None:
        if emit:
            emit(55.0 + p * 0.45, "Encoding video…")

    render_music_video(
        frame_png,
        audio_mp3,
        video_mp4,
        segments=segments,
        total_duration_sec=final_dur,
        video_width=cfg.video_width,
        video_height=cfg.video_height,
        title_font_path=title_font,
        title_font_size=cfg.text.font_size_px,
        title_color=cfg.text.fill_color,
        on_progress=video_progress if progress else None,
    )

    if progress and emit:
        emit(100.0, "Complete")
        print(file=sys.stderr)

    return {
        "output_dir": run_dir,
        "frame_png": frame_png,
        "audio_mp3": audio_mp3,
        "video_mp4": video_mp4,
        "tracklist_txt": tracklist_txt,
        "thumbnail_png": thumbnail_out,
        "background": bg,
        "playlist": playlist,
        "segments": segments,
        "final_audio_duration_sec": final_dur,
    }
