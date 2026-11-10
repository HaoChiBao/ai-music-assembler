"""Focused reproduction for partial R2 claims; update these assertions with the permanent fix."""

from __future__ import annotations

from types import SimpleNamespace

from botocore.exceptions import ClientError

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


class _PartialFailureClient:
    """In-memory S3 client that can fail a copy or a delete without changing its target."""

    def __init__(
        self,
        keys: set[str],
        *,
        fail_copy: bool = False,
        fail_deletes: set[str] | None = None,
    ) -> None:
        self.keys = set(keys)
        self.fail_copy = fail_copy
        self.fail_deletes = fail_deletes or set()
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


def test_background_delete_failure_leaves_permanently_reserved_pool_source() -> None:
    filename = "background.png"
    pool_key = f"{IMAGES}{filename}"
    flight_key = in_flight_key(IMAGES, "asm_first", filename)
    client = _PartialFailureClient({pool_key}, fail_deletes={pool_key})

    assert (
        claim_background_on_r2(
            client,
            BUCKET,
            images_prefix=IMAGES,
            execution_id="asm_first",
        )
        is None
    )
    assert client.keys == {pool_key, flight_key}
    assert list_available_background_keys(client, BUCKET, IMAGES) == []
    assert (
        claim_background_on_r2(
            client,
            BUCKET,
            images_prefix=IMAGES,
            execution_id="asm_retry",
        )
        is None
    )
    assert client.copy_calls == 1
    assert client.delete_calls == 1


def test_pre_processed_delete_failure_leaves_permanently_reserved_pool_source() -> None:
    filename = "source.jpg"
    pool_key = f"{PRE_PROCESSED}{filename}"
    flight_key = pre_processed_in_flight_key(
        PRE_PROCESSED, "ext_first", filename
    )
    client = _PartialFailureClient({pool_key}, fail_deletes={pool_key})

    assert (
        claim_pre_processed_on_r2(
            client,
            BUCKET,
            pre_processed_prefix=PRE_PROCESSED,
            images_prefix=IMAGES,
            execution_id="ext_first",
        )
        is None
    )
    assert client.keys == {pool_key, flight_key}
    assert (
        list_claimable_pre_processed_keys(
            client,
            BUCKET,
            pre_processed_prefix=PRE_PROCESSED,
            images_prefix=IMAGES,
        )
        == []
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
