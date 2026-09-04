"""One pure function from an approved document to a reviewable authoring result.

    Artifact 검증
    → Extract
    → Locator 검증
    → 분류 불변식
    → Control/resource/evaluation type 검증
    → Evidence capability 검증
    → Rule build
    → 최종 검증

**영속화하지 않는다.** 저장은 `record_authoring_result()`가 하고, 이 함수는 같은 입력에 같은
출력을 낸다. 순수하게 유지하는 이유는 재시도다 — worker가 같은 요청을 다시 처리했을 때 같은
결과가 나와야 저장 계층이 "같은 추출의 재시도"와 "다른 추출"을 구별할 수 있다.

`PolicyCandidateExtraction.candidates`에 들어가는 것은 AUTOMATABLE과 MANUAL Rule뿐이다.
UNSUPPORTED와 rejected는 보존되지만 승인 가능한 Rule로 변환되지 않는다.
"""

from __future__ import annotations

from apps.backend.policy.authoring.artifact_reader import NormalizedArtifactReader
from apps.backend.policy.authoring.extractor import PolicyCandidateExtractor
from apps.backend.policy.authoring.rule_builder import build_candidate
from packages.contracts import (
    AcceptedRequirement,
    CandidateClassification,
    ExtractedRequirement,
    GovernanceControlCatalog,
    NormalizedPolicyDocument,
    PolicyAuthoringResult,
    RejectedRequirement,
)


class DuplicateRequirementError(ValueError):
    """Raised when one extraction yields two Requirements with the same identity.

    같은 Requirement가 두 번 나오면 같은 Rule ID가 두 번 만들어진다. 조용히 하나를 버리면
    어느 쪽을 버렸는지 아무 데도 남지 않으므로 추출 전체를 실패시킨다.
    """


def extract_policy_candidates(
    *,
    customer_id: str,
    document: NormalizedPolicyDocument,
    artifact_reader: NormalizedArtifactReader,
    extractor: PolicyCandidateExtractor,
    catalog: GovernanceControlCatalog,
    authoring_run_id: str,
    requested_at: str,
) -> PolicyAuthoringResult:
    """Run one authoring pass. Raises only when the run itself cannot be trusted."""
    if not isinstance(document, NormalizedPolicyDocument):
        raise TypeError("document must be a NormalizedPolicyDocument")
    if not isinstance(catalog, GovernanceControlCatalog):
        raise TypeError("catalog must be a GovernanceControlCatalog")

    # Artifact 검증 실패는 `ArtifactReadError`로 그대로 올라간다 — 후보 하나의 문제가 아니라
    # 어떤 문서를 읽고 있는지 말할 수 없는 상태이므로 추출 전체를 중단한다.
    units = artifact_reader.read(customer_id=customer_id, document=document)
    extraction = extractor.extract(document=document, units=units, catalog=catalog)
    requirements = extraction.requirements
    _require_unique(requirements)

    accepted: list[AcceptedRequirement] = []
    manual: list[AcceptedRequirement] = []
    unsupported: list[ExtractedRequirement] = []
    rejected: list[RejectedRequirement] = []

    for requirement in requirements:
        if not isinstance(requirement, ExtractedRequirement):
            raise TypeError("extractor must return ExtractedRequirement values")
        if requirement.classification is CandidateClassification.UNSUPPORTED:
            unsupported.append(requirement)
            continue
        outcome = build_candidate(requirement=requirement, document=document, catalog=catalog)
        if isinstance(outcome, RejectedRequirement):
            # AUTOMATABLE이 실패해도 MANUAL로 바꾸지 않는다. 검증 실패로부터 승인 가능한 Rule을
            # 만들어내는 일이기 때문이다.
            rejected.append(outcome)
        elif requirement.classification is CandidateClassification.MANUAL:
            manual.append(outcome)
        else:
            accepted.append(outcome)

    return PolicyAuthoringResult(
        document=document,
        # 분류하지 못한 unit은 결과에 남는다. 조용한 유실이 아니라 보이는 미완료다.
        unclassified=extraction.unclassified,
        accepted=tuple(accepted),
        manual=tuple(manual),
        unsupported=tuple(unsupported),
        rejected=tuple(rejected),
        provenance=extractor.identity.provenance(
            catalog=catalog,
            authoring_run_id=authoring_run_id,
            requested_at=requested_at,
        ),
    )


def _require_unique(requirements: tuple[ExtractedRequirement, ...]) -> None:
    seen: set[str] = set()
    for requirement in requirements:
        if requirement.digest in seen:
            raise DuplicateRequirementError(
                "one extraction must not repeat the same requirement identity"
            )
        seen.add(requirement.digest)
