"""Regression coverage for partial and ambiguous R2 claim moves."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import music_assembler.r2_storage as r2_storage
from music_assembler.r2_storage import (
    claim_background_on_r2,
    claim_pre_processed_on_r2,
    in_flight_key,
    list_available_background_keys,
    list_claimable_pre_processed_keys,
    pre_processed_in_flight_key,
)

BUCKET = "test-bucket"
IMAGES = "post-processed/korean/"
PRE_PROCESSED = "pre-processed/korean/"


@pytest.fixture(autouse=True)
def _restore_r2_storage_helpers():
    """Isolate these tests from older suites that replace module helpers globally."""
    importlib.reload(r2_storage)
    yield
    importlib.reload(r2_storage)


class _PartialFailureClient:
    """In-memory S3 client with deterministic copy/delete failure modes."""

    def __init__(
        self,
        keys: set[str],
        *,
        fail_copy: bool = False,
        fail_deletes: set[str] | None = None,
        fail_delete_counts: dict[str, int] | None = None,
        delete_then_raise: set[str] | None = None,
    ) -> None:
        self.keys = set(keys)
        self.fail_copy = fail_copy
        self.fail_deletes = fail_deletes or set()
        self.fail_delete_counts = dict(fail_delete_counts or {})
        self.delete_then_raise = set(delete_then_raise or set())
        self.copy_calls = 0
        self.delete_calls = 0
        self.exceptions = SimpleNamespace(ClientError=ClientError)

    def get_paginator(self, operation: str):
        assert operation == "list_objects_v2"
        client = self

        class _Paginator:
            def paginate(self, **kwargs):
                prefix = kwargs["Prefix"]
                yield {
                    "Contents": [
                        {"Key": key}
                        for key in sorted(client.keys)
                        if key.startswith(prefix)
                    ]
                }

        return _Paginator()

    def head_object(self, *, Bucket: str, Key: str):  # noqa: N803
        assert Bucket == BUCKET
        if Key not in self.keys:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "HeadObject",
            )
        return {}

    def copy_object(self, *, Bucket: str, Key: str, CopySource, MetadataDirective: str):  # noqa: N803
        assert Bucket == BUCKET
        assert MetadataDirective == "COPY"
        self.copy_calls += 1
        if self.fail_copy:
            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "forced copy failure"}},
                "CopyObject",
            )
        source = CopySource["Key"]
        assert source in self.keys
        self.keys.add(Key)

    def delete_object(self, *, Bucket: str, Key: str):  # noqa: N803
        assert Bucket == BUCKET
        self.delete_calls += 1
        if Key in self.delete_then_raise:
            self.delete_then_raise.remove(Key)
            self.keys.discard(Key)
            raise ClientError(
                {
                    "Error": {
                        "Code": "InternalError",
                        "Message": "delete completed before response failure",
                    }
                },
                "DeleteObject",
            )
        remaining_failures = self.fail_delete_counts.get(Key, 0)
        if remaining_failures:
            self.fail_delete_counts[Key] = remaining_failures - 1
            raise ClientError(
                {
                    "Error": {
                        "Code": "InternalError",
                        "Message": "transient delete failure",
                    }
                },
                "DeleteObject",
            )
        if Key in self.fail_deletes:
            raise ClientError(
                {
                    "Error": {
                        "Code": "InternalError",
                        "Message": "forced delete failure",
                    }
                },
                "DeleteObject",
            )
        self.keys.discard(Key)


def test_background_delete_failure_retries_source_delete_and_completes_claim() -> None:
    filename = "background.png"
    pool_key = f"{IMAGES}{filename}"
    flight_key = in_flight_key(IMAGES, "asm_first", filename)
    client = _PartialFailureClient(
        {pool_key}, fail_delete_counts={pool_key: 1}
    )

    assert claim_background_on_r2(
        client,
        BUCKET,
        images_prefix=IMAGES,
        execution_id="asm_first",
    ) == filename
    assert client.keys == {flight_key}
    assert list_available_background_keys(client, BUCKET, IMAGES) == []
    assert client.copy_calls == 1
    assert client.delete_calls == 2


def test_background_ambiguous_delete_that_completed_keeps_claim() -> None:
    filename = "ambiguous.png"
    pool_key = f"{IMAGES}{filename}"
    flight_key = in_flight_key(IMAGES, "asm_ambiguous", filename)
    client = _PartialFailureClient({pool_key}, delete_then_raise={pool_key})

    assert claim_background_on_r2(
        client,
        BUCKET,
        images_prefix=IMAGES,
        execution_id="asm_ambiguous",
    ) == filename
    assert client.keys == {flight_key}
    assert client.delete_calls == 1


def test_background_persistent_source_delete_failure_rolls_back_destination() -> None:
    filename = "persistent.png"
    pool_key = f"{IMAGES}{filename}"
    client = _PartialFailureClient({pool_key}, fail_deletes={pool_key})

    assert (
        claim_background_on_r2(
            client,
            BUCKET,
            images_prefix=IMAGES,
            execution_id="asm_persistent",
        )
        is None
    )
    assert client.keys == {pool_key}
    assert list_available_background_keys(client, BUCKET, IMAGES) == [pool_key]
    assert client.copy_calls == 1
    assert client.delete_calls == 3


def test_background_failed_rollback_retains_destination_as_real_claim() -> None:
    filename = "rollback-failed.png"
    pool_key = f"{IMAGES}{filename}"
    flight_key = in_flight_key(IMAGES, "asm_rollback_failed", filename)
    client = _PartialFailureClient(
        {pool_key}, fail_deletes={pool_key, flight_key}
    )

    assert claim_background_on_r2(
        client,
        BUCKET,
        images_prefix=IMAGES,
        execution_id="asm_rollback_failed",
    ) == filename
    assert client.keys == {pool_key, flight_key}
    assert list_available_background_keys(client, BUCKET, IMAGES) == []
    assert client.delete_calls == 3


def test_pre_processed_delete_failure_retries_source_delete_and_completes_claim() -> None:
    filename = "source.jpg"
    pool_key = f"{PRE_PROCESSED}{filename}"
    flight_key = pre_processed_in_flight_key(
        PRE_PROCESSED, "ext_first", filename
    )
    client = _PartialFailureClient(
        {pool_key}, fail_delete_counts={pool_key: 1}
    )

    assert claim_pre_processed_on_r2(
        client,
        BUCKET,
        pre_processed_prefix=PRE_PROCESSED,
        images_prefix=IMAGES,
        execution_id="ext_first",
    ) == filename
    assert client.keys == {flight_key}
    assert client.delete_calls == 2


def test_pre_processed_ambiguous_delete_that_completed_keeps_claim() -> None:
    filename = "ambiguous.jpg"
    pool_key = f"{PRE_PROCESSED}{filename}"
    flight_key = pre_processed_in_flight_key(
        PRE_PROCESSED, "ext_ambiguous", filename
    )
    client = _PartialFailureClient({pool_key}, delete_then_raise={pool_key})

    assert claim_pre_processed_on_r2(
        client,
        BUCKET,
        pre_processed_prefix=PRE_PROCESSED,
        images_prefix=IMAGES,
        execution_id="ext_ambiguous",
    ) == filename
    assert client.keys == {flight_key}
    assert client.delete_calls == 1


def test_pre_processed_persistent_source_delete_failure_rolls_back_destination() -> None:
    filename = "persistent.jpg"
    pool_key = f"{PRE_PROCESSED}{filename}"
    client = _PartialFailureClient({pool_key}, fail_deletes={pool_key})

    assert (
        claim_pre_processed_on_r2(
            client,
            BUCKET,
            pre_processed_prefix=PRE_PROCESSED,
            images_prefix=IMAGES,
            execution_id="ext_persistent",
        )
        is None
    )
    assert client.keys == {pool_key}
    assert (
        list_claimable_pre_processed_keys(
            client,
            BUCKET,
            pre_processed_prefix=PRE_PROCESSED,
            images_prefix=IMAGES,
        )
        == [pool_key]
    )


def test_copy_failure_does_not_create_a_reservation() -> None:
    pool_key = f"{IMAGES}copy-failed.png"
    client = _PartialFailureClient({pool_key}, fail_copy=True)

    assert (
        claim_background_on_r2(
            client,
            BUCKET,
            images_prefix=IMAGES,
            execution_id="asm_copy_failure",
        )
        is None
    )
    assert client.keys == {pool_key}
    assert list_available_background_keys(client, BUCKET, IMAGES) == [pool_key]
