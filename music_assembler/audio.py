"""Build a long MP3 by concatenating random tracks from a folder."""

from __future__ import annotations

import random
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from music_assembler.ffmpeg_util import run_ffmpeg, run_ffmpeg_with_progress, run_ffprobe

# Strip trailing ``-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`` from a filename stem so
# variants of the same song (different IDs) share one logical name.
_UUID_TAIL = re.compile(
    r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def logical_track_name(path: Path) -> str:
    """Same logical title for duplicate exports that only differ by a trailing UUID in the filename."""
    stem = path.stem
    m = _UUID_TAIL.search(stem)
    if m:
        return stem[: m.start()]
    return stem


def discover_mp3_files(songs_dir: Path) -> list[Path]:
    if not songs_dir.is_dir():
        raise FileNotFoundError(f"Songs directory does not exist: {songs_dir}")
    # Only audio files; .txt and other junk in the folder are ignored.
    files = sorted(songs_dir.glob("*.mp3")) + sorted(songs_dir.glob("*.MP3"))
    if not files:
        raise ValueError(f"No MP3 files found under {songs_dir}")
    return files


def probe_duration_seconds(path: Path) -> float:
    r = run_ffprobe(
        [
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"ffprobe failed for {path}: {r.stderr}")
    return float(r.stdout.strip())


def _escape_concat_path(path: Path) -> str:
    s = str(path.resolve())
    return s.replace("'", "'\\''")


def _write_concat_list(files: list[Path], list_path: Path) -> None:
    lines = [f"file '{_escape_concat_path(p)}'" for p in files]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concat_mp3_files(
    files: list[Path],
    output_mp3: Path,
    *,
    duration_sec: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    if not files:
        raise ValueError("No files to concatenate.")
    output_mp3.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        list_path = Path(tmp) / "concat.txt"
        _write_concat_list(files, list_path)
        args = [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(output_mp3),
        ]
        if on_progress is not None:
            run_ffmpeg_with_progress(args, duration_sec=duration_sec, on_progress=on_progress)
        else:
            r = run_ffmpeg(["-y", *args])
            if r.returncode != 0:
                raise RuntimeError(f"ffmpeg concat failed: {r.stderr}")


def trim_mp3(
    input_mp3: Path,
    output_mp3: Path,
    duration_sec: float,
    *,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    args = [
        "-i",
        str(input_mp3),
        "-t",
        str(duration_sec),
        "-c",
        "copy",
        str(output_mp3),
    ]
    if on_progress is not None:
        run_ffmpeg_with_progress(args, duration_sec=duration_sec, on_progress=on_progress)
    else:
        r = run_ffmpeg(["-y", *args])
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg trim failed: {r.stderr}")


def build_random_playlist(
    files: list[Path],
    min_sec: float,
    max_sec: float,
    rng: random.Random,
) -> tuple[list[Path], float]:
    """
    Pick tracks (with reuse if needed) until total duration is in [min_sec, max_sec].
    If a single track exceeds max_sec, the playlist is that one track (caller should trim).

    Two files with the same logical name (see :func:`logical_track_name`) are never placed
    back-to-back; they may both appear in the same mix. If the library has only one logical
    title, consecutive repeats are unavoidable.
    """
    if not files:
        raise ValueError("No MP3 files.")
    durations = {p: probe_duration_seconds(p) for p in files}
    distinct_keys = {logical_track_name(p) for p in files}

    playlist: list[Path] = []
    total = 0.0
    idx = 0
    max_iterations = max(len(files) * 500, 1000)

    while total < min_sec and idx < max_iterations:
        last_key = logical_track_name(playlist[-1]) if playlist else None
        if last_key is not None and len(distinct_keys) > 1:
            pool = [p for p in files if logical_track_name(p) != last_key]
        else:
            pool = list(files)
        if not pool:
            pool = list(files)

        path = rng.choice(pool)
        d = durations[path]
        if not playlist and d >= max_sec:
            return [path], d
        playlist.append(path)
        total += d
        idx += 1
        if total >= min_sec:
            break

    if not playlist:
        raise RuntimeError("Could not build playlist (empty result).")

    return playlist, total


def build_random_mix(
    songs_dir: Path,
    output_mp3: Path,
    min_sec: float,
    max_sec: float,
    seed: int | None = None,
    *,
    on_progress: Callable[[float, str], None] | None = None,
) -> tuple[list[Path], float]:
    """
    Concatenate random MP3s until duration >= min_sec, then trim to max_sec if needed.
    Returns (playlist used, final duration seconds).
    """
    def _emit(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(pct, msg)

    _emit(0.0, "Building playlist…")
    rng = random.Random(seed)
    files = discover_mp3_files(songs_dir)
    playlist, playlist_sum_sec = build_random_playlist(files, min_sec, max_sec, rng)
    _emit(5.0, "Concatenating audio…")

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "merged.mp3"

        def concat_local(p: float) -> None:
            _emit(5.0 + p * 0.40, "Concatenating audio…")

        concat_mp3_files(
            playlist,
            raw,
            duration_sec=playlist_sum_sec,
            on_progress=concat_local if on_progress else None,
        )
        dur = probe_duration_seconds(raw)
        if dur > max_sec:
            _emit(46.0, "Trimming audio…")

            def trim_local(p: float) -> None:
                _emit(46.0 + p * 0.04, "Trimming audio…")

            trim_mp3(raw, output_mp3, max_sec, on_progress=trim_local if on_progress else None)
            _emit(50.0, "Audio ready")
            return playlist, max_sec
        output_mp3.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(raw), str(output_mp3))
        _emit(50.0, "Audio ready")
        return playlist, dur
