"""Render horizontal artwork with overlaid text (Figma-style typography)."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from music_assembler.config import TextOverlayStyle, resolve_font_path

# Neighbors for outline rings and embolden passes (8 directions).
_NEI8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))

# Avoid repeating the same PIL font log for every frame in a batch.
_LOGGED_FONT_KEYS: set[tuple[str, int]] = set()


def _ring_offsets(radius_layers: int) -> list[tuple[int, int]]:
    """Offsets for each ring 1..radius_layers in eight directions (thinner than a solid square)."""
    if radius_layers <= 0:
        return []
    out: list[tuple[int, int]] = []
    for r in range(1, radius_layers + 1):
        for dx, dy in _NEI8:
            out.append((dx * r, dy * r))
    return out


def _load_font(
    size_px: int,
    font_path: Path | None,
    *,
    log_load: bool = False,
) -> ImageFont.ImageFont:
    if font_path and font_path.is_file():
        try:
            font = ImageFont.truetype(str(font_path), size=size_px)
            if log_load:
                key = (str(font_path.resolve()), size_px)
                if key not in _LOGGED_FONT_KEYS:
                    _LOGGED_FONT_KEYS.add(key)
                    try:
                        name = font.getname()  # type: ignore[union-attr]
                        print(
                            f"PIL loaded TrueType: {font_path.resolve()} → family={name[0]!r} style={name[1]!r}",
                            file=sys.stderr,
                        )
                    except (AttributeError, TypeError, OSError):
                        print(f"PIL loaded TrueType: {font_path.resolve()}", file=sys.stderr)
            return font
        except OSError as e:
            print(
                f"warning: could not load {font_path.resolve()}: {e}; using PIL bitmap fallback (text will look thick)",
                file=sys.stderr,
            )
            return ImageFont.load_default()
    elif font_path:
        print(
            f"warning: font path missing or not a file: {font_path!r}; using PIL bitmap fallback",
            file=sys.stderr,
        )
    else:
        print(
            "warning: no font file resolved for this font_key; using PIL bitmap fallback (not Inria Light)",
            file=sys.stderr,
        )
    return ImageFont.load_default()


def _wrap_lines(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current: list[str] = []
        for w in words:
            trial = (" ".join(current + [w])).strip()
            bbox = font.getbbox(trial)
            if bbox[2] - bbox[0] <= max_width or not current:
                current.append(w)
            else:
                lines.append(" ".join(current))
                current = [w]
        if current:
            lines.append(" ".join(current))
    return lines


def _anchor_position(
    img_w: int,
    img_h: int,
    text_w: int,
    text_h: int,
    style: TextOverlayStyle,
) -> tuple[int, int]:
    m = style.margin_px
    h = style.horizontal
    v = style.vertical
    if h == "left":
        x = m
    elif h == "right":
        x = img_w - text_w - m
    else:
        x = (img_w - text_w) // 2
    if v == "top":
        y = m
    elif v == "bottom":
        bottom_m = style.margin_bottom_px if style.margin_bottom_px is not None else m
        y = img_h - text_h - bottom_m
    else:
        y = (img_h - text_h) // 2
    return x, y


def render_image_with_text(
    image_path: Path,
    text: str,
    output_path: Path,
    style: TextOverlayStyle,
    project_root: Path | None = None,
    *,
    log_font_load: bool = False,
) -> None:
    root = project_root or Path.cwd()
    font_path = resolve_font_path(style.font_key, root, weight=style.font_weight)
    img = Image.open(image_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    font = _load_font(style.font_size_px, font_path, log_load=log_font_load)

    max_w = img.width - 2 * style.margin_px
    lines = _wrap_lines(text, font, max_w)
    line_heights: list[int] = []
    line_widths: list[int] = []
    for line in lines:
        bbox = font.getbbox(line)
        line_heights.append(bbox[3] - bbox[1])
        line_widths.append(bbox[2] - bbox[0])

    spacing = int(style.font_size_px * (style.line_spacing - 1.0))
    total_h = sum(line_heights) + spacing * max(0, len(lines) - 1)
    total_w = max(line_widths) if line_widths else 0

    x0, y0 = _anchor_position(img.width, img.height, total_w, total_h, style)

    y = y0
    for i, line in enumerate(lines):
        bbox = font.getbbox(line)
        w = bbox[2] - bbox[0]
        if style.horizontal == "center":
            lx = x0 + (total_w - w) // 2
        elif style.horizontal == "right":
            lx = x0 + (total_w - w)
        else:
            lx = x0
        for ox, oy in _ring_offsets(style.stroke_width):
            draw.text(
                (lx + ox, y + oy),
                line,
                font=font,
                fill=style.stroke_color,
            )
        for ox, oy in _ring_offsets(style.embolden):
            draw.text((lx + ox, y + oy), line, font=font, fill=style.fill_color)
        draw.text((lx, y), line, font=font, fill=style.fill_color)
        y += line_heights[i] + spacing

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, format="PNG")
