"""Tests for required background folder (images_folder) validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from music_assembler.api.app import ChannelScheduleRequest, StartJobRequest
from music_assembler.api.r2_catalog import _asset_folder, asset_object_key, list_assets


def test_start_job_requires_images_folder():
    with pytest.raises(ValidationError):
        StartJobRequest(channel="nappabeats", images_folder="")


def test_start_job_normalizes_images_folder():
    req = StartJobRequest(channel="nappabeats", images_folder=" korean ")
    assert req.images_folder == "korean"


def test_schedule_requires_images_folder():
    with pytest.raises(ValidationError):
        ChannelScheduleRequest(images_folder="")


def test_schedule_rejects_path_traversal():
    with pytest.raises(ValidationError):
        ChannelScheduleRequest(images_folder="../evil")


def test_asset_folder_uses_images_folder_for_post_processed():
    assert _asset_folder("korean", "post-processed", "japanese") == "japanese"
    assert _asset_folder("korean", "pre-processed", "japanese") == "korean"


def test_asset_object_key_post_processed_with_images_folder():
    key = asset_object_key("korean", "post-processed", "a.png", images_folder="japanese")
    assert key == "post-processed/japanese/a.png"


def test_list_assets_mock_client():
    class Client:
        class exceptions:
            ClientError = Exception

        def get_paginator(self, _name):
            class Paginator:
                def paginate(self, **_kwargs):
                    return [{"Contents": [{"Key": "post-processed/korean/x.png", "Size": 1}]}]

            return Paginator()

    rows = list_assets(Client(), "b", category="korean", pool="post-processed", images_folder="korean")
    assert rows and rows[0]["name"] == "x.png"
