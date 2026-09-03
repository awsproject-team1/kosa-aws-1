"""DynamoDB store for the bytes behind a `RemediationPatch` (content-addressed, immutable).

item은 고객 partition 아래 `REMEDIATION_PATCH#{content_sha256}`이다. digest가 key이므로 같은
patch의 재시도는 같은 item을 가리키고, 조건부 put이 다른 바이트의 덮어쓰기를 막는다. read는
바이트의 SHA-256을 patch의 `content_sha256`과 다시 대조한 뒤에만 내용을 돌려준다 — 저장 경로 밖에서
item이 바뀌었다면 PR에 올라가기 전에 여기서 걸린다.

S3가 아니라 DynamoDB인 이유: Worker runtime identity가 아직 tenant-scoped가 아니어서 Artifact
bucket 접근을 열지 않기로 했다(ADR-0014). patch 내용은 수 KB이고 상한(`MAX_PATCH_CONTENT_BYTES`)을
`patch_content` 모듈이 강제한다.
"""

from __future__ import annotations

from collections.abc import Mapping

from apps.backend.remediation.patch_content import (
    MAX_PATCH_CONTENT_BYTES,
    PatchContent,
    PatchContentError,
    patch_content_digest,
    verified_patch_content,
)
from apps.backend.repositories.dynamodb import DynamoTable
from apps.backend.repositories.errors import RepositoryError, StoredDataError
from packages.contracts import RemediationPatch

ENTITY_TYPE = "REMEDIATION_PATCH_CONTENT"


class DynamoDbPatchContentStore:
    """Persist and read back patch bytes under the patch's own digest."""

    def __init__(self, table: DynamoTable) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table

    def put(self, *, patch: RemediationPatch, content: bytes) -> None:
        if not isinstance(patch, RemediationPatch):
            raise TypeError("patch must be a RemediationPatch")
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        if len(content) > MAX_PATCH_CONTENT_BYTES:
            raise PatchContentError("patch content exceeds the stored size limit")
        digest = patch_content_digest(content)
        if digest != patch.artifact.content_sha256:
            raise PatchContentError("patch content does not match the patch digest")
        item = {
            "PK": f"CUSTOMER#{patch.artifact.customer_id}",
            "SK": _sort_key(digest),
            "entity_type": ENTITY_TYPE,
            "customer_id": patch.artifact.customer_id,
            "repository_id": patch.artifact.repository_id,
            "finding_id": patch.finding_id,
            "base_commit_sha": patch.base_commit_sha,
            "content_sha256": digest,
            "byte_size": len(content),
            # canonical JSON은 ASCII escape이므로 str로 저장해도 바이트가 보존된다.
            "content": content.decode("ascii"),
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except Exception as error:
            if _error_code(error) != "ConditionalCheckFailedException":
                raise RepositoryError("patch content write failed") from None
            # 같은 digest가 이미 있다. content-addressed이므로 같은 바이트여야 한다.
            existing = self._read(patch)
            if existing != content:
                raise StoredDataError("stored patch content differs for the same digest") from None

    def get(self, *, patch: RemediationPatch) -> PatchContent:
        if not isinstance(patch, RemediationPatch):
            raise TypeError("patch must be a RemediationPatch")
        content = self._read(patch)
        try:
            return verified_patch_content(patch=patch, content=content)
        except PatchContentError as error:
            raise StoredDataError(f"stored patch content is invalid: {error}") from error

    def _read(self, patch: RemediationPatch) -> bytes:
        try:
            response = self._table.get_item(
                Key={
                    "PK": f"CUSTOMER#{patch.artifact.customer_id}",
                    "SK": _sort_key(patch.artifact.content_sha256),
                },
                ConsistentRead=True,
            )
        except Exception:
            raise RepositoryError("patch content read failed") from None
        item = response.get("Item") if isinstance(response, Mapping) else None
        if not isinstance(item, Mapping) or item.get("entity_type") != ENTITY_TYPE:
            raise StoredDataError("patch content is not stored")
        if item.get("customer_id") != patch.artifact.customer_id:
            raise StoredDataError("stored patch content scope is invalid")
        content = item.get("content")
        if not isinstance(content, str) or not content:
            raise StoredDataError("stored patch content is empty")
        try:
            return content.encode("ascii")
        except UnicodeEncodeError:
            raise StoredDataError("stored patch content is not canonical") from None


def _sort_key(digest: str) -> str:
    return f"REMEDIATION_PATCH#{digest}"


def _error_code(error: BaseException) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    detail = response.get("Error")
    if not isinstance(detail, Mapping):
        return None
    code = detail.get("Code")
    return code if isinstance(code, str) else None
