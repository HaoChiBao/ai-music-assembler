"""CLI: overlay text on the first three images in ``post-processed/`` (placement is configurable)."""

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

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP"}

# Default light face: ``fonts/Inria_Serif/InriaSerif-Light.ttf`` (see ``DEFAULT_BUNDLED_LIGHT_FONT_*`` in config).
DEFAULT_FONT_VARIANT = DEFAULT_BUNDLED_LIGHT_FONT_STEM

# --- Edit defaults here (or use CLI flags / env-style overrides in your shell) ---
DEFAULT_FONT_SIZE = 80
DEFAULT_FONT_WEIGHT = 300
DEFAULT_STROKE_WIDTH = 0
DEFAULT_EMBOLDEN = 0
DEFAULT_MARGIN_PX = 40
# Extra space from the bottom edge to the text (does not narrow horizontal wrap; see --margin).
DEFAULT_MARGIN_BOTTOM_PX = 70
DEFAULT_HORIZONTAL = "center"
DEFAULT_VERTICAL = "bottom"


def _discover_images(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    return sorted(p for p in input_dir.iterdir() if p.suffix in IMAGE_EXTS and p.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="add-text",
        description=(
            "Overlay text on the first three images (by filename) in the input folder; "
            "writes PNGs to the output folder. Placement and outline are configurable."
        ),
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
        "--input-dir",
        type=Path,
        default=Path("post-processed"),
        help="Folder of backgrounds (default: post-processed).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("post-text-processed"),
        help="Output folder (default: post-text-processed).",
    )
    parser.add_argument(
        "--font",
        default=None,
        metavar="STEM",
        help=(
            f"Face stem in fonts/ (e.g. {DEFAULT_FONT_VARIANT}, InriaSerif-Light). "
            "Default: bundled Inria Serif Light when present; run --list-fonts to see stems."
        ),
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=DEFAULT_FONT_SIZE,
        help=f"Font size in pixels (default: {DEFAULT_FONT_SIZE}).",
    )
    parser.add_argument(
        "--font-weight",
        type=int,
        default=DEFAULT_FONT_WEIGHT,
        metavar="N",
        help=(
            "CSS-style weight when resolving a face without an exact --font (300 = Light, 400 = Regular, 700 = Bold). "
            f"Default: {DEFAULT_FONT_WEIGHT}. Use --font with a stem for exact control."
        ),
    )
    parser.add_argument(
        "--h-align",
        choices=("left", "center", "right"),
        default=DEFAULT_HORIZONTAL,
        help=f"Horizontal anchor: left, center, or right (default: {DEFAULT_HORIZONTAL}).",
    )
    parser.add_argument(
        "--v-align",
        choices=("top", "center", "bottom"),
        default=DEFAULT_VERTICAL,
        help=f"Vertical anchor: top, center, or bottom (default: {DEFAULT_VERTICAL}).",
    )
    parser.add_argument(
        "--margin",
        type=int,
        default=DEFAULT_MARGIN_PX,
        help=(
            f"Horizontal inset for line wrapping (both sides) in pixels (default: {DEFAULT_MARGIN_PX}). "
            "Bottom gap when --v-align bottom is set separately via --margin-bottom."
        ),
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
        default=DEFAULT_STROKE_WIDTH,
        help=(
            f"Outline thickness (rings in 8 directions; 0 = no outline). "
            f"Default: {DEFAULT_STROKE_WIDTH}. Lower = thinner halo."
        ),
    )
    parser.add_argument(
        "--embolden",
        type=int,
        default=DEFAULT_EMBOLDEN,
        help=(
            f"Simulated font weight: extra fill passes at small offsets (0 = lightest, "
            f"1–2 = heavier). Default: {DEFAULT_EMBOLDEN}."
        ),
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Project root for fonts/ (default: current directory).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files in the output folder.",
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

    try:
        images = _discover_images(args.input_dir.resolve())
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2

    batch = images[:3]
    if not batch:
        print(f"No images in {args.input_dir}", file=sys.stderr)
        return 2

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    for src in batch:
        out_name = src.stem + ".png"
        dest = out_dir / out_name
        if dest.is_file() and not args.force:
            print(f"skip (exists): {dest.name}")
            continue
        print(f"{src.name} -> {dest.name}")
        try:
            render_text_overlay(
                src,
                text,
                dest,
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
            print(f"error: {src.name}: {e}", file=sys.stderr)
            return 1

    print(f"Done ({len(batch)} file(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
