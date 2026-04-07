"""CLI: draw text on one image using ``fonts/`` (or system fallback); placement is configurable."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from music_assembler import __version__
from music_assembler.bottom_text_overlay import (
    render_text_overlay,
    resolve_font_key,
)
from music_assembler.config import DEFAULT_BUNDLED_LIGHT_FONT_STEM, discover_font_stems

DEFAULT_FONT_VARIANT = DEFAULT_BUNDLED_LIGHT_FONT_STEM
DEFAULT_MARGIN_BOTTOM_PX = 96


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="add-bottom-text",
        description="Overlay text on an image (uses fonts/ when available). Default: bottom center, no outline.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--list-fonts",
        action="store_true",
        help="List all font face stems under fonts/ and exit.",
    )
    parser.add_argument(
        "--text",
        "-t",
        default=None,
        help="Caption text (use \\n for multiple lines). Required unless --list-fonts.",
    )
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        required=True,
        help="Source image (PNG, JPEG, WebP).",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        required=True,
        help="Output image path (PNG).",
    )
    parser.add_argument(
        "--font",
        default=None,
        metavar="STEM",
        help=(
            f"Face stem in fonts/ (e.g. {DEFAULT_FONT_VARIANT}). "
            "Default: bundled Inria Serif Light when present. Use --list-fonts."
        ),
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=96,
        help="Font size in pixels (default: 96).",
    )
    parser.add_argument(
        "--font-weight",
        type=int,
        default=300,
        metavar="N",
        help="CSS-style weight when resolving a face (300 = Light, 400 = Regular, 700 = Bold). Default: 300.",
    )
    parser.add_argument(
        "--h-align",
        choices=("left", "center", "right"),
        default="center",
        help="Horizontal anchor (default: center).",
    )
    parser.add_argument(
        "--v-align",
        choices=("top", "center", "bottom"),
        default="bottom",
        help="Vertical anchor (default: bottom).",
    )
    parser.add_argument(
        "--margin",
        type=int,
        default=48,
        help="Horizontal inset for line wrapping (both sides) in pixels (default: 48).",
    )
    parser.add_argument(
        "--margin-bottom",
        type=int,
        default=DEFAULT_MARGIN_BOTTOM_PX,
        metavar="PX",
        help=(
            f"Pixels from the bottom of the image to the text when --v-align is bottom "
            f"(default: {DEFAULT_MARGIN_BOTTOM_PX}). Ignored for top/center vertical alignment."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print which TrueType font PIL loaded (stderr). Warnings still print on fallback.",
    )
    parser.add_argument(
        "--stroke-width",
        type=int,
        default=0,
        help="Outline thickness (8-direction rings; 0 = no outline). Default: 0.",
    )
    parser.add_argument(
        "--embolden",
        type=int,
        default=0,
        help="Simulated weight: 0 = lightest, 1–2 = bolder (default: 0).",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Project root for resolving fonts/ (default: current directory).",
    )
    args = parser.parse_args(argv)
    root = args.project_root.resolve()

    if args.list_fonts:
        for stem in discover_font_stems(root):
            print(stem)
        return 0

    if args.text is None:
        parser.error("--text is required unless you pass --list-fonts")

    text = args.text.replace("\\n", "\n")
    font_key = resolve_font_key(root, args.font, weight=args.font_weight)
    margin_bottom = args.margin_bottom if args.v_align == "bottom" else None

    if not args.input.is_file():
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 2

    try:
        render_text_overlay(
            args.input.resolve(),
            text,
            args.output.resolve(),
            font_key=font_key,
            font_size_px=args.font_size,
            margin_px=args.margin,
            margin_bottom_px=margin_bottom,
            horizontal=args.h_align,
            vertical=args.v_align,
            stroke_width=args.stroke_width,
            embolden=args.embolden,
            font_weight=args.font_weight,
            project_root=root,
            log_font_load=not args.quiet,
        )
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
