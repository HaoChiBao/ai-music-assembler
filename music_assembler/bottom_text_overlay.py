"""Bottom-centered caption on still images using project fonts (see ``fonts/``)."""

from __future__ import annotations

from pathlib import Path

from music_assembler.config import TextOverlayStyle, default_font_stem, first_font_stem_in_project
from music_assembler.image_text import render_image_with_text


def resolve_font_key(project_root: Path, font_key: str | None, weight: int | None = None) -> str:
    """Use explicit ``font_key``, else bundled default (Inria Light when present), else first match, else ``arial``."""
    if font_key:
        return font_key.strip()
    d = default_font_stem(project_root, weight=weight)
    if d:
        return d
    first = first_font_stem_in_project(project_root, weight=weight)
    if first:
        return first
    return "arial"


def render_text_overlay(
    image_path: Path,
    text: str,
    output_path: Path,
    *,
    font_key: str,
    font_size_px: int,
    margin_px: int,
    margin_bottom_px: int | None = None,
    horizontal: str,
    vertical: str,
    stroke_width: int,
    embolden: int,
    font_weight: int | None,
    project_root: Path,
    log_font_load: bool = False,
) -> None:
    style = TextOverlayStyle(
        font_key=font_key,
        font_size_px=font_size_px,
        font_weight=font_weight,
        horizontal=horizontal,
        vertical=vertical,
        margin_px=margin_px,
        margin_bottom_px=margin_bottom_px,
        stroke_width=stroke_width,
        embolden=embolden,
    )
    render_image_with_text(
        image_path,
        text,
        output_path,
        style,
        project_root=project_root,
        log_font_load=log_font_load,
    )

