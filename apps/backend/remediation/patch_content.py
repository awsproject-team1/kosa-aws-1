"""The bytes behind a `RemediationPatch`: canonical encoding, digest, and the content store port.

`RemediationPatch`(packages/contracts)는 patch의 **identity**만 담는다 — finding, base commit,
content digest, changed paths. 변경된 파일의 **내용**은 담지 않는다. 그 내용이 어디에도 저장되지
않으면 PR write는 만들 것이 없고, digest는 아무것도 가리키지 않는 값이 된다.

여기서 그 내용의 정규 표현을 하나로 고정한다.

    {"base_commit_sha": "...", "changes": {"path": "full new contents", ...}, "finding_id": "..."}

key 정렬·구분자 고정·ASCII escape의 canonical JSON이며, `RemediationPatch.artifact.content_sha256`은
**정확히 이 바이트**의 SHA-256이다. 생성기와 PR writer가 같은 함수를 쓰므로 저장된 내용이 patch의
digest와 어긋나면 read 시점에 무결성 검사로 드러난다.

크기 상한은 DynamoDB item 한도(400KB) 아래에 둔다. 데모·MVP의 Terraform 변경은 수 KB이고, 상한을
넘는 변경은 "최소 변경"이라는 remediation 정의에도 맞지 않으므로 fail-closed한다. S3 artifact로
옮기는 것은 tenant-scoped runtime identity(ADR-0014)가 검토된 뒤의 일이다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from packages.contracts import RemediationPatch

#: DynamoDB item 한도 400KB 아래의 여유 있는 상한. 초과는 오류다.
MAX_PATCH_CONTENT_BYTES = 300_000


class PatchContentError(ValueError):
    """The patch content is malformed, oversized, or does not match its patch identity."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PatchContent:
    """Decoded patch bytes: which files change and their complete new contents."""

    finding_id: str
    base_commit_sha: str
    changes: Mapping[str, str]

    def __post_init__(self) -> None:
        for name in ("finding_id", "base_commit_sha"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PatchContentError(f"{name} must be a non-empty string")
        if not isinstance(self.changes, Mapping) or not self.changes:
            raise PatchContentError("changes must be a non-empty mapping")
        for path, contents in self.changes.items():
            if not isinstance(path, str) or not path.strip():
                raise PatchContentError("change path must be a non-empty string")
            if path.startswith("/") or ".." in path.split("/"):
                raise PatchContentError("change path must be repository-relative")
            if not isinstance(contents, str) or not contents:
                raise PatchContentError("change contents must be a non-empty string")

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.changes))


def encode_patch_content(
    *, finding_id: str, base_commit_sha: str, changes: Mapping[str, str]
) -> bytes:
    """Return the canonical bytes whose SHA-256 is the patch's `content_sha256`."""
    content = PatchContent(
        finding_id=finding_id, base_commit_sha=base_commit_sha, changes=dict(changes)
    )
    encoded = json.dumps(
        {
            "finding_id": content.finding_id,
            "base_commit_sha": content.base_commit_sha,
            "changes": {path: content.changes[path] for path in content.changed_paths},
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_PATCH_CONTENT_BYTES:
        raise PatchContentError("patch content exceeds the stored size limit")
    return encoded


def decode_patch_content(content: bytes) -> PatchContent:
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    if len(content) > MAX_PATCH_CONTENT_BYTES:
        raise PatchContentError("patch content exceeds the stored size limit")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PatchContentError("patch content is not canonical JSON") from error
    if not isinstance(value, Mapping) or set(value) != {"finding_id", "base_commit_sha", "changes"}:
        raise PatchContentError("patch content fields are invalid")
    changes = value["changes"]
    if not isinstance(changes, Mapping):
        raise PatchContentError("patch content changes must be an object")
    decoded = PatchContent(
        finding_id=value["finding_id"],
        base_commit_sha=value["base_commit_sha"],
        changes=dict(changes),
    )
    # 정규형이어야 digest가 재현된다. 다시 encode해 같은 바이트가 나오지 않으면 저장 경로 밖에서
    # 만들어진 값이다.
    if (
        encode_patch_content(
            finding_id=decoded.finding_id,
            base_commit_sha=decoded.base_commit_sha,
            changes=decoded.changes,
        )
        != content
    ):
        raise PatchContentError("patch content is not in canonical form")
    return decoded


def patch_content_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def require_content_matches_patch(content: PatchContent, patch: RemediationPatch) -> None:
    """Refuse content that describes a different finding, commit, or file set than the patch."""
    if not isinstance(patch, RemediationPatch):
        raise TypeError("patch must be a RemediationPatch")
    if content.finding_id != patch.finding_id or content.base_commit_sha != patch.base_commit_sha:
        raise PatchContentError("patch content is bound to a different finding or commit")
    if content.changed_paths != tuple(sorted(patch.changed_paths)):
        raise PatchContentError("patch content changes different paths than the patch declares")


class PatchContentStore(Protocol):
    """Content-addressed storage of patch bytes, keyed by the patch's digest."""

    def put(self, *, patch: RemediationPatch, content: bytes) -> None:
        """Store the bytes once; an identical retry is absorbed, a different body is refused."""
        ...

    def get(self, *, patch: RemediationPatch) -> PatchContent:
        """Load and verify the bytes the patch's `content_sha256` names."""
        ...


class InMemoryPatchContentStore:
    """Deterministic in-memory store for tests and fixture-backed development."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], bytes] = {}

    def put(self, *, patch: RemediationPatch, content: bytes) -> None:
        _require_digest(patch, content)
        key = (patch.artifact.customer_id, patch.artifact.content_sha256)
        existing = self._items.get(key)
        if existing is not None and existing != content:
            raise PatchContentError("patch content digest collision")
        self._items[key] = content

    def get(self, *, patch: RemediationPatch) -> PatchContent:
        if not isinstance(patch, RemediationPatch):
            raise TypeError("patch must be a RemediationPatch")
        try:
            content = self._items[(patch.artifact.customer_id, patch.artifact.content_sha256)]
        except KeyError:
            raise PatchContentError("patch content is not stored") from None
        return verified_patch_content(patch=patch, content=content)


def verified_patch_content(*, patch: RemediationPatch, content: bytes) -> PatchContent:
    """Decode stored bytes only after they prove to be the patch's own content."""
    _require_digest(patch, content)
    decoded = decode_patch_content(content)
    require_content_matches_patch(decoded, patch)
    return decoded


def _require_digest(patch: RemediationPatch, content: bytes) -> None:
    if not isinstance(patch, RemediationPatch):
        raise TypeError("patch must be a RemediationPatch")
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    if patch_content_digest(content) != patch.artifact.content_sha256:
        raise PatchContentError("patch content does not match the patch digest")
