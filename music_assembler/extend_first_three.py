"""
Extend only the first three images in pre-processed/ (sorted by filename).

Same as `extend-backgrounds --limit 3` (Gemini image API). For other counts, use `extend-backgrounds --limit N`.
"""

from __future__ import annotations

import sys

from music_assembler.extend_backgrounds import main as extend_main


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return extend_main(["--limit", "3", *argv])


if __name__ == "__main__":
    raise SystemExit(main())
