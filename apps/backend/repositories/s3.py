"""Injected S3 adapter for immutable content-addressed artifacts."""

import hashlib
from collections.abc import Mapping
from typing import Protocol

from apps.backend.repositories.ports import (
    ArtifactCollisionError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReference,
    ArtifactStoreError,
)


class S3Client(Protocol):
    """Minimum S3 client operations used by the adapter."""

    def put_object(self, **kwargs: object) -> object: ...

    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...


class S3ArtifactStore:
    """Store raw bytes under SHA-256 keys without allowing overwrites."""

    def __init__(self, client: S3Client, *, bucket_name: str, customer_id: str) -> None:
        if client is None:
            raise TypeError("client is required")
        _require_non_empty_string(bucket_name, "bucket_name")
        _require_non_empty_string(customer_id, "customer_id")
        self._client = client
        self._bucket_name = bucket_name
        self._customer_id = customer_id

    def put(self, content: bytes) -> ArtifactReference:
        """Conditionally create immutable bytes or confirm an identical object."""
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        reference = _reference_for(self._customer_id, content)
        try:
            self._client.put_object(
                Bucket=self._bucket_name,
                Key=_object_key(reference),
                Body=content,
                IfNoneMatch="*",
                Metadata={"sha256": reference.hex_digest},
            )
        except Exception as error:
            if _provider_error_code(error) in {"PreconditionFailed", "412"}:
                existing = self._read(reference)
                if existing == content:
                    return reference
                raise ArtifactCollisionError("artifact digest collision") from None
            raise ArtifactStoreError("artifact write failed") from None
        return reference

    def get(self, reference: ArtifactReference) -> bytes:
        """Load immutable bytes and verify their SHA-256 digest."""
        _require_reference(reference)
        content = self._read(reference)
        if _reference_for(reference.customer_id, content) != reference:
            raise ArtifactIntegrityError("artifact content digest mismatch")
        return content

    def _read(self, reference: ArtifactReference) -> bytes:
        try:
            response = self._client.get_object(
                Bucket=self._bucket_name,
                Key=_object_key(reference),
            )
            body = response["Body"]
            content = body.read()
            if not isinstance(content, bytes):
                raise TypeError
            return content
        except Exception as error:
            code = _provider_error_code(error)
            if code in {"NoSuchKey", "NotFound", "404"}:
                raise ArtifactNotFoundError("artifact not found") from None
            raise ArtifactStoreError("artifact read failed") from None


def _reference_for(customer_id: str, content: bytes) -> ArtifactReference:
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactReference(customer_id=customer_id, content_digest=f"sha256:{digest}")


def _object_key(reference: ArtifactReference) -> str:
    _require_reference(reference)
    return f"customers/{reference.customer_id}/artifacts/sha256/{reference.hex_digest}"


def _provider_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    detail = response.get("Error")
    if not isinstance(detail, Mapping):
        return None
    code = detail.get("Code")
    return code if isinstance(code, str) else None


def _require_reference(reference: object) -> None:
    if not isinstance(reference, ArtifactReference):
        raise TypeError("reference must be an ArtifactReference")


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
