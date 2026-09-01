"""승인된 IaC Snapshot에 바인딩되는 결정적 Remediation Patch 생성 경계.

`RemediationService`(service.py)는 생성된 patch가 요청한 finding·snapshot에 제대로
묶여 있는지 *검증*만 한다. 실제로 patch를 만들어내는 주체는 `PatchGenerator`이며, 이
모듈은 그 결정적 구현체를 제공한다.

이 경계는 어떤 실제 write도 하지 않는다. 만들어지는 `RemediationPatch`는 "제안"일 뿐이며,
Branch/Commit/PR write와 Terraform Plan/Apply는 이후 Task(M2 task7/task8, M3) 범위다.
실제 AI/LLM 기반 Terraform 생성 역시 Integrated 단계에서 이 generator 뒤 어댑터로 교체된다.
지금은 Fixture로 변경 내용을 주입받아 경계와 scope 규칙만 결정적으로 고정한다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    IaCSnapshot,
    RemediationDecision,
    RemediationPatch,
)


class FixturePatchGenerator:
    """Fixture로 주입된 변경 계획을 snapshot에 바인딩된 patch로 만드는 결정적 generator.

    `PatchGenerator` Protocol(service.py)을 만족한다. finding_id별로 "어떤 파일을 어떻게
    바꿀지"를 미리 주입받고, 요청이 오면 판정(`RemediationDecision`)의 finding_id에 해당하는
    계획을 `IaCSnapshot`의 좌표(commit/customer/repository)에 결정적으로 묶어
    `RemediationPatch`를 만든다. 실제 코드 생성을 하지 않으므로, 같은 입력은 항상 같은 patch를
    만든다. 판정의 action 게이트(TERRAFORM_PATCH만 허용)는 `RemediationService`가 강제한다.
    """

    def __init__(self, plans: Mapping[str, tuple[str, ...]]) -> None:
        """finding_id → 변경 대상 repository-relative 경로 튜플의 매핑을 받는다.

        경로 유효성(빈 경로, 절대경로, `..` 포함 거부)은 `RemediationPatch` Contract가
        생성 시점에 강제하므로 여기서 중복 검증하지 않는다. 다만 계획이 비어 있으면
        만들 patch가 없으므로 매핑 형태만 확인한다.
        """
        if not isinstance(plans, Mapping) or not plans:
            raise ValueError("plans must be a non-empty mapping of finding_id to paths")
        normalized: dict[str, tuple[str, ...]] = {}
        for finding_id, paths in plans.items():
            if not isinstance(finding_id, str) or not finding_id.strip():
                raise ValueError("plan finding_id must be a non-empty string")
            if not isinstance(paths, tuple) or not paths:
                raise ValueError("plan paths must be a non-empty tuple")
            # changed_paths의 순서는 "어떤 파일이 바뀌는가"라는 집합 의미만 가지며 순서 자체는
            # 무의미하다. 여기서 한 번 정렬해 두면 반환되는 patch.changed_paths와 digest 계산이
            # 항상 같은 순서를 보므로, 입력 순서만 다른 계획이 같은 patch로 정규화된다.
            normalized[finding_id] = tuple(sorted(paths))
        self._plans = normalized

    def generate(self, *, decision: RemediationDecision, snapshot: IaCSnapshot) -> RemediationPatch:
        """판정에 해당하는 변경 계획을 snapshot에 바인딩해 patch를 만든다.

        finding_id는 판정에서 꺼낸다(ADR-0018 D3). 별도 인자로 받으면 판정과 대상이 어긋난
        호출이 가능해지기 때문이다. 계획이 없는 finding_id는 만들 patch가 없다는 뜻이므로
        `KeyError`가 아니라 의미가 분명한 `ValueError`로 거부한다. snapshot 좌표는 그대로
        patch artifact에 반영되어, `RemediationService`의 scope·commit 바인딩 검증을 통과한다.

        action 게이트(TERRAFORM_PATCH만 허용)는 `RemediationService`가 강제하므로 여기서는
        중복 검사하지 않는다. 다만 타입은 최소한으로 확인한다.
        """
        if not isinstance(decision, RemediationDecision):
            raise TypeError("decision must be a RemediationDecision")
        if not isinstance(snapshot, IaCSnapshot):
            raise TypeError("snapshot must be an IaCSnapshot")
        finding_id = decision.finding_id
        try:
            changed_paths = self._plans[finding_id]
        except KeyError:
            raise ValueError(f"no remediation plan is registered for {finding_id!r}") from None

        # patch 내용 digest는 (finding_id, snapshot commit, 변경 경로)로부터 결정적으로
        # 만든다. 같은 입력 → 같은 digest이므로 재실행이 동일한 patch를 낸다.
        digest = _content_digest(
            finding_id=finding_id,
            commit_sha=snapshot.commit_sha,
            changed_paths=changed_paths,
        )
        return RemediationPatch(
            finding_id=finding_id,
            base_commit_sha=snapshot.commit_sha,
            artifact=ArtifactReference(
                # artifact_id는 immutable artifact의 identity이므로 "같은 ID = 같은 내용"이
                # 보장돼야 한다. 내용을 유일하게 규정하는 값은 content digest이며, digest는
                # (finding_id, commit_sha, changed_paths)를 모두 담는다. 따라서 digest를
                # identity에 포함하면, 같은 repository/finding/commit이라도 계획(changed_paths)이
                # 달라지면 ID도 달라진다. commit만 넣던 이전 방식은 같은 commit에서 계획이 바뀌면
                # 같은 ID가 다른 내용을 가리켜 저장 계층에서 충돌·오조회를 냈다. finding_id는
                # 사람이 로그에서 식별하기 쉽도록 접두로 남긴다(정확성은 digest가 보장).
                artifact_id=f"remediation-patch:{snapshot.repository_id}:{finding_id}:{digest}",
                artifact_type=ArtifactType.REMEDIATION_PATCH,
                content_sha256=digest,
                customer_id=snapshot.customer_id,
                repository_id=snapshot.repository_id,
            ),
            changed_paths=changed_paths,
        )


def _content_digest(*, finding_id: str, commit_sha: str, changed_paths: tuple[str, ...]) -> str:
    """patch 식별을 위한 결정적 SHA-256 digest.

    실제 Terraform 본문 대신 patch를 유일하게 규정하는 좌표를 정규화해 해싱한다. 정렬과
    구분자를 고정해 플랫폼·실행 순서와 무관하게 같은 입력이 같은 값을 내도록 한다.
    """
    payload = json.dumps(
        {
            "finding_id": finding_id,
            "base_commit_sha": commit_sha,
            "changed_paths": sorted(changed_paths),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
