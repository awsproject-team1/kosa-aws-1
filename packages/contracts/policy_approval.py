"""Human approval and Policy Profile publication contracts.

`docs/POLICY_INGESTION.md`의 "Required public/API boundary" 4번과 5번이다. **승인과 게시는 서로
다른 operation이다.** 승인은 Source/Control/Rule version을 확정할 뿐이고, 그 Rule들을 실제 평가
경계로 만드는 것은 Profile publication이다. 하나의 operation으로 합치더라도 거부 조건은 같다.

Task 2의 규율을 계승한다: 거부 사유는 자유 문장이 아니라 열거값이고, 어떤 값도 정책 원문이나
추출 텍스트를 담지 않는다.
"""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import require_non_empty_string
from packages.contracts.policy import PolicyRule, PolicyRuleReference
from packages.contracts.policy_ingestion import NormalizedPolicyDocument


class RuleLifecycle(StrEnum):
    """Where one Rule version sits between extraction and evaluation.

    `docs/DATABASE.md`의 Rule metadata 항목(`RULE#{rule_id}#VERSION#{version}`)이 담기로 한
    lifecycle이다. `APPROVED`만 Profile이 참조할 수 있다.
    """

    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class ApprovalRejectionCode(StrEnum):
    """Why an approval or a publication was refused.

    자유 문장이 아니라 열거값이다. 거부 사유가 원문 문장이나 locator 내용을 인용해 로그로 새는
    경로를 막는다.
    """

    SOURCE_NOT_APPROVABLE = "SOURCE_NOT_APPROVABLE"
    RULE_NOT_APPROVABLE = "RULE_NOT_APPROVABLE"
    SOURCE_VERSION_MISMATCH = "SOURCE_VERSION_MISMATCH"
    ORIGINAL_BINDING_MISMATCH = "ORIGINAL_BINDING_MISMATCH"
    UNKNOWN_LOCATOR = "UNKNOWN_LOCATOR"
    CONTENT_DIGEST_MISMATCH = "CONTENT_DIGEST_MISMATCH"
    RULE_NOT_APPROVED = "RULE_NOT_APPROVED"
    SOURCE_NOT_APPROVED = "SOURCE_NOT_APPROVED"
    DUPLICATE_RULE_REFERENCE = "DUPLICATE_RULE_REFERENCE"
    EMPTY_PROFILE = "EMPTY_PROFILE"


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleCandidate:
    """One proposed Rule and where in an approved source version it came from.

    후보는 사람이 승인하기 전의 상태다. 자동 생성됐든 손으로 썼든, 승인 전에는 Profile이나
    Assessment에 들어갈 수 없다 (`docs/POLICY_INGESTION.md` Security and tenant isolation).
    """

    rule: PolicyRule
    lifecycle: RuleLifecycle = RuleLifecycle.CANDIDATE

    def __post_init__(self) -> None:
        if not isinstance(self.rule, PolicyRule):
            raise TypeError("rule must be a PolicyRule")
        if not isinstance(self.lifecycle, RuleLifecycle):
            raise TypeError("lifecycle must be a RuleLifecycle")

    @property
    def reference(self) -> PolicyRuleReference:
        return PolicyRuleReference(rule_id=self.rule.rule_id, version=self.rule.version)

    @property
    def is_approved(self) -> bool:
        return self.lifecycle is RuleLifecycle.APPROVED

    def approved(self) -> "RuleCandidate":
        """Return the same Rule marked approved. 후보 자체는 변경하지 않는다."""
        return RuleCandidate(rule=self.rule, lifecycle=RuleLifecycle.APPROVED)

    def rejected(self) -> "RuleCandidate":
        return RuleCandidate(rule=self.rule, lifecycle=RuleLifecycle.REJECTED)

    def to_dict(self) -> dict[str, object]:
        return {"rule": self.rule.to_dict(), "lifecycle": self.lifecycle.value}


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyCandidateExtraction:
    """C's immutable handoff for one exact normalized policy source version.

    The result carries document metadata and Rule candidates, but never
    normalized document text. C reads text only from the protected normalized
    Artifact; A persists this output for approval/publication read paths.
    """

    document: NormalizedPolicyDocument
    candidates: tuple[RuleCandidate, ...]
    extractor_id: str
    extractor_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.document, NormalizedPolicyDocument):
            raise TypeError("document must be a NormalizedPolicyDocument")
        if not self.document.is_approvable:
            raise ValueError("candidate extraction requires a READY document")
        for name in ("extractor_id", "extractor_version"):
            require_non_empty_string(getattr(self, name), name)

        seen: set[tuple[str, str]] = set()
        for candidate in self.candidates:
            if not isinstance(candidate, RuleCandidate):
                raise TypeError("candidates must contain RuleCandidate values")
            if candidate.lifecycle is not RuleLifecycle.CANDIDATE:
                raise ValueError("candidate extraction must contain undecided candidates")
            key = (candidate.rule.rule_id, candidate.rule.version)
            if key in seen:
                raise ValueError("candidate extraction must not duplicate a rule version")
            seen.add(key)
            self._require_document_provenance(candidate.rule)

    def _require_document_provenance(self, rule: PolicyRule) -> None:
        for reference in rule.source_references:
            if (reference.source_id, reference.source_version) != (
                self.document.source_id,
                self.document.source_version,
            ):
                raise ValueError("candidate source reference must match the extracted document")
            unit = self.document.unit(reference.locator)
            if unit is None:
                raise ValueError(
                    "candidate source reference locator is not in the extracted document"
                )
            if unit.text_sha256 != reference.content_sha256:
                raise ValueError(
                    "candidate source reference digest does not match the extracted document"
                )

    def to_dict(self) -> dict[str, object]:
        """Return the A persistence handoff without protected artifact bytes."""
        return {
            "document": self.document.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicySourceApproval:
    """An immutable human approval bound to one exact uploaded original.

    `docs/POLICY_INGESTION.md` Original finalization: 승인 record는
    `(source_id, source_version, artifact_id, s3_version_id, content_sha256)`을 그대로 인용하며,
    다른 판본으로 승인을 옮겨 붙일 수 없다. Profile publication이 이 tuple을 다시 대조한다.
    """

    source_id: str
    source_version: str
    artifact_id: str
    s3_version_id: str
    content_sha256: str
    normalized_artifact_id: str
    normalized_sha256: str
    approved_rules: tuple[PolicyRuleReference, ...]
    approved_by: str
    approved_at: str

    def __post_init__(self) -> None:
        for name in (
            "source_id",
            "source_version",
            "artifact_id",
            "s3_version_id",
            "content_sha256",
            "normalized_artifact_id",
            "normalized_sha256",
            "approved_by",
            "approved_at",
        ):
            require_non_empty_string(getattr(self, name), name)
        if not self.approved_rules:
            raise ValueError("approved_rules must not be empty")
        seen: set[tuple[str, str]] = set()
        for reference in self.approved_rules:
            if not isinstance(reference, PolicyRuleReference):
                raise TypeError("approved_rules items must be PolicyRuleReference values")
            key = (reference.rule_id, reference.version)
            if key in seen:
                raise ValueError(f"duplicate approved rule {reference.rule_id}@{reference.version}")
            seen.add(key)

    @property
    def original_binding(self) -> tuple[str, str, str]:
        """`(artifact_id, s3_version_id, content_sha256)` — 승인이 붙은 정확한 원본 판본.

        `docs/POLICY_INGESTION.md` Original finalization이 요구하는 tuple 전체다. 게시 시점의
        대조는 `(artifact_id, content_sha256)`까지만 가능하다 — `PolicySource` Contract에
        `s3_version_id` 필드가 없다. 두 값이 같으면 같은 바이트이므로 판본이 뒤바뀌는 경우는
        걸리지만, S3 object version까지 못 박으려면 A가 조건부 write에서 이 tuple을 그대로 쓴다.
        """
        return (self.artifact_id, self.s3_version_id, self.content_sha256)

    def approves(self, reference: PolicyRuleReference) -> bool:
        """Whether this approval covers one exact Rule version."""
        return any(
            approved.rule_id == reference.rule_id and approved.version == reference.version
            for approved in self.approved_rules
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "artifact_id": self.artifact_id,
            "s3_version_id": self.s3_version_id,
            "content_sha256": self.content_sha256,
            "normalized_artifact_id": self.normalized_artifact_id,
            "normalized_sha256": self.normalized_sha256,
            "approved_rules": [reference.to_dict() for reference in self.approved_rules],
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }
