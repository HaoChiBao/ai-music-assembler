"""Place large text *behind* the subject of a photo.

The image is split into two layers via subject segmentation (rembg): the subject
(foreground) and everything else (background). Text is drawn across the
background and the subject is composited back on top, so the letters appear to
pass behind the person/object.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from music_assembler.config import resolve_font_path
from music_assembler.image_text import _load_font, _ring_offsets
from music_assembler.segmentation import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_REMBG_MODEL,
    DEFAULT_SUBJECT_PROMPT,
    refine_mask,
    segment_subject_mask,
)


def _hex_to_rgba(value: str) -> tuple[int, int, int, int]:
    """Parse ``#RRGGBB`` / ``#RRGGBBAA`` (``#`` optional) into an RGBA tuple."""
    s = value.strip().lstrip("#")
    if len(s) == 6:
        r, g, b = (int(s[i : i + 2], 16) for i in (0, 2, 4))
        return (r, g, b, 255)
    if len(s) == 8:
        r, g, b, a = (int(s[i : i + 2], 16) for i in (0, 2, 4, 6))
        return (r, g, b, a)
    raise ValueError(f"Invalid color {value!r}; expected #RRGGBB or #RRGGBBAA")


def _wrap_lines_at_size(
    text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int
) -> list[str]:
    """Word-wrap each explicit line to ``max_width`` (px)."""
    out: list[str] = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        words = paragraph.split()
        if not words:
            out.append("")
            continue
        current: list[str] = []
        for w in words:
            trial = " ".join(current + [w]).strip()
            bbox = font.getbbox(trial)
            if (bbox[2] - bbox[0]) <= max_width or not current:
                current.append(w)
            else:
                out.append(" ".join(current))
                current = [w]
        if current:
            out.append(" ".join(current))
    return out


def _measure(
    lines: list[str], font: ImageFont.FreeTypeFont | ImageFont.ImageFont, line_spacing: float
) -> tuple[int, int, list[int], list[int]]:
    widths: list[int] = []
    heights: list[int] = []
    for line in lines:
        bbox = font.getbbox(line if line else " ")
        widths.append(bbox[2] - bbox[0])
        heights.append(bbox[3] - bbox[1])
    spacing = int(max(heights) * (line_spacing - 1.0)) if heights else 0
    total_h = sum(heights) + spacing * max(0, len(lines) - 1)
    total_w = max(widths) if widths else 0
    return total_w, total_h, widths, heights


def _autofit_font(
    text: str,
    font_path: Path | None,
    img_w: int,
    img_h: int,
    *,
    width_frac: float,
    height_frac: float,
    line_spacing: float,
    log_load: bool,
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, list[str]]:
    """Pick the largest font size whose wrapped text fills ~``width_frac`` of the image."""
    target_w = int(img_w * width_frac)
    target_h = int(img_h * height_frac)

    best_size = 8
    best_lines = text.split("\n")
    lo, hi = 8, max(16, img_h)
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _load_font(mid, font_path, log_load=False)
        lines = _wrap_lines_at_size(text, font, target_w)
        total_w, total_h, _, _ = _measure(lines, font, line_spacing)
        if total_w <= target_w and total_h <= target_h:
            best_size, best_lines = mid, lines
            lo = mid + 1
        else:
            hi = mid - 1

    font = _load_font(best_size, font_path, log_load=log_load)
    return font, best_lines


def render_text_behind_subject(
    image_path: Path,
    text: str,
    output_path: Path,
    *,
    font_key: str,
    font_weight: int | None,
    font_size_px: int | None = None,
    fill_color: tuple[int, int, int, int] = (255, 255, 255, 255),
    stroke_width: int = 0,
    stroke_color: tuple[int, int, int, int] = (0, 0, 0, 255),
    embolden: int = 0,
    width_frac: float = 0.92,
    height_frac: float = 0.6,
    line_spacing: float = 1.05,
    vertical: str = "top",
    segmenter: str = "rembg",
    rembg_model: str = DEFAULT_REMBG_MODEL,
    alpha_matting: bool = False,
    feather_px: float = 1.5,
    mask_shrink_px: int = 1,
    subject_prompt: str = DEFAULT_SUBJECT_PROMPT,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    project_root: Path | None = None,
    log_font_load: bool = False,
) -> None:
    """Composite ``text`` behind the segmented subject of ``image_path``.

    When ``font_size_px`` is ``None`` the text is auto-sized to span ``width_frac``
    of the image width (large letters spanning the back). Output is a PNG.
    """
    root = project_root or Path.cwd()
    font_path = resolve_font_path(font_key, root, weight=font_weight)

    base = Image.open(image_path).convert("RGBA")
    img_w, img_h = base.size

    if font_size_px is None:
        font, lines = _autofit_font(
            text,
            font_path,
            img_w,
            img_h,
            width_frac=width_frac,
            height_frac=height_frac,
            line_spacing=line_spacing,
            log_load=log_font_load,
        )
    else:
        font = _load_font(font_size_px, font_path, log_load=log_font_load)
        lines = _wrap_lines_at_size(text, font, int(img_w * width_frac))

    total_w, total_h, widths, heights = _measure(lines, font, line_spacing)
    spacing = int(max(heights) * (line_spacing - 1.0)) if heights else 0

    x0 = (img_w - total_w) // 2
    if vertical == "top":
        # Top gap matches the horizontal side gap (text is centered, so that gap is x0).
        y0 = x0
    elif vertical == "bottom":
        y0 = img_h - total_h - x0
    else:
        y0 = (img_h - total_h) // 2

    # Text layer (drawn across the whole image; later masked out where the subject is).
    text_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_layer)
    y = y0
    for i, line in enumerate(lines):
        # Center each line within the text block, accounting for the glyph bbox origin.
        bbox = font.getbbox(line if line else " ")
        lx = x0 + (total_w - widths[i]) // 2 - bbox[0]
        ly = y - bbox[1]
        for ox, oy in _ring_offsets(stroke_width):
            draw.text((lx + ox, ly + oy), line, font=font, fill=stroke_color)
        for ox, oy in _ring_offsets(embolden):
            draw.text((lx + ox, ly + oy), line, font=font, fill=fill_color)
        draw.text((lx, ly), line, font=font, fill=fill_color)
        y += heights[i] + spacing

    # Layer 1: background + text. Layer 2: subject pasted back on top.
    composite = Image.alpha_composite(base, text_layer)

    subject_mask = segment_subject_mask(
        base,
        backend=segmenter,
        rembg_model=rembg_model,
        alpha_matting=alpha_matting,
        subject=subject_prompt,
        gemini_model=gemini_model,
    )
    # Shrink off the background fringe, then feather so the subject blends with the
    # text/background behind it instead of looking hand-cut.
    subject_mask = refine_mask(subject_mask, shrink_px=mask_shrink_px, feather_px=feather_px)

    # Soft-alpha composite: the feathered mask becomes the subject's alpha channel.
    subject_layer = base.copy()
    subject_layer.putalpha(subject_mask)
    composite = Image.alpha_composite(composite, subject_layer)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    composite.convert("RGB").save(output_path, format="PNG")
