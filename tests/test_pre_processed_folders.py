"""Tests for pre-processed folder discovery and extend source_folder validation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from music_assembler.api import app as app_module
from music_assembler.api.app import StartExtendRequest
from music_assembler.api.r2_catalog import list_pre_processed_folders
from music_assembler.r2_storage import (
    extend_prefixes_for_folder,
    normalize_source_folder,
)


def test_normalize_source_folder():
    assert normalize_source_folder(" korean ") == "korean"


def test_normalize_source_folder_rejects_nested():
    with pytest.raises(ValueError):
        normalize_source_folder("a/b")


def test_normalize_source_folder_rejects_reserved():
    with pytest.raises(ValueError):
        normalize_source_folder("used")
    with pytest.raises(ValueError):
        normalize_source_folder("in-flight")


def test_extend_prefixes_for_folder():
    prefixes = extend_prefixes_for_folder("lofi")
    assert prefixes.source_folder == "lofi"
    assert prefixes.pre_processed_prefix == "pre-processed/lofi/"
    assert prefixes.used_pre_processed_prefix == "pre-processed/lofi/used/"
    assert prefixes.images_prefix == "post-processed/lofi/"


def test_start_extend_allows_category_fallback():
    req = StartExtendRequest(category="korean", limit=1)
    assert req.source_folder is None


def test_start_extend_uses_category_as_source_folder(monkeypatch):
    seen: dict[str, str] = {}

    monkeypatch.setattr(app_module, "_r2", lambda: ("client", "bucket"))
    monkeypatch.setattr(
        app_module,
        "_assert_pre_processed_folder_exists",
        lambda _client, _bucket, folder: seen.setdefault("asserted_folder", folder),
    )
    monkeypatch.setattr(app_module, "_invalidate_category_cache", lambda _category: None)
    monkeypatch.setattr(
        app_module,
        "r2_config_from_env",
        lambda *, category: SimpleNamespace(category=category),
    )

    def count_pending(_client, _cfg, *, force, source_folder):
        assert force is False
        seen["counted_folder"] = source_folder
        return 1

    def queue_job(_client, _bucket, _settings, **kwargs):
        seen["queued_folder"] = kwargs["source_folder"]
        return {"execution_id": kwargs["execution_id"], "status": "starting"}

    monkeypatch.setattr(app_module, "count_pending_r2_sources", count_pending)
    monkeypatch.setattr(app_module, "_new_extend_id", lambda: "ext_test")
    monkeypatch.setattr(app_module, "_queue_extend_job", queue_job)

    result = app_module.start_extend(
        StartExtendRequest(category="korean", limit=1),
        _auth=None,
        settings=SimpleNamespace(default_category="default"),
    )

    assert result["execution_id"] == "ext_test"
    assert result["source_folder"] == "korean"
    assert seen == {
        "asserted_folder": "korean",
        "counted_folder": "korean",
        "queued_folder": "korean",
    }


def test_start_extend_normalizes_source_folder():
    req = StartExtendRequest(source_folder=" korean ", limit=3)
    assert req.source_folder == "korean"
    assert req.limit == 3


def test_start_extend_rejects_limit_below_one():
    with pytest.raises(ValidationError):
        StartExtendRequest(source_folder="korean", limit=0)


def test_start_extend_allows_large_batch():
    req = StartExtendRequest(source_folder="korean", limit=50)
    assert req.limit == 50


def test_list_pre_processed_folders_excludes_reserved():
    class Client:
        def get_paginator(self, _name):
            class Paginator:
                def paginate(self, **_kwargs):
                    return [
                        {
                            "CommonPrefixes": [
                                {"Prefix": "pre-processed/korean/"},
                                {"Prefix": "pre-processed/lofi/"},
                                {"Prefix": "pre-processed/used/"},
                                {"Prefix": "pre-processed/in-flight/"},
                            ]
                        }
                    ]

            return Paginator()

    folders = list_pre_processed_folders(Client(), "b")
    assert folders == ["korean", "lofi"]
