from __future__ import annotations

from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from audio_archive.cloud.models import DELIVERY_ROLES, CloudOutput
from audio_archive.cloud.storage import (
    R2DeliveryStorage,
    object_key_for,
    validate_download_filename,
)
from audio_archive.verify import sha256_file


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.uploads: list[dict[str, object]] = []
        self.deleted: list[tuple[str, str]] = []
        self.presigned: list[dict[str, object]] = []
        self.force_bad_size = False
        self.force_bad_sha = False

    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        ExtraArgs: dict[str, object] | None = None,
    ) -> None:
        data = Path(filename).read_bytes()
        extra = ExtraArgs or {}
        metadata = dict(extra.get("Metadata") or {})
        self.objects[(bucket, key)] = {
            "ContentLength": len(data),
            "ContentDisposition": extra.get("ContentDisposition"),
            "Metadata": metadata,
        }
        self.uploads.append(
            {
                "filename": filename,
                "bucket": bucket,
                "key": key,
                "extra": extra,
            }
        )

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        try:
            result = dict(self.objects[(Bucket, Key)])
        except KeyError as exc:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            ) from exc
        if self.force_bad_size:
            result["ContentLength"] = int(result["ContentLength"]) + 1
        if self.force_bad_sha:
            metadata = dict(result["Metadata"])
            metadata["sha256"] = "0" * 64
            result["Metadata"] = metadata
        return result

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.objects.pop((Bucket, Key), None)
        self.deleted.append((Bucket, Key))
        return {}

    def generate_presigned_url(
        self,
        operation_name: str,
        *,
        Params: dict[str, object],
        ExpiresIn: int,
    ) -> str:
        self.presigned.append(
            {
                "operation_name": operation_name,
                "params": Params,
                "expires_in": ExpiresIn,
            }
        )
        return "https://signed.example/download"


def _storage(client: FakeS3Client | None = None) -> tuple[R2DeliveryStorage, FakeS3Client]:
    fake = client or FakeS3Client()
    storage = R2DeliveryStorage(
        bucket="audio-archive-delivery",
        client=fake,  # type: ignore[arg-type]
        signed_url_ttl_seconds=900,
    )
    return storage, fake


def test_object_key_is_deterministic_and_omits_human_metadata() -> None:
    digest = "a" * 64
    key = object_key_for(job_id=42, role="ableton", sha256=digest, suffix=".wav")

    assert key == f"delivery/42/ableton/{digest}.wav"
    assert "Portishead" not in key
    assert "Roads" not in key


def test_publish_file_verifies_local_digest_and_remote_size(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"verified audio fixture")
    expected = sha256_file(source)
    storage, client = _storage()

    published = storage.publish_file(
        job_id=7,
        role="ableton",
        path=source,
        filename="Portishead - Roads.wav",
        content_type="audio/wav",
        expected_sha256=expected,
    )

    assert published.sha256 == expected
    assert published.size_bytes == source.stat().st_size
    assert published.filename == "Portishead - Roads.wav"
    assert published.object_key == f"delivery/7/ableton/{expected}.wav"
    extra = client.uploads[0]["extra"]
    assert extra["ContentType"] == "audio/wav"
    assert "filename*=UTF-8''Portishead%20-%20Roads.wav" in str(extra["ContentDisposition"])
    assert extra["Metadata"] == {"sha256": expected, "role": "ableton"}


def test_publish_refuses_file_that_no_longer_matches_verified_digest(tmp_path: Path) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"changed after verification")
    storage, client = _storage()

    with pytest.raises(ValueError, match="verified expected digest"):
        storage.publish_file(
            job_id=1,
            role="source",
            path=source,
            filename="source.webm",
            content_type="audio/webm",
            expected_sha256="0" * 64,
        )

    assert client.uploads == []


def test_publish_deletes_remote_object_when_size_verification_fails(tmp_path: Path) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"fixture")
    client = FakeS3Client()
    client.force_bad_size = True
    storage, _ = _storage(client)

    with pytest.raises(RuntimeError, match="size mismatch"):
        storage.publish_file(
            job_id=1,
            role="source",
            path=source,
            filename="source.webm",
            content_type="audio/webm",
            expected_sha256=sha256_file(source),
        )

    assert len(client.deleted) == 1
    assert client.objects == {}


def test_publish_deletes_remote_object_when_sha_metadata_fails(tmp_path: Path) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"fixture")
    client = FakeS3Client()
    client.force_bad_sha = True
    storage, _ = _storage(client)

    with pytest.raises(RuntimeError, match="SHA-256 metadata"):
        storage.publish_file(
            job_id=1,
            role="source",
            path=source,
            filename="source.webm",
            content_type="audio/webm",
            expected_sha256=sha256_file(source),
        )

    assert len(client.deleted) == 1


def test_presigned_download_uses_locked_ttl_and_plain_get_object() -> None:
    storage, client = _storage()

    url = storage.create_download_url(
        object_key=f"delivery/1/source/{'a' * 64}.webm",
        filename="Björk - Jóga.webm",
    )

    assert url == "https://signed.example/download"
    request = client.presigned[0]
    assert request["operation_name"] == "get_object"
    assert request["expires_in"] == 900
    assert request["params"] == {
        "Bucket": "audio-archive-delivery",
        "Key": f"delivery/1/source/{'a' * 64}.webm",
    }


def test_object_exists_distinguishes_missing_object() -> None:
    storage, client = _storage()
    key = f"delivery/1/source/{'a' * 64}.webm"

    assert storage.object_exists(key) is False
    client.objects[("audio-archive-delivery", key)] = {
        "ContentLength": 1,
        "Metadata": {"sha256": "a" * 64},
    }
    assert storage.object_exists(key) is True


@pytest.mark.parametrize(
    "filename",
    ["../escape.wav", "folder/file.wav", "folder\\file.wav", "bad\x00name.wav", ""],
)
def test_download_filename_rejects_path_or_control_input(filename: str) -> None:
    with pytest.raises(ValueError):
        validate_download_filename(filename)


def test_every_deliverable_role_can_be_published() -> None:
    """A role the worker can produce must be a role the storage layer accepts.

    These were separate lists, so publishing a 24-bit WAV failed after the audio had
    been acquired, converted and verified.
    """

    for role in sorted(DELIVERY_ROLES):
        key = object_key_for(job_id=7, role=role, sha256="a" * 64, suffix=".wav")
        assert key == f"delivery/7/{role}/{'a' * 64}.wav"


def test_every_output_a_job_can_request_is_a_deliverable_role() -> None:
    assert {output.value for output in CloudOutput} <= DELIVERY_ROLES
    assert "source" in DELIVERY_ROLES


def test_an_unknown_role_is_still_refused() -> None:
    with pytest.raises(ValueError, match="Unsupported delivery role"):
        object_key_for(job_id=7, role="flac", sha256="a" * 64)
