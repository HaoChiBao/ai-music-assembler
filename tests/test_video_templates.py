"""Tests for the video template registry."""

from __future__ import annotations

import pytest

from music_assembler.video_templates import (
    DEFAULT_TEMPLATE_ID,
    ENV_TEMPLATE_ID,
    PLAYLIST_LANDSCAPE,
    SHORTS_VERTICAL,
    UnknownTemplateError,
    get_template,
    list_template_ids,
    list_templates,
    normalize_template_id,
    resolve_template,
    resolve_template_id,
    templates_public_list,
)


def test_default_template_is_playlist_landscape():
    assert DEFAULT_TEMPLATE_ID == "playlist_landscape"
    assert get_template(None).id == DEFAULT_TEMPLATE_ID
    assert get_template("").id == DEFAULT_TEMPLATE_ID
    assert normalize_template_id(None) == DEFAULT_TEMPLATE_ID


def test_list_templates_includes_landscape_and_shorts():
    ids = list_template_ids()
    assert "playlist_landscape" in ids
    assert "shorts_vertical" in ids
    assert ids[0] == DEFAULT_TEMPLATE_ID


def test_playlist_landscape_geometry():
    t = PLAYLIST_LANDSCAPE
    assert t.video_width == 1920
    assert t.video_height == 1080
    assert t.aspect_label == "16:9"
    assert t.orientation == "landscape"
    assert t.thumbnail_strategy == "text_behind_subject"
    assert t.gemini_aspect_ratio == "16:9"


def test_shorts_vertical_geometry():
    t = SHORTS_VERTICAL
    assert t.video_width == 1080
    assert t.video_height == 1920
    assert t.aspect_label == "9:16"
    assert t.orientation == "portrait"
    assert t.default_duration_min == 1
    assert t.gemini_aspect_ratio == "9:16"


def test_unknown_template_raises():
    with pytest.raises(UnknownTemplateError):
        get_template("not_a_real_template")


def test_resolve_template_id_from_env(monkeypatch):
    monkeypatch.setenv(ENV_TEMPLATE_ID, "shorts_vertical")
    assert resolve_template_id(None) == "shorts_vertical"
    assert resolve_template(None).id == "shorts_vertical"
    # Explicit arg wins over env.
    assert resolve_template_id("playlist_landscape") == "playlist_landscape"


def test_resolve_template_id_clears_blank_env(monkeypatch):
    monkeypatch.delenv(ENV_TEMPLATE_ID, raising=False)
    assert resolve_template_id(None) == DEFAULT_TEMPLATE_ID


def test_public_list_shape():
    public = templates_public_list()
    assert len(public) >= 2
    row = next(r for r in public if r["id"] == "playlist_landscape")
    assert row["is_default"] is True
    assert row["aspect_label"] == "16:9"
    assert "description" in row
    assert isinstance(row["tags"], list)


def test_text_overlay_style_from_template():
    style = SHORTS_VERTICAL.text_overlay_style(font_key="InriaSerif-Regular")
    assert style.font_key == "InriaSerif-Regular"
    assert style.font_size_px == SHORTS_VERTICAL.title_font_size_px


def test_duration_bounds_positive():
    bounds = SHORTS_VERTICAL.duration_bounds()
    assert bounds.min_sec >= 1.0
    assert bounds.max_sec >= bounds.min_sec
    playlist = PLAYLIST_LANDSCAPE.duration_bounds()
    assert playlist.min_sec < playlist.max_sec


def test_list_templates_stable_order():
    a = [t.id for t in list_templates()]
    b = [t.id for t in list_templates()]
    assert a == b
