"""Generate a YouTube title + description for a music-mix video with OpenAI or Gemini.

The creative copy (title + description body) comes from the model, guided by the
prompt in ``prompts/youtube_metadata.txt`` and the list of songs in the mix. The
timestamped tracklist (YouTube chapters) is appended afterward by code so the
chapters are always accurate.

Provider is chosen by ``provider`` / ``YOUTUBE_METADATA_PROVIDER`` (``auto`` by
default): ``auto`` uses OpenAI when ``OPENAI_API_KEY`` is set, otherwise Gemini.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from music_assembler.music_video import format_timestamp

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_PROMPT_FILE = Path("prompts/youtube_metadata.txt")
# Persistent log of titles already used, so we never reuse one.
DEFAULT_USED_TITLES_FILE = Path("youtube_used_titles.txt")

# YouTube hard limits.
MAX_TITLE_LEN = 100
MAX_DESCRIPTION_LEN = 5000


@dataclass
class YouTubeMetadata:
    title: str
    description: str


def _strip_json_fence(text: str) -> str:
    """Remove ```json fences / surrounding prose so ``json.loads`` sees a bare object."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start : end + 1]
    return t


def _normalize_title(title: str) -> str:
    """Lowercased, whitespace-collapsed form for reuse comparison."""
    return " ".join(title.lower().split())


def load_used_titles(path: Path = DEFAULT_USED_TITLES_FILE) -> list[str]:
    """Read previously used titles (one per line); empty list if the file is missing."""
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def record_used_title(title: str, path: Path = DEFAULT_USED_TITLES_FILE) -> None:
    """Append ``title`` to the used-titles log (skips exact/normalized duplicates)."""
    title = title.strip()
    if not title:
        return
    existing = {_normalize_title(t) for t in load_used_titles(path)}
    if _normalize_title(title) in existing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(title + "\n")


def _unique_titles(segments: list[tuple[float, float, str]]) -> list[str]:
    """Song titles in play order, collapsing immediate repeats."""
    out: list[str] = []
    for _start, _end, title in segments:
        if not out or out[-1] != title:
            out.append(title)
    return out


def build_chapters_block(segments: list[tuple[float, float, str]]) -> str:
    """Timestamped tracklist for the description (YouTube chapters; first line is 0:00)."""
    lines = ["Tracklist:"]
    for start, _end, title in segments:
        lines.append(f"{format_timestamp(start)} {title}")
    return "\n".join(lines)


def _compose_description(body: str, segments: list[tuple[float, float, str]]) -> str:
    chapters = build_chapters_block(segments)
    full = f"{body.strip()}\n\n{chapters}\n"
    if len(full) > MAX_DESCRIPTION_LEN:
        # Keep the chapters intact (they make YouTube chapters work); trim the body.
        room = MAX_DESCRIPTION_LEN - (len(chapters) + 3)
        full = f"{body.strip()[: max(0, room)].rstrip()}\n\n{chapters}\n"
    return full


def _resolve_provider(provider: str | None) -> str:
    """Return 'openai' or 'gemini'. ``auto`` prefers OpenAI when its key is present."""
    choice = (provider or os.environ.get("YOUTUBE_METADATA_PROVIDER") or "auto").lower()
    if choice == "auto":
        return "openai" if os.environ.get("OPENAI_API_KEY") else "gemini"
    if choice not in ("openai", "gemini"):
        raise ValueError(f"provider must be 'openai', 'gemini', or 'auto'; got {provider!r}")
    return choice


def _generate_text_openai(prompt: str, model: str, key: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as e:  # pragma: no cover - optional install
        raise RuntimeError("Generating metadata with OpenAI needs: pip install openai") from e
    # Bound each request so a stalled call can't hang the pipeline for minutes.
    client = OpenAI(api_key=key, timeout=60.0, max_retries=2)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or ""


def _generate_text_gemini(prompt: str, model: str, key: str) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:  # pragma: no cover - optional install
        raise RuntimeError("Generating metadata with Gemini needs: pip install google-genai") from e
    client = genai.Client(api_key=key)
    config = types.GenerateContentConfig(response_mime_type="application/json")
    response = client.models.generate_content(model=model, contents=[prompt], config=config)
    return getattr(response, "text", None) or ""


def generate_youtube_metadata(
    segments: list[tuple[float, float, str]],
    total_duration_sec: float,
    *,
    prompt_path: Path = DEFAULT_PROMPT_FILE,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    used_titles: list[str] | None = None,
    max_attempts: int = 4,
) -> YouTubeMetadata:
    """Ask OpenAI or Gemini for a title + description, then append the tracklist.

    ``provider`` is ``openai``, ``gemini``, or ``auto`` (default; prefers OpenAI when
    ``OPENAI_API_KEY`` is set). ``used_titles`` are titles to avoid; the model is told
    not to reuse them and, if it does anyway, generation is retried up to ``max_attempts``
    times. Falls back to a simple title/description only if the model response can't be
    parsed, so the upload step always has usable metadata.
    """
    resolved = _resolve_provider(provider)
    if resolved == "openai":
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "Generating YouTube metadata with OpenAI needs OPENAI_API_KEY in the "
                "environment. Add it to .env (or pass --metadata-provider gemini)."
            )
        model = model or os.environ.get("OPENAI_TEXT_MODEL", DEFAULT_OPENAI_MODEL)

        def generate(text_prompt: str) -> str:
            return _generate_text_openai(text_prompt, model, key)
    else:
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "Generating YouTube metadata with Gemini needs GEMINI_API_KEY (or "
                "GOOGLE_API_KEY) in the environment. Add it to .env (or set OPENAI_API_KEY "
                "to use OpenAI)."
            )
        model = model or os.environ.get("GEMINI_TEXT_MODEL", DEFAULT_GEMINI_MODEL)

        def generate(text_prompt: str) -> str:
            return _generate_text_gemini(text_prompt, model, key)

    base_prompt = prompt_path.read_text(encoding="utf-8").strip()
    songs = _unique_titles(segments)
    song_list = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(songs))
    minutes = int(round(total_duration_sec / 60))
    context = (
        f"\n\nSongs in this mix ({len(songs)} tracks, about {minutes} minutes total):\n{song_list}\n"
    )
    used = list(used_titles or [])
    if used:
        used_block = "\n".join(f"- {t}" for t in used)
        context += (
            "\n\nPreviously used titles (do NOT reuse or closely imitate any of these):\n"
            f"{used_block}\n"
        )
    used_norm = {_normalize_title(t) for t in used}

    title = ""
    body = ""
    for _attempt in range(max(1, max_attempts)):
        text = generate(base_prompt + context)
        cand_title = ""
        cand_body = ""
        try:
            data = json.loads(_strip_json_fence(text))
            if isinstance(data, dict):
                cand_title = str(data.get("title") or "").strip().replace("\n", " ").strip()
                cand_body = str(data.get("description") or "").strip()
        except (json.JSONDecodeError, ValueError):
            pass
        # Keep the first usable response as a fallback, but prefer a non-reused title.
        if cand_title and not title:
            title, body = cand_title, cand_body
        if cand_title and _normalize_title(cand_title) not in used_norm:
            title, body = cand_title, cand_body
            break

    if not title:
        # Fallback title so the upload never blocks on a bad model response.
        first = songs[0] if songs else "Music Mix"
        title = f"{first} & more — {minutes} min music mix"
    if not body:
        body = (
            "A relaxing music mix to study, work, or unwind to. "
            "Press play, get comfortable, and enjoy."
        )

    title = title.replace("\n", " ").strip()[:MAX_TITLE_LEN]
    description = _compose_description(body, segments)
    return YouTubeMetadata(title=title, description=description)
