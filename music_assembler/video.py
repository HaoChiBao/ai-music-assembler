"""Mux a still image and MP3 into an MP4 with FFmpeg."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from music_assembler.audio import probe_duration_seconds
from music_assembler.ffmpeg_util import run_ffmpeg, run_ffmpeg_with_progress


def still_image_to_video(
    image_path: Path,
    audio_mp3: Path,
    output_mp4: Path,
    *,
    video_width: int,
    video_height: int,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale={video_width}:{video_height}:force_original_aspect_ratio=decrease,pad={video_width}:{video_height}:(ow-iw)/2:(oh-ih)/2"
    audio_dur = probe_duration_seconds(audio_mp3)
    args = [
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-i",
        str(audio_mp3),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-tune",
        "stillimage",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-pix_fmt",
        "yuv420p",
        str(output_mp4),
    ]
    if on_progress is not None:
        run_ffmpeg_with_progress(args, duration_sec=audio_dur, on_progress=on_progress)
    else:
        r = run_ffmpeg(["-y", *args])
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg video encode failed: {r.stderr}")
