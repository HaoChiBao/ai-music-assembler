"""Tests for pre-processed folder discovery and extend source_folder validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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


def test_start_extend_requires_source_folder():
    with pytest.raises(ValidationError):
        StartExtendRequest(limit=1)


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
