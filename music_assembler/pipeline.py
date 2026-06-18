"""End-to-end: random mix MP3 → background still + per-song bottom-left titles → MP4.

Each run writes a self-contained folder ``output_dir/<base>/`` holding the still
(``frame.png``), the audio mix, the music video, the tracklist transcript, and —
when ``thumbnail_background_text`` is given — a thumbnail with that text drawn
*behind* the subject of the background image.
"""

from __future__ import annotations

import random
import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from music_assembler.audio import build_random_mix, build_track_segments
from music_assembler.bottom_text_overlay import render_text_overlay, resolve_font_key
from music_assembler.config import AssemblerConfig
from music_assembler.music_video import render_music_video, resolve_title_font, write_tracklist
from music_assembler.text_behind_subject import render_text_behind_subject
from music_assembler.youtube_metadata import (
    DEFAULT_PROMPT_FILE,
    YouTubeMetadata,
    _normalize_title,
    generate_youtube_metadata,
)


def _generate_unique_metadata(
    segments,
    total_duration_sec: float,
    *,
    prompt_path: Path,
    provider: str | None,
    used_titles: list[str] | None,
    lock: threading.Lock | None,
    reserve_attempts: int = 3,
) -> YouTubeMetadata:
    """Generate metadata, then reserve a unique title under ``lock``.

    The (slow) model call runs WITHOUT holding ``lock`` so parallel batch runs overlap;
    only the quick title-uniqueness check/append is serialized. On the rare collision the
    title is regenerated. With no lock (single video) this is just one generation.
    """
    meta = None
    for _ in range(max(1, reserve_attempts)):
        snapshot = list(used_titles) if used_titles is not None else []
        meta = generate_youtube_metadata(
            segments,
            total_duration_sec,
            prompt_path=prompt_path,
            provider=provider,
            used_titles=snapshot,
        )
        if used_titles is None or lock is None:
            return meta
        with lock:
            existing = {_normalize_title(t) for t in used_titles}
            if _normalize_title(meta.title) not in existing:
                used_titles.append(meta.title)
                return meta
    # Couldn't get a unique title after retries — reserve what we have and move on.
    if used_titles is not None and lock is not None and meta is not None:
        with lock:
            used_titles.append(meta.title)
    assert meta is not None
    return meta


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
    # Optional bottom-middle caption drawn ON TOP of the finished thumbnail (over the
    # subject and everything else), styled like the video's bottom-left title.
    thumbnail_bottom_text: str | None = None,
    thumbnail_bottom_font_weight: int = 700,
    thumbnail_bottom_font_size: int | None = None,
    # YouTube metadata — generated EARLY (right after the tracklist, before the slow
    # thumbnail/encode steps) so an API/key error fails fast instead of after the encode.
    generate_metadata: bool = False,
    metadata_provider: str | None = None,
    metadata_prompt_path: Path = DEFAULT_PROMPT_FILE,
    # Shared mutable list of titles to avoid (the new title is appended); pass
    # ``metadata_lock`` so parallel runs reserve unique titles atomically.
    metadata_used_titles: list[str] | None = None,
    metadata_lock: threading.Lock | None = None,
    move_used_image: bool = False,
    on_progress: Callable[[float, str], None] | None = None,
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
    5) If ``move_used_image`` is True, move the chosen background into
       ``<images_dir>/used/`` so it won't be reused on later runs.

    The song-title style (font, size, color) comes from ``cfg.text``. Progress reporting:
    pass ``on_progress(pct, msg)`` to receive 0–100% updates (used by the parallel batch
    runner for per-video bars); otherwise set ``progress=True`` to print a ``\\r`` line to
    stderr. ``on_progress`` takes precedence over ``progress``.
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
    title_txt = run_dir / f"{base}_title.txt"
    description_txt = run_dir / f"{base}_description.txt"

    # Custom callback wins; else fall back to a simple \r stderr printer when progress=True.
    emit = on_progress
    owns_stderr_line = False
    if emit is None and progress:
        owns_stderr_line = True

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

    if emit:
        emit(51.0, "Choosing background…")
    bg = pick_background_image(cfg.paths.images_dir, image_filename, cfg.seed)
    # Persist the chosen still as frame.png so the folder is self-contained; this is
    # also the exact image used by both the video and the thumbnail.
    Image.open(bg).convert("RGB").save(frame_png, format="PNG")

    if emit:
        emit(52.0, "Writing tracklist…")
    segments = build_track_segments(playlist, final_dur)
    write_tracklist(tracklist_txt, segments)

    # Generate the YouTube title/description NOW (before the expensive thumbnail + encode),
    # so a metadata/API failure aborts early instead of after wasting time on the video.
    youtube_meta: YouTubeMetadata | None = None
    if generate_metadata:
        if emit:
            emit(53.0, "Generating title/description…")
        youtube_meta = _generate_unique_metadata(
            segments,
            final_dur,
            prompt_path=metadata_prompt_path,
            provider=metadata_provider,
            used_titles=metadata_used_titles,
            lock=metadata_lock,
        )
        title_txt.write_text(youtube_meta.title + "\n", encoding="utf-8")
        description_txt.write_text(youtube_meta.description + "\n", encoding="utf-8")

    # The thumbnail is built up in layers, any of which is optional:
    #   1) text drawn *behind* the subject (thumbnail_background_text)
    #   2) a caption drawn on *top*, bottom-middle (thumbnail_bottom_text)
    # The thumbnail is produced if either layer is requested.
    thumbnail_out: Path | None = None
    if thumbnail_background_text or thumbnail_bottom_text:
        if emit:
            emit(54.0, "Rendering thumbnail…")
        base_ready = False
        if thumbnail_background_text:
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
                base_ready = True
            except (RuntimeError, OSError) as e:
                # Don't fail the whole video if segmentation isn't available; warn and
                # fall back to the plain still so any bottom caption still renders.
                print(f"\nwarning: thumbnail background text not applied: {e}", file=sys.stderr)

        if not base_ready:
            # Start the thumbnail from the plain still (no behind-subject text, or it failed).
            Image.open(frame_png).convert("RGB").save(thumbnail_png, format="PNG")
        thumbnail_out = thumbnail_png

        if thumbnail_bottom_text:
            try:
                with Image.open(thumbnail_png) as _thumb:
                    th = _thumb.height
                bottom_size = thumbnail_bottom_font_size or max(40, int(th * 0.06))
                margin = max(24, int(th * 0.05))
                overlay_font_key = resolve_font_key(
                    project_root, None, weight=thumbnail_bottom_font_weight
                )
                render_text_overlay(
                    thumbnail_png,
                    thumbnail_bottom_text,
                    thumbnail_png,
                    font_key=overlay_font_key,
                    font_size_px=bottom_size,
                    margin_px=margin,
                    margin_bottom_px=margin,
                    horizontal="center",
                    vertical="bottom",
                    stroke_width=0,
                    embolden=0,
                    font_weight=thumbnail_bottom_font_weight,
                    project_root=project_root,
                )
            except OSError as e:
                print(f"\nwarning: thumbnail bottom text not applied: {e}", file=sys.stderr)

    title_font = resolve_title_font(cfg.text.font_key, project_root, cfg.text.font_weight)

    if emit:
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
        on_progress=video_progress if emit else None,
    )

    # Video (and thumbnail) are done: retire the source background into <images_dir>/used/
    # so it isn't picked again on future runs. frame.png in the run folder is a copy, so
    # moving the original doesn't affect the outputs.
    moved_to: Path | None = None
    if move_used_image:
        used_dir = cfg.paths.images_dir.resolve() / "used"
        used_dir.mkdir(parents=True, exist_ok=True)
        dest = used_dir / bg.name
        try:
            if dest.exists():
                dest.unlink()
            moved_to = Path(shutil.move(str(bg), str(dest)))
        except OSError as e:
            print(f"\nwarning: could not move used image {bg.name} to {used_dir}: {e}", file=sys.stderr)

    if emit:
        emit(100.0, "Complete")
    if owns_stderr_line:
        print(file=sys.stderr)

    return {
        "output_dir": run_dir,
        "frame_png": frame_png,
        "audio_mp3": audio_mp3,
        "video_mp4": video_mp4,
        "tracklist_txt": tracklist_txt,
        "thumbnail_png": thumbnail_out,
        "youtube_metadata": youtube_meta,
        "title_txt": title_txt if youtube_meta else None,
        "description_txt": description_txt if youtube_meta else None,
        "background": bg,
        "used_image": moved_to,
        "playlist": playlist,
        "segments": segments,
        "final_audio_duration_sec": final_dur,
    }
