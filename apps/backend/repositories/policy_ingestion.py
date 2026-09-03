"""A-owned tenant-scoped Policy Source upload-session persistence."""

from collections.abc import Mapping
from hashlib import sha256
from typing import Protocol

from apps.backend.api.policy_sources import PolicySourceUploadSession
from apps.backend.policy.ingestion.pipeline import UploadedPolicyOriginal
from apps.backend.policy.ingestion.storage_keys import (
    normalized_object_key,
    original_object_key,
)
from packages.contracts import (
    DocumentUnitKind,
    ExtractionWarningCode,
    IngestionFailureCode,
    IngestionStatus,
    NormalizedDocumentUnit,
    NormalizedPolicyDocument,
    PolicySourceFormat,
    PolicySourceUploadRequest,
)


class DynamoTable(Protocol):
    def put_item(self, **kwargs: object) -> object: ...

    def update_item(self, **kwargs: object) -> object: ...

    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...


class S3Presigner(Protocol):
    def generate_presigned_url(
        self, ClientMethod: str, Params: dict[str, str], ExpiresIn: int
    ) -> str: ...


class S3ObjectReader(Protocol):
    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]: ...


class S3ObjectWriter(Protocol):
    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> object: ...


class DynamoDbPolicySourceUploadRepository:
    def __init__(self, *, table: DynamoTable, bucket: str, presigner: S3Presigner) -> None:
        if table is None or presigner is None:
            raise TypeError("table and presigner are required")
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("bucket must be a non-empty string")
        self._table, self._bucket, self._presigner = table, bucket, presigner

    def create_upload_session(
        self,
        *,
        customer_id: str,
        request: PolicySourceUploadRequest,
        source_id: str,
        source_version: str,
    ) -> PolicySourceUploadSession:
        key = original_object_key(
            customer_id=customer_id, source_id=source_id, source_version=source_version
        )
        item = {
            "PK": f"CUSTOMER#{customer_id}",
            "SK": f"POLICY_INGESTION#{source_id}#VERSION#{source_version}",
            "entity_type": "POLICY_INGESTION",
            "customer_id": customer_id,
            "source_id": source_id,
            "source_version": source_version,
            "filename": request.filename,
            "declared_media_type": request.declared_media_type,
            "byte_size": request.byte_size,
            "status": "UPLOAD_PENDING",
            "artifact_id": f"policy-original-{source_id}-{source_version}",
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except Exception as error:
            raise RuntimeError("policy upload session persistence failed") from error
        url = self._presigner.generate_presigned_url(
            "put_object",
            {"Bucket": self._bucket, "Key": key, "ContentType": request.declared_media_type},
            900,
        )
        return PolicySourceUploadSession(
            source_id=source_id, source_version=source_version, upload_url=url
        )

    def finalize_upload(
        self,
        *,
        customer_id: str,
        source_id: str,
        source_version: str,
        reader: object,
    ) -> tuple[UploadedPolicyOriginal, bytes]:
        """Read exact server-derived S3 bytes; clients never choose object identity."""
        key = original_object_key(
            customer_id=customer_id, source_id=source_id, source_version=source_version
        )
        if not hasattr(reader, "get_object"):
            raise TypeError("reader must provide get_object")
        item = self._get_item(customer_id, source_id, source_version)
        if item.get("status") != "UPLOAD_PENDING":
            raise RuntimeError("policy upload is not pending finalization")
        filename = item.get("filename")
        declared_media_type = item.get("declared_media_type")
        declared_size = item.get("byte_size")
        artifact_id = item.get("artifact_id")
        if (
            not isinstance(filename, str)
            or not isinstance(declared_media_type, str)
            or isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or not isinstance(artifact_id, str)
        ):
            raise RuntimeError("policy upload metadata is invalid")
        response = reader.get_object(Bucket=self._bucket, Key=key)  # type: ignore[union-attr]
        version_id, body = response.get("VersionId"), response.get("Body")
        payload = body.read() if hasattr(body, "read") else None
        if (
            not isinstance(version_id, str)
            or not version_id
            or not isinstance(payload, bytes)
            or len(payload) != declared_size
            or response.get("ContentType") != declared_media_type
        ):
            raise RuntimeError("uploaded policy object is invalid")
        original = UploadedPolicyOriginal(
            source_id=source_id,
            source_version=source_version,
            artifact_id=artifact_id,
            s3_version_id=version_id,
            content_sha256=sha256(payload).hexdigest(),
            filename=filename,
            declared_media_type=declared_media_type,
            byte_size=len(payload),
        )
        try:
            self._table.update_item(
                Key={"PK": f"CUSTOMER#{customer_id}", "SK": item["SK"]},
                UpdateExpression=(
                    "SET #status = :uploaded, s3_version_id = :version, content_sha256 = :digest"
                ),
                ConditionExpression="customer_id = :customer AND #status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":customer": customer_id,
                    ":pending": "UPLOAD_PENDING",
                    ":uploaded": IngestionStatus.UPLOADED.value,
                    ":version": version_id,
                    ":digest": original.content_sha256,
                },
            )
        except Exception as error:
            raise RuntimeError("policy upload finalization state write failed") from error
        return original, payload

    def record_normalization(
        self,
        *,
        customer_id: str,
        document: NormalizedPolicyDocument,
        normalized_payload: bytes | None,
    ) -> None:
        """Persist Contract metadata only; original and normalized text stay out of DynamoDB."""
        if not isinstance(document, NormalizedPolicyDocument):
            raise TypeError("document must be a NormalizedPolicyDocument")
        item = self._get_item(customer_id, document.source_id, document.source_version)
        if item.get("status") != IngestionStatus.UPLOADED.value:
            raise RuntimeError("policy upload is not ready for normalization")
        if (
            item.get("artifact_id"),
            item.get("s3_version_id"),
            item.get("content_sha256"),
        ) != (document.artifact_id, document.s3_version_id, document.content_sha256):
            raise RuntimeError("policy normalization original binding mismatch")
        if document.status is IngestionStatus.FAILED:
            if normalized_payload is not None:
                raise ValueError("a failed normalization must not persist artifact bytes")
        else:
            if not isinstance(normalized_payload, bytes) or not normalized_payload:
                raise ValueError("a successful normalization must persist artifact bytes")
            if sha256(normalized_payload).hexdigest() != document.normalized_sha256:
                raise RuntimeError("normalized payload digest does not match document")
            writer = self._presigner
            if not hasattr(writer, "put_object"):
                raise TypeError(
                    "presigner must also provide put_object to persist normalized artifacts"
                )
            writer.put_object(  # type: ignore[union-attr]
                Bucket=self._bucket,
                Key=normalized_object_key(
                    customer_id=customer_id,
                    source_id=document.source_id,
                    source_version=document.source_version,
                ),
                Body=normalized_payload,
                ContentType="application/json",
            )
        values = document.to_dict()
        try:
            self._table.update_item(
                Key={
                    "PK": f"CUSTOMER#{customer_id}",
                    "SK": f"POLICY_INGESTION#{document.source_id}#VERSION#{document.source_version}",
                },
                UpdateExpression=(
                    "SET #status = :status, detected_media_type = :detected, source_format = :format, "
                    "parser_id = :parser_id, parser_version = :parser_version, "
                    "normalized_artifact_id = :artifact, normalized_sha256 = :digest, "
                    "units = :units, warnings = :warnings, failure_code = :failure"
                ),
                ConditionExpression=(
                    "customer_id = :customer AND #status = :uploaded AND artifact_id = :original "
                    "AND s3_version_id = :version AND content_sha256 = :content_digest"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":customer": customer_id,
                    ":uploaded": IngestionStatus.UPLOADED.value,
                    ":status": values["status"],
                    ":detected": values["detected_media_type"],
                    ":format": values["source_format"],
                    ":parser_id": values["parser_id"],
                    ":parser_version": values["parser_version"],
                    ":artifact": values["normalized_artifact_id"],
                    ":digest": values["normalized_sha256"],
                    ":units": values["units"],
                    ":warnings": values["warnings"],
                    ":failure": values["failure_code"],
                    ":original": document.artifact_id,
                    ":version": document.s3_version_id,
                    ":content_digest": document.content_sha256,
                },
            )
        except Exception as error:
            raise RuntimeError("policy normalization state write failed") from error

    def get_document(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> NormalizedPolicyDocument:
        item = self._get_item(customer_id, source_id, source_version)
        return document_from_item(item)

    def _get_item(
        self, customer_id: str, source_id: str, source_version: str
    ) -> Mapping[str, object]:
        try:
            item = self._table.get_item(
                Key={
                    "PK": f"CUSTOMER#{customer_id}",
                    "SK": f"POLICY_INGESTION#{source_id}#VERSION#{source_version}",
                },
                ConsistentRead=True,
            ).get("Item")
        except Exception as error:
            raise RuntimeError("policy ingestion state read failed") from error
        if not isinstance(item, Mapping) or item.get("customer_id") != customer_id:
            raise LookupError("policy source version not found")
        return item


def document_from_item(item: Mapping[str, object]) -> NormalizedPolicyDocument:
    """`POLICY_INGESTION` item을 `NormalizedPolicyDocument`로 재구성한다.

    수집 상태 조회(`get_document`)와 승인 read 경로(`load_review`)가 같은 재구성을 쓰도록
    공용 함수로 둔다. item 형태가 어긋나면 예외 대신 `RuntimeError`로 감싼다.
    """
    try:
        return NormalizedPolicyDocument(
            source_id=_string(item, "source_id"),
            source_version=_string(item, "source_version"),
            artifact_id=_string(item, "artifact_id"),
            s3_version_id=_string(item, "s3_version_id"),
            content_sha256=_string(item, "content_sha256"),
            filename=_string(item, "filename"),
            declared_media_type=_string(item, "declared_media_type"),
            byte_size=_integer(item, "byte_size"),
            status=IngestionStatus(_string(item, "status")),
            detected_media_type=_optional_string(item, "detected_media_type"),
            source_format=_optional_enum(item, "source_format", PolicySourceFormat),
            parser_id=_optional_string(item, "parser_id"),
            parser_version=_optional_string(item, "parser_version"),
            normalized_artifact_id=_optional_string(item, "normalized_artifact_id"),
            normalized_sha256=_optional_string(item, "normalized_sha256"),
            units=tuple(
                NormalizedDocumentUnit(
                    locator=_string(unit, "locator"),
                    kind=DocumentUnitKind(_string(unit, "kind")),
                    text_sha256=_string(unit, "text_sha256"),
                    text_length=_integer(unit, "text_length"),
                    origin=_string(unit, "origin"),
                )
                for unit in _mappings(item, "units")
            ),
            warnings=tuple(ExtractionWarningCode(value) for value in _strings(item, "warnings")),
            failure_code=_optional_enum(item, "failure_code", IngestionFailureCode),
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError("policy ingestion record is invalid") from error


def _string(item: Mapping[str, object], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    return value


def _optional_string(item: Mapping[str, object], name: str) -> str | None:
    value = item.get(name)
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{name} is invalid")
    return value


def _integer(item: Mapping[str, object], name: str) -> int:
    value = item.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} is invalid")
    return value


def _strings(item: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = item.get(name, [])
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise ValueError(f"{name} is invalid")
    return tuple(value)


def _mappings(item: Mapping[str, object], name: str) -> tuple[Mapping[str, object], ...]:
    value = item.get(name, [])
    if not isinstance(value, list) or not all(isinstance(entry, Mapping) for entry in value):
        raise ValueError(f"{name} is invalid")
    return tuple(value)


def _optional_enum(item: Mapping[str, object], name: str, enum_type: type) -> object | None:
    value = _optional_string(item, name)
    return None if value is None else enum_type(value)
