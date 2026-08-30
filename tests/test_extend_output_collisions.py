"""Regression tests for same-stem extension output collisions."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import music_assembler.r2_storage as r2_storage
from music_assembler.extend_backgrounds import main as extend_backgrounds_main
from music_assembler.extend_from_r2 import (
    extend_one_claimed_on_r2,
    run_extend_from_r2,
)
from music_assembler.extend_naming import plan_extended_output_names
from music_assembler.r2_storage import (
    R2Config,
    claim_pre_processed_on_r2,
    list_claimable_pre_processed_keys,
)

_ORIGINAL_LIST_OBJECT_KEYS = r2_storage.list_object_keys
_ORIGINAL_OBJECT_EXISTS = r2_storage.object_exists


@pytest.fixture(autouse=True)
def _restore_r2_helpers(monkeypatch):
    """Isolate these tests from global replacements in older race tests."""
    monkeypatch.setattr(r2_storage, "list_object_keys", _ORIGINAL_LIST_OBJECT_KEYS)
    monkeypatch.setattr(r2_storage, "object_exists", _ORIGINAL_OBJECT_EXISTS)


class _ClientError(Exception):
    def __init__(self, code: str = "NoSuchKey") -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _MemoryR2:
    class exceptions:
        ClientError = _ClientError

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)

    def get_paginator(self, _name: str):
        client = self

        class _Paginator:
            def paginate(self, **kwargs):
                prefix = kwargs["Prefix"]
                yield {
                    "Contents": [
                        {"Key": key}
                        for key in sorted(client.objects)
                        if key.startswith(prefix)
                    ]
                }

        return _Paginator()

    def head_object(self, *, Bucket: str, Key: str):  # noqa: N803
        if Key not in self.objects:
            raise _ClientError()
        return {}

    def copy_object(self, *, Bucket: str, Key: str, CopySource, MetadataDirective: str):  # noqa: N803
        source = CopySource["Key"]
        if source not in self.objects:
            raise _ClientError()
        self.objects[Key] = self.objects[source]

    def delete_object(self, *, Bucket: str, Key: str):  # noqa: N803
        self.objects.pop(Key, None)

    def download_file(self, bucket: str, key: str, destination: str) -> None:
        Path(destination).write_bytes(self.objects[key])

    def upload_file(self, source: str, bucket: str, key: str) -> None:
        self.objects[key] = Path(source).read_bytes()


def _cfg() -> R2Config:
    return R2Config(
        bucket="bucket",
        endpoint="https://example.com",
        access_key_id="key",
        secret_access_key="secret",
        category="korean",
    )


def _settings() -> dict[str, object]:
    return {
        "retries": 0,
        "backoff": 0,
        "model": "test",
        "prompt": "test",
        "aspect": "16:9",
        "img_size": "1K",
        "out_w": None,
    }


def test_output_plan_preserves_legacy_name_when_stem_is_unique() -> None:
    assert plan_extended_output_names(["unique.jpg"]) == {
        "unique.jpg": "unique.png"
    }


def test_output_plan_disambiguates_same_stem_and_secondary_collisions() -> None:
    planned = plan_extended_output_names(
        ["cover.jpg", "cover.webp", "cover.jpg.png"]
    )
    assert len(set(planned.values())) == 3
    assert planned["cover.webp"] == "cover.webp.png"


def test_claimable_comparison_uses_collision_safe_output_names() -> None:
    cfg = _cfg()
    client = _MemoryR2(
        {
            f"{cfg.pre_processed_prefix}cover.jpg": b"jpeg-source",
            f"{cfg.pre_processed_prefix}cover.webp": b"webp-source",
            f"{cfg.images_prefix}cover.jpg.png": b"existing-output",
        }
    )

    claimable = list_claimable_pre_processed_keys(
        client,
        cfg.bucket,
        pre_processed_prefix=cfg.pre_processed_prefix,
        images_prefix=cfg.images_prefix,
    )

    assert claimable == [f"{cfg.pre_processed_prefix}cover.webp"]


def test_parallel_claims_preserve_same_stem_outputs(tmp_path, monkeypatch) -> None:
    cfg = _cfg()
    sources = {
        f"{cfg.pre_processed_prefix}cover.jpg": b"jpeg-source",
        f"{cfg.pre_processed_prefix}cover.webp": b"webp-source",
    }
    client = _MemoryR2(sources)

    first = claim_pre_processed_on_r2(
        client,
        cfg.bucket,
        pre_processed_prefix=cfg.pre_processed_prefix,
        images_prefix=cfg.images_prefix,
        execution_id="ext-a",
    )
    second = claim_pre_processed_on_r2(
        client,
        cfg.bucket,
        pre_processed_prefix=cfg.pre_processed_prefix,
        images_prefix=cfg.images_prefix,
        execution_id="ext-b",
    )
    assert {first, second} == {"cover.jpg", "cover.webp"}

    def fake_extend(**kwargs) -> None:
        source = kwargs["image_path"].read_bytes()
        kwargs["out_path"].write_bytes(b"generated:" + source)

    monkeypatch.setattr(
        "music_assembler.extend_from_r2.extend_one_with_retry", fake_extend
    )

    for execution_id, filename in (("ext-a", first), ("ext-b", second)):
        ok, error = extend_one_claimed_on_r2(
            client=client,
            cfg=cfg,
            execution_id=execution_id,
            filename=filename,
            work_dir=tmp_path / execution_id,
            gemini_client=MagicMock(),
            gemini_settings=_settings(),
        )
        assert ok is True
        assert error is None

    output_keys = {
        key
        for key in client.objects
        if key.startswith(cfg.images_prefix)
        and "/" not in key[len(cfg.images_prefix) :]
    }
    assert output_keys == {
        f"{cfg.images_prefix}cover.jpg.png",
        f"{cfg.images_prefix}cover.webp.png",
    }
    assert {client.objects[key] for key in output_keys} == {
        b"generated:jpeg-source",
        b"generated:webp-source",
    }
    assert {
        key
        for key in client.objects
        if key.startswith(cfg.used_pre_processed_prefix)
    } == {
        f"{cfg.used_pre_processed_prefix}cover.jpg",
        f"{cfg.used_pre_processed_prefix}cover.webp",
    }


def test_local_r2_batch_preserves_same_stem_outputs(tmp_path, monkeypatch) -> None:
    cfg = _cfg()
    client = _MemoryR2(
        {
            f"{cfg.pre_processed_prefix}cover.jpg": b"jpeg-source",
            f"{cfg.pre_processed_prefix}cover.webp": b"webp-source",
        }
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "music_assembler.extend_from_r2.r2_config_from_env", lambda **_kwargs: cfg
    )
    monkeypatch.setattr(
        "music_assembler.extend_from_r2.r2_client", lambda _cfg: client
    )
    monkeypatch.setattr(
        "music_assembler.extend_from_r2._load_prompt", lambda _path: "test prompt"
    )

    genai = types.ModuleType("google.genai")
    genai.Client = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
    google = types.ModuleType("google")
    google.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)

    def fake_extend(**kwargs) -> None:
        source = kwargs["image_path"].read_bytes()
        kwargs["out_path"].write_bytes(b"generated:" + source)

    monkeypatch.setattr(
        "music_assembler.extend_from_r2.extend_one_with_retry", fake_extend
    )

    result = run_extend_from_r2(
        process_all=True,
        work_dir=tmp_path,
        keep_work_dir=True,
        workers=2,
    )

    assert result["ok"] == 2
    output_keys = {
        key
        for key in client.objects
        if key.startswith(cfg.images_prefix)
        and "/" not in key[len(cfg.images_prefix) :]
    }
    assert output_keys == {
        f"{cfg.images_prefix}cover.jpg.png",
        f"{cfg.images_prefix}cover.webp.png",
    }
    assert {client.objects[key] for key in output_keys} == {
        b"generated:jpeg-source",
        b"generated:webp-source",
    }


def test_limited_local_r2_batches_keep_names_stable_across_runs(
    tmp_path, monkeypatch
) -> None:
    cfg = _cfg()
    client = _MemoryR2(
        {
            f"{cfg.pre_processed_prefix}cover.jpg": b"jpeg-source",
            f"{cfg.pre_processed_prefix}cover.webp": b"webp-source",
        }
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "music_assembler.extend_from_r2.r2_config_from_env", lambda **_kwargs: cfg
    )
    monkeypatch.setattr(
        "music_assembler.extend_from_r2.r2_client", lambda _cfg: client
    )
    monkeypatch.setattr(
        "music_assembler.extend_from_r2._load_prompt", lambda _path: "test prompt"
    )

    genai = types.ModuleType("google.genai")
    genai.Client = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
    google = types.ModuleType("google")
    google.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)

    def fake_extend(**kwargs) -> None:
        source = kwargs["image_path"].read_bytes()
        kwargs["out_path"].write_bytes(b"generated:" + source)

    monkeypatch.setattr(
        "music_assembler.extend_from_r2.extend_one_with_retry", fake_extend
    )

    first = run_extend_from_r2(
        limit=1,
        work_dir=tmp_path / "first",
        keep_work_dir=True,
        workers=1,
    )
    second = run_extend_from_r2(
        limit=1,
        work_dir=tmp_path / "second",
        keep_work_dir=True,
        workers=1,
    )

    assert first["ok"] == second["ok"] == 1
    output_keys = {
        key
        for key in client.objects
        if key.startswith(cfg.images_prefix)
        and "/" not in key[len(cfg.images_prefix) :]
    }
    assert output_keys == {
        f"{cfg.images_prefix}cover.jpg.png",
        f"{cfg.images_prefix}cover.webp.png",
    }


def test_local_directory_batches_preserve_same_stem_outputs(
    tmp_path, monkeypatch
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "cover.jpg").write_bytes(b"jpeg-source")
    (input_dir / "cover.webp").write_bytes(b"webp-source")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "music_assembler.extend_backgrounds._load_prompt", lambda _path: "test prompt"
    )

    genai = types.ModuleType("google.genai")
    genai.Client = lambda **_kwargs: MagicMock()  # type: ignore[attr-defined]
    google = types.ModuleType("google")
    google.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)

    def fake_extend(**kwargs) -> None:
        source = kwargs["image_path"].read_bytes()
        kwargs["out_path"].write_bytes(b"generated:" + source)

    monkeypatch.setattr(
        "music_assembler.extend_backgrounds.extend_one_with_retry", fake_extend
    )
    args = [
        "--input-dir",
        str(input_dir),
        "--output-dir",
        str(output_dir),
        "--prompt-file",
        str(tmp_path / "prompt.txt"),
        "--limit",
        "1",
        "--workers",
        "1",
    ]

    assert extend_backgrounds_main(args) == 0
    assert extend_backgrounds_main(args) == 0
    assert {path.name for path in output_dir.iterdir()} == {
        "cover.jpg.png",
        "cover.webp.png",
    }
    assert {path.read_bytes() for path in output_dir.iterdir()} == {
        b"generated:jpeg-source",
        b"generated:webp-source",
    }
