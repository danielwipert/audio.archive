from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from ..verify import sha256_file
from .config import CloudSettings
from .models import DELIVERY_ROLES


@dataclass(frozen=True)
class PublishedObject:
    object_key: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str


class R2DeliveryStorage:
    def __init__(
        self,
        *,
        bucket: str,
        client: BaseClient,
        signed_url_ttl_seconds: int = 900,
    ) -> None:
        if not bucket.strip():
            raise ValueError("bucket is required")
        if not 60 <= signed_url_ttl_seconds <= 3600:
            raise ValueError("signed_url_ttl_seconds must be between 60 and 3600")
        self.bucket = bucket
        self.client = client
        self.signed_url_ttl_seconds = signed_url_ttl_seconds

    @classmethod
    def from_settings(cls, settings: CloudSettings) -> "R2DeliveryStorage":
        client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
        return cls(
            bucket=settings.r2_bucket,
            client=client,
            signed_url_ttl_seconds=settings.signed_url_ttl_seconds,
        )

    def publish_file(
        self,
        *,
        job_id: int,
        role: str,
        path: Path,
        filename: str,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> PublishedObject:
        if job_id <= 0:
            raise ValueError("job_id must be positive")
        if role not in DELIVERY_ROLES:
            raise ValueError(f"Unsupported delivery role: {role}")
        if not path.is_file():
            raise FileNotFoundError(path)
        safe_filename = validate_download_filename(filename)
        if not content_type.strip():
            raise ValueError("content_type is required")

        actual_sha256 = sha256_file(path)
        if expected_sha256 is not None and actual_sha256 != expected_sha256.lower():
            raise ValueError("Local file SHA-256 does not match the verified expected digest")

        size_bytes = path.stat().st_size
        suffix = _safe_suffix(path.suffix)
        object_key = object_key_for(job_id=job_id, role=role, sha256=actual_sha256, suffix=suffix)
        content_disposition = _content_disposition(safe_filename)

        self.client.upload_file(
            str(path),
            self.bucket,
            object_key,
            ExtraArgs={
                "ContentType": content_type,
                "ContentDisposition": content_disposition,
                "Metadata": {
                    "sha256": actual_sha256,
                    "role": role,
                },
            },
        )

        head = self.client.head_object(Bucket=self.bucket, Key=object_key)
        remote_size = int(head.get("ContentLength", -1))
        metadata = {str(k).lower(): str(v) for k, v in (head.get("Metadata") or {}).items()}
        if remote_size != size_bytes:
            self.delete_object(object_key)
            raise RuntimeError(
                f"Published object size mismatch: local={size_bytes} remote={remote_size}"
            )
        if metadata.get("sha256") != actual_sha256:
            self.delete_object(object_key)
            raise RuntimeError("Published object SHA-256 metadata did not round-trip")

        return PublishedObject(
            object_key=object_key,
            filename=safe_filename,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=actual_sha256,
        )

    def create_download_url(self, *, object_key: str, filename: str) -> str:
        validate_download_filename(filename)
        return str(
            self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": object_key},
                ExpiresIn=self.signed_url_ttl_seconds,
            )
        )

    def delete_object(self, object_key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=object_key)

    def object_exists(self, object_key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=object_key)
            return True
        except ClientError as exc:
            response = exc.response
            status = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(response.get("Error", {}).get("Code", ""))
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise


def object_key_for(*, job_id: int, role: str, sha256: str, suffix: str = "") -> str:
    if job_id <= 0:
        raise ValueError("job_id must be positive")
    if role not in DELIVERY_ROLES:
        raise ValueError(f"Unsupported delivery role: {role}")
    digest = sha256.lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("sha256 must be a 64-character hexadecimal digest")
    safe_suffix = _safe_suffix(suffix)
    return f"delivery/{job_id}/{role}/{digest}{safe_suffix}"


def validate_download_filename(filename: str) -> str:
    candidate = filename.strip()
    if not candidate:
        raise ValueError("filename is required")
    if candidate in {".", ".."}:
        raise ValueError("unsafe filename")
    if "/" in candidate or "\\" in candidate or "\x00" in candidate:
        raise ValueError("filename must not contain path separators or NUL")
    if any(ord(character) < 32 for character in candidate):
        raise ValueError("filename must not contain control characters")
    return candidate


def _safe_suffix(suffix: str) -> str:
    if not suffix:
        return ""
    candidate = suffix.lower()
    if not candidate.startswith("."):
        candidate = f".{candidate}"
    body = candidate[1:]
    if not body or len(body) > 12 or not body.isalnum():
        raise ValueError(f"Unsafe file suffix: {suffix}")
    return candidate


def _content_disposition(filename: str) -> str:
    # The fallback is deliberately generic ASCII. filename* carries the exact UTF-8 name.
    encoded = quote(filename, safe="")
    return f"attachment; filename=download; filename*=UTF-8''{encoded}"
