"""Shared CLI helpers for assembly commands (duration parsing, R2 folder paths)."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass

from music_assembler.config import DurationBounds

# Default mix: 1h15m .. 1h30m (same as make-short-music-video).
DEFAULT_TARGET_SEC = 82.5 * 60.0  # midpoint
DEFAULT_VARIANCE_SEC = 7.5 * 60.0

_DURATION_TOKEN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)?",
    re.IGNORECASE,
)

_CHANNEL_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def normalize_channel(value: str | None) -> str | None:
    """Normalize a YouTube channel slug for R2 paths (``music-video/{channel}/``)."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    slug = raw.lower().replace(" ", "-")
    if not _CHANNEL_SLUG.match(slug):
        raise ValueError(
            "channel must be 1-64 chars: lowercase letters, digits, hyphens, underscores "
            f"(got {value!r})"
        )
    return slug


def resolve_channel_arg(value: str | None) -> str | None:
    """CLI/env channel: explicit value or ``ASSEMBLY_CHANNEL`` from the environment."""
    if value is not None and value.strip():
        return normalize_channel(value)
    env = os.environ.get("ASSEMBLY_CHANNEL", "").strip()
    return normalize_channel(env) if env else None


def video_output_prefix(channel: str) -> str:
    """R2 prefix for finished assembly runs: ``music-video/{channel}/``."""
    ch = normalize_channel(channel)
    if not ch:
        raise ValueError("channel is required for music-video output path")
    return f"music-video/{ch}/"


def unique_output_basename(execution_id: str | None = None) -> str:
    """Unique ``mv_*`` folder name — safe for parallel Cloud Run jobs."""
    from datetime import datetime, timezone
    import uuid

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if execution_id:
        token = execution_id.rsplit("_", 1)[-1][:8]
        return f"mv_{ts}_{token}"
    return f"mv_{ts}_{uuid.uuid4().hex[:6]}"


def assembly_video_object_key(channel: str, video_id: str) -> str:
    """R2 key for the main MP4 of a finished assembly run."""
    return f"{video_output_prefix(channel)}{video_id}/{video_id}_video.mp4"


def parse_duration(value: str) -> float:
    """Parse a duration string into seconds.

    **Bare numbers are minutes:** ``120`` -> 120 min (2 h). Same for ``--variance 15``.

    Also supports units: ``90m``, ``2h``, ``1h30m``, ``90s``, and clock form ``1:30:00``.
    """
    raw = value.strip()
    if not raw:
        raise argparse.ArgumentTypeError("duration cannot be empty")

    if ":" in raw:
        parts = [p.strip() for p in raw.split(":")]
        try:
            nums = [float(p) for p in parts]
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"invalid duration: {value!r}") from e
        if len(nums) == 2:
            minutes, seconds = nums
            return minutes * 60.0 + seconds
        if len(nums) == 3:
            hours, minutes, seconds = nums
            return hours * 3600.0 + minutes * 60.0 + seconds
        raise argparse.ArgumentTypeError(f"invalid duration: {value!r}")

    total = 0.0
    matched = False
    for m in _DURATION_TOKEN.finditer(raw.replace(" ", "")):
        matched = True
        amount = float(m.group(1))
        unit = (m.group(2) or "m").lower()
        if unit in ("h", "hr", "hrs", "hour", "hours"):
            total += amount * 3600.0
        elif unit in ("m", "min", "mins", "minute", "minutes"):
            total += amount * 60.0
        elif unit in ("s", "sec", "secs", "second", "seconds"):
            total += amount
        else:
            raise argparse.ArgumentTypeError(f"invalid duration unit in {value!r}")

    if not matched:
        raise argparse.ArgumentTypeError(f"invalid duration: {value!r}")
    if total <= 0:
        raise argparse.ArgumentTypeError(f"duration must be positive: {value!r}")
    return total


def duration_bounds_from_env() -> tuple[float | None, float | None]:
    """Read ``ASSEMBLY_DURATION_MIN`` / ``ASSEMBLY_VARIANCE_MIN`` (minutes) as seconds.

    Cloud Run jobs set these via the control plane; CLI flags take precedence when set.
    Returns ``(duration_sec, variance_sec)`` with ``None`` for missing/invalid values.
    """
    duration_sec: float | None = None
    variance_sec: float | None = None
    raw_duration = os.environ.get("ASSEMBLY_DURATION_MIN", "").strip()
    if raw_duration:
        try:
            minutes = float(raw_duration)
            if minutes > 0:
                duration_sec = minutes * 60.0
        except ValueError:
            pass
    raw_variance = os.environ.get("ASSEMBLY_VARIANCE_MIN", "").strip()
    if raw_variance:
        try:
            minutes = float(raw_variance)
            if minutes >= 0:
                variance_sec = minutes * 60.0
        except ValueError:
            pass
    return duration_sec, variance_sec


def resolve_duration_bounds(
    *,
    duration_sec: float | None,
    variance_sec: float | None,
    min_sec: float | None,
    max_sec: float | None,
) -> DurationBounds:
    """Build ``DurationBounds`` from target±variance or explicit min/max."""
    if min_sec is not None or max_sec is not None:
        lo = min_sec if min_sec is not None else (max_sec or DEFAULT_TARGET_SEC) - DEFAULT_VARIANCE_SEC
        hi = max_sec if max_sec is not None else (min_sec or DEFAULT_TARGET_SEC) + DEFAULT_VARIANCE_SEC
        return DurationBounds(min_sec=lo, max_sec=hi)

    if duration_sec is not None:
        margin = variance_sec if variance_sec is not None else DEFAULT_VARIANCE_SEC
        if margin < 0:
            raise ValueError("variance must be non-negative.")
        return DurationBounds(min_sec=duration_sec - margin, max_sec=duration_sec + margin)

    return DurationBounds(
        min_sec=DEFAULT_TARGET_SEC - DEFAULT_VARIANCE_SEC,
        max_sec=DEFAULT_TARGET_SEC + DEFAULT_VARIANCE_SEC,
    )


def add_duration_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``--duration``, ``--variance``, ``--min-duration``, ``--max-duration``."""
    parser.add_argument(
        "--duration",
        type=parse_duration,
        default=None,
        metavar="MIN",
        help=(
            "Target length in minutes if bare number (120 = 2 hours). "
            "Also: 90m, 2h, 1h30m. Default without flags: 75-90 min."
        ),
    )
    parser.add_argument(
        "--variance",
        type=parse_duration,
        default=None,
        metavar="MIN",
        help=(
            "Margin in minutes if bare number (15 = +/- 15 min). "
            "With --duration 120 --variance 15 -> 105-135 min. Default: 7.5 min."
        ),
    )
    parser.add_argument(
        "--min-duration",
        type=parse_duration,
        default=None,
        dest="min_sec",
        metavar="MIN",
        help="Minimum length (minutes if bare number). Overrides --duration/--variance.",
    )
    parser.add_argument(
        "--max-duration",
        type=parse_duration,
        default=None,
        dest="max_sec",
        metavar="MIN",
        help="Maximum length (minutes if bare number). Overrides --duration/--variance.",
    )


@dataclass(frozen=True)
class R2AssemblyPrefixes:
    music_prefix: str
    images_prefix: str
    used_images_prefix: str
    output_prefix: str
    music_folder: str
    images_folder: str
    output_folder: str
    channel: str | None = None


def resolve_r2_assembly_prefixes(
    *,
    category: str | None,
    music_folder: str | None,
    images_folder: str | None,
    output_folder: str | None,
    channel: str | None = None,
) -> R2AssemblyPrefixes:
    """Map CLI folder names to R2 key prefixes under music/, post-processed/, music-video/."""
    base = (category or os.environ.get("ASSEMBLY_CATEGORY", "korean")).strip().strip("/")
    if not base and not (music_folder or images_folder or output_folder):
        raise SystemExit("error: set ASSEMBLY_CATEGORY, --category, or folder flags")

    music = (music_folder or base).strip().strip("/")
    env_images = os.environ.get("ASSEMBLY_IMAGES_FOLDER", "").strip().strip("/")
    images = (images_folder or env_images or base).strip().strip("/")
    output = (output_folder or base).strip().strip("/")

    if not music or not images or not output:
        raise SystemExit("error: music, images, and output folder names cannot be empty")

    ch = resolve_channel_arg(channel)
    if not ch:
        raise SystemExit(
            "error: --channel (YouTube channel slug) is required for music-video output "
            "(set ASSEMBLY_CHANNEL or pass --channel)"
        )
    return R2AssemblyPrefixes(
        music_prefix=f"music/{music}/",
        images_prefix=f"post-processed/{images}/",
        used_images_prefix=f"post-processed/{images}/used/",
        output_prefix=video_output_prefix(ch),
        music_folder=music,
        images_folder=images,
        output_folder=ch,
        channel=ch,
    )


def add_r2_folder_arguments(parser: argparse.ArgumentParser) -> None:
    """Register category / per-asset R2 subfolder flags."""
    parser.add_argument(
        "--category",
        default=None,
        metavar="NAME",
        help=(
            "Subfolder name for music/ and post-processed/ "
            "(e.g. korean). Default: ASSEMBLY_CATEGORY from .env."
        ),
    )
    parser.add_argument(
        "--folder",
        default=None,
        metavar="NAME",
        help="Shorthand: same as --category (one folder for music and backgrounds).",
    )
    parser.add_argument(
        "--music-folder",
        default=None,
        metavar="NAME",
        help="R2 subfolder under music/ (default: --category or --folder).",
    )
    parser.add_argument(
        "--images-folder",
        default=None,
        metavar="NAME",
        help="R2 subfolder under post-processed/ (default: --category or --folder).",
    )
    parser.add_argument(
        "--output-folder",
        default=None,
        metavar="NAME",
        help="Deprecated — output path is music-video/{channel}/; use --channel instead.",
    )
    parser.add_argument(
        "--channel",
        default=None,
        metavar="SLUG",
        help=(
            "YouTube channel slug for music-video/{channel}/ output "
            "(required; default: ASSEMBLY_CHANNEL env)."
        ),
    )


def resolve_folder_args(
    args: argparse.Namespace,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Return (category, music_folder, images_folder, output_folder, channel) from parsed args."""
    category = args.category or args.folder
    return category, args.music_folder, args.images_folder, args.output_folder, args.channel
