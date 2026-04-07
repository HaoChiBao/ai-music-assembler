"""Generate YouTube title + description via OpenAI; write a simple .txt for uploads."""

from __future__ import annotations

import json
import os
from pathlib import Path


def generate_youtube_title_description(caption: str, *, api_key: str) -> tuple[str, str]:
    """
    Return (title, description) matching the project’s house style.
    Raises if ``api_key`` is missing or the API call fails.
    """
    key = api_key.strip()
    if not key:
        raise ValueError("OPENAI_API_KEY is not set")

    from openai import OpenAI

    client = OpenAI(api_key=key)
    caption_clean = caption.strip()
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    system = (
        "You write YouTube metadata for one music video. Reply with JSON only: "
        '{"title": string, "description": string}.\n\n'
        "Title (single line): Start with a short quoted phrase from the video theme (the caption). "
        "Then an evocative subtitle (no quotes), optional one emoji, then \" | \" and 2–4 mood fragments "
        "joined with middle dot · (e.g. smooth R&B song · aesthetic · mood). Keep under ~100 characters.\n\n"
        "Description: Line 1 = the main song/title words (short, no quotes), matching the vibe. "
        "Line 2 = blank line. Line 3+ = hashtags only, each starting with #, separated by two spaces, "
        "8–12 tags (e.g. #krnb #korean #rnb #eveningwalk). Match genre/mood to the caption; use Latin hashtags if the song is Korean-leaning RnB as in the examples."
    )
    user = f"Video on-screen caption / theme:\n{caption_clean}"

    r = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.7,
    )
    raw = (r.choices[0].message.content or "").strip()
    data = json.loads(raw)
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()
    if not title or not description:
        raise ValueError("OpenAI returned empty title or description")
    return title, description


def write_youtube_metadata_txt(path: Path, title: str, description: str) -> None:
    """Plain text: Title / Description sections (easy to copy-paste into YouTube)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"Title\n{title}\n\nDescription\n{description}\n"
    path.write_text(body, encoding="utf-8")
