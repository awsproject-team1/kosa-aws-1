"""D producer: M4 Golden observation bundle의 배포 결합 digest (ADR-0022 §4).

ADR-0021/0022는 실제 customer sandbox 실행 리포트를 release evidence로 요구하고, 그 실행이 C의
품질 리포트와 **같은 실행**임을 결합하는 키로 demo repository commit·deployment ID·artifact set의
SHA-256을 쓴다(ADR-0022 §3·§4). D는 그 세 digest를 만든다 — 원문(commit/ID/artifact 내용)은
bundle에 넣지 않고 digest만 결합한다.

C가 소비하는 `GoldenObservationBundle`(apps/backend/assessment/release_quality.py)의
`repository_commit_sha256`/`deployment_id_sha256`/`artifact_set_sha256` 필드가 이 producer의 출력이다.
세 값은 모두 소문자 SHA-256 hex 64자여야 C parser의 `_digest` 검증을 통과한다. 실제 commit/ID/
artifact 값은 sandbox 실행 시점에 주입되며, 이 모듈은 그 값들을 결정적 digest로 결합하는 순수
함수만 제공한다(자격 증명·네트워크 없음).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ReleaseBindingError(ValueError):
    """배포 결합 입력이 형식을 벗어났을 때 발생한다."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentReleaseBinding:
    """C observation bundle의 D 필드 세 개(모두 소문자 SHA-256 hex)."""

    repository_commit_sha256: str
    deployment_id_sha256: str
    artifact_set_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "repository_commit_sha256",
            "deployment_id_sha256",
            "artifact_set_sha256",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ReleaseBindingError(f"{name} must be a lowercase SHA-256")

    def to_dict(self) -> dict[str, str]:
        return {
            "repository_commit_sha256": self.repository_commit_sha256,
            "deployment_id_sha256": self.deployment_id_sha256,
            "artifact_set_sha256": self.artifact_set_sha256,
        }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def derive_repository_commit_digest(commit_sha: str) -> str:
    """40자 default-branch merge commit SHA를 검증한 뒤 그 문자열의 SHA-256을 낸다.

    bundle에는 commit 원문 대신 이 digest를 결합한다(ADR-0022 §3 — repository URL/commit 원문은
    공개 schema에 없다). commit이 lowercase 40자 hex가 아니면 fail-closed한다.
    """
    if not isinstance(commit_sha, str) or _COMMIT_SHA.fullmatch(commit_sha) is None:
        raise ReleaseBindingError("commit_sha must be a lowercase 40-character Git SHA")
    return _sha256(commit_sha)


def derive_deployment_id_digest(deployment_id: str) -> str:
    """Platform이 발급한 deployment ID 문자열의 SHA-256을 낸다."""
    if not isinstance(deployment_id, str) or not deployment_id.strip():
        raise ReleaseBindingError("deployment_id must be a non-empty string")
    return _sha256(deployment_id)


def derive_artifact_set_digest(artifact_sha256s: Sequence[str]) -> str:
    """apply가 소비한 artifact set의 결정적 결합 digest를 낸다.

    각 원소는 이미 artifact 내용의 SHA-256(예: plan/binary artifact의 `content_sha256`)이다. 순서에
    무관하고 중복을 흡수하도록 정렬된 고유 목록을 canonical JSON으로 직렬화한 뒤 다시 SHA-256을
    낸다. canonical 규칙(sort_keys·compact ASCII)은 C의 observation digest와 같은 원칙이라 두
    producer가 어긋나지 않는다. 빈 집합·형식 위반은 fail-closed한다.
    """
    if isinstance(artifact_sha256s, str) or not isinstance(artifact_sha256s, Sequence):
        raise ReleaseBindingError("artifact_sha256s must be a sequence of SHA-256 strings")
    unique: set[str] = set()
    for digest in artifact_sha256s:
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ReleaseBindingError("each artifact digest must be a lowercase SHA-256")
        unique.add(digest)
    if not unique:
        raise ReleaseBindingError("artifact_sha256s must not be empty")
    canonical = json.dumps(
        sorted(unique), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def derive_release_binding(
    *,
    commit_sha: str,
    deployment_id: str,
    artifact_sha256s: Sequence[str],
) -> DeploymentReleaseBinding:
    """세 배포 결합 digest를 한 번에 만든다(ADR-0022 §4, D producer 진입점).

    실제 값(merge commit, deployment ID, apply가 소비한 artifact digest 집합)은 sandbox 실행 시점에
    주입한다. 이 함수는 그 값들을 결정적 digest로 결합만 하며, 원문은 어디에도 보관하지 않는다.
    """
    return DeploymentReleaseBinding(
        repository_commit_sha256=derive_repository_commit_digest(commit_sha),
        deployment_id_sha256=derive_deployment_id_digest(deployment_id),
        artifact_set_sha256=derive_artifact_set_digest(artifact_sha256s),
    )
