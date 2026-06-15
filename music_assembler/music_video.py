"""Assemble a music video: still background + the current song title (bottom-left).

The current song title is drawn in the bottom-left corner with the same margin on
the left and the bottom. The title changes as each track plays. A companion
``*_tracklist.txt`` lists the song at each timestamp (YouTube chapter format).

Note: the bundled FFmpeg has no ``drawtext`` (no libfreetype), so titles are rendered
to transparent PNGs with Pillow and overlaid with timeline-gated ``overlay`` filters.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw

from music_assembler.config import resolve_font_path
from music_assembler.ffmpeg_util import run_ffmpeg_with_progress
from music_assembler.image_text import _load_font

DEFAULT_MARGIN_PX = 64
DEFAULT_TITLE_FONT_SIZE = 46
DEFAULT_TITLE_COLOR = (255, 255, 255, 235)

# Broad-coverage fallback fonts for non-Latin titles (Korean/CJK/etc.). The project's
# bundled fonts (Inria Serif) are Latin-only, so titles with Hangul would render as boxes.
_UNICODE_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def _find_unicode_font() -> Path | None:
    for cand in _UNICODE_FONT_CANDIDATES:
        p = Path(cand)
        if p.is_file():
            return p
    return None


def _needs_unicode_font(text: str) -> bool:
    """True if the text has characters beyond Latin (e.g. Hangul/CJK) that Latin fonts lack."""
    return any(ord(c) > 0x024F for c in text)


def format_timestamp(seconds: float) -> str:
    """``H:MM:SS`` (or ``M:SS`` under an hour) — the format YouTube parses as chapters."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def write_tracklist(path: Path, segments: list[tuple[float, float, str]]) -> None:
    """Write a YouTube-ready tracklist (timestamps + titles, first line at 0:00)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["Tracklist"]
    for start, _end, title in segments:
        lines.append(f"{format_timestamp(start)} {title}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_title_png(
    title: str,
    out_path: Path,
    *,
    width: int,
    font_path: Path | None,
    font_size: int,
    color: tuple[int, int, int, int],
) -> int:
    """Render one left-aligned title on a transparent canvas; return the canvas height.

    The canvas is sized tightly to the font line height so the caller can place the
    canvas bottom a fixed distance from the frame bottom (equal bottom + left margins).
    """
    font = _load_font(font_size, font_path, log_load=False)
    ascent, descent = font.getmetrics()
    shadow = 2
    height = ascent + descent + shadow
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bbox = font.getbbox(title if title else " ")
    x = -bbox[0]  # flush the glyph ink to the left edge
    # Soft shadow so the title stays legible over bright/busy backgrounds.
    draw.text((x + shadow, shadow), title, font=font, fill=(0, 0, 0, 150), anchor="la")
    draw.text((x, 0), title, font=font, fill=color, anchor="la")
    img.save(out_path, format="PNG")
    return height


def render_music_video(
    image_path: Path,
    audio_mp3: Path,
    output_mp4: Path,
    *,
    segments: list[tuple[float, float, str]],
    total_duration_sec: float,
    video_width: int = 1920,
    video_height: int = 1080,
    title_font_path: Path | None = None,
    title_font_size: int = DEFAULT_TITLE_FONT_SIZE,
    title_color: tuple[int, int, int, int] = DEFAULT_TITLE_COLOR,
    margin_px: int = DEFAULT_MARGIN_PX,
    fps: int = 25,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Encode the MP4: background still + the current song title in the bottom-left corner."""
    output_mp4.parent.mkdir(parents=True, exist_ok=True)

    title_area_w = max(1, video_width - 2 * margin_px)
    unicode_font = _find_unicode_font()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        title_pngs: list[Path] = []
        title_heights: list[int] = []
        for i, (_start, _end, title) in enumerate(segments):
            png = tmp_dir / f"title_{i:04d}.png"
            # Use a broad-coverage font for non-Latin titles (e.g. Korean) so they
            # don't render as boxes; otherwise keep the configured (bundled) font.
            if _needs_unicode_font(title) and unicode_font is not None:
                font_for_title: Path | None = unicode_font
            else:
                font_for_title = title_font_path or unicode_font
            h = _render_title_png(
                title,
                png,
                width=title_area_w,
                font_path=font_for_title,
                font_size=title_font_size,
                color=title_color,
            )
            title_pngs.append(png)
            title_heights.append(h)

        args: list[str] = ["-loop", "1", "-i", str(image_path), "-i", str(audio_mp3)]
        for png in title_pngs:
            args += ["-loop", "1", "-i", str(png)]

        parts = [
            f"[0:v]scale={video_width}:{video_height}:force_original_aspect_ratio=decrease,"
            f"pad={video_width}:{video_height}:(ow-iw)/2:(oh-ih)/2,setsar=1[bg]"
        ]
        prev = "bg"
        for i, (start, end, _title) in enumerate(segments):
            nxt = f"v{i + 1}"
            # Bottom-left: same margin on the left (x) and the bottom (canvas bottom at H - margin).
            title_y = video_height - margin_px - title_heights[i]
            parts.append(
                f"[{prev}][{i + 2}:v]overlay=x={margin_px}:y={title_y}:"
                f"enable='between(t,{start:.3f},{end:.3f})'[{nxt}]"
            )
            prev = nxt

        filter_complex = ";".join(parts)
        args += [
            "-filter_complex",
            filter_complex,
            "-map",
            f"[{prev}]",
            "-map",
            "1:a",
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            # Bound the output explicitly: looped image inputs in a filter_complex make
            # -shortest unreliable, so cap at the audio/mix duration.
            "-t",
            f"{total_duration_sec:.3f}",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_mp4),
        ]

        run_ffmpeg_with_progress(
            args, duration_sec=total_duration_sec, on_progress=on_progress or (lambda _p: None)
        )


def resolve_title_font(font_key: str, project_root: Path, weight: int | None) -> Path | None:
    """Resolve the on-screen title font file (delegates to config)."""
    return resolve_font_path(font_key, project_root, weight=weight)
