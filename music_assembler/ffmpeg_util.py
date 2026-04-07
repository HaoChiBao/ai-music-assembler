"""FFmpeg / FFprobe discovery and subprocess helpers."""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path


class FFmpegNotFoundError(RuntimeError):
    pass


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FFmpegNotFoundError(
            "ffmpeg is not on PATH. Install FFmpeg (e.g. brew install ffmpeg on macOS)."
        )
    return path


def find_ffprobe() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise FFmpegNotFoundError(
            "ffprobe is not on PATH. Install FFmpeg (ffprobe ships with ffmpeg)."
        )
    return path


def run_ffmpeg(args: Sequence[str | Path], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [find_ffmpeg(), *[str(a) for a in args]]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=True,
    )


def run_ffmpeg_with_progress(
    args: Sequence[str | Path],
    *,
    duration_sec: float | None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """
    Run ffmpeg with ``-progress pipe:1`` and map ``out_time_ms`` (microseconds) to 0–100%
    when ``duration_sec`` is set. Emits at most ~every 0.5% to limit noise.
    """
    cmd = [
        find_ffmpeg(),
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-loglevel",
        "error",
        *[str(a) for a in args],
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    stderr_chunks: list[str] = []

    def drain_stderr() -> None:
        if proc.stderr:
            stderr_chunks.append(proc.stderr.read())

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()
    last_emit = -1.0
    try:
        for line in proc.stdout:
            line = line.strip()
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k == "out_time_ms" and duration_sec is not None and duration_sec > 0 and on_progress:
                try:
                    us = int(v)
                    sec = us / 1_000_000.0
                    pct = min(100.0, (sec / duration_sec) * 100.0)
                except ValueError:
                    continue
                if pct - last_emit >= 0.5 or pct >= 99.5:
                    last_emit = pct
                    on_progress(pct)
            elif k == "progress" and v == "end" and on_progress:
                on_progress(100.0)
    finally:
        proc.wait()
        t.join(timeout=30.0)
    err_text = "".join(stderr_chunks)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {err_text}")


def run_ffprobe(args: Sequence[str | Path], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [find_ffprobe(), *[str(a) for a in args]]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=True,
    )
