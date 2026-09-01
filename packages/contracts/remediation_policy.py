"""What may be remediated automatically, what is exempt, and what a human must decide.

`docs/PRD.md` Assessment stages와 `docs/DESIGN.md` State and execution이 조치 유형을 이미
규정한다. IaC가 수정돼야 하는 위반과 Drift에는 Terraform Patch를, IaC는 안전하고 Actual만
이탈한 경우에는 Patch 없는 동기화를, IaC에 매핑되지 않거나 안전한 조치를 만들 수 없는 경우에는
`MANUAL_REVIEW`를 남긴다. 이 모듈은 그 문장을 판정 가능한 값으로 고정한다 (ADR-0017).

Task 2·3의 규율을 계승한다: 사유는 자유 문장이 아니라 열거값이고, 어떤 값도 정책 원문이나
추출 텍스트를 담지 않는다.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from packages.contracts._validation import (
    require_non_empty_string,
    require_offset_aware_timestamp,
    require_optional_non_empty_string,
)
from packages.contracts.assessments import EvaluationPerspective, EvaluationStatus


class RemediationAction(StrEnum):
    """What may happen to one Finding after the policy boundary has judged it."""

    #: 안전한 상태를 확정하는 Terraform Patch를 만든다.
    TERRAFORM_PATCH = "TERRAFORM_PATCH"
    #: IaC는 이미 안전하다. Patch 없이 현재 commit을 배포 대상으로 삼아 Actual을 맞춘다.
    ACTUAL_SYNC = "ACTUAL_SYNC"
    #: 자동으로 안전한 조치를 만들 수 없다. 사람이 판단한다.
    MANUAL_REVIEW = "MANUAL_REVIEW"
    #: 고객이 승인한 예외가 덮고 있다. 이번 Assessment에서는 조치하지 않는다.
    SUPPRESSED = "SUPPRESSED"


class RemediationEligibility(StrEnum):
    """Whether a Terraform patch for one Rule version can be synthesized without a human.

    `AUTOMATIC`은 (1) Rule 하나가 준수 상태를 유일하게 결정하고 (2) 그 변경이 리소스 교체나
    데이터 손실을 요구하지 않을 때만 부여한다. 둘 중 하나라도 어긋나면 `MANUAL_ONLY`다
    (ADR-0017).

    두 기준 모두 **Patch 합성**에 대한 것이다. `ACTUAL_SYNC`는 새 변경을 만들지 않고 사람이 쓴
    commit을 배포 대상으로 삼으므로 이 값이 막지 않는다.
    """

    AUTOMATIC = "AUTOMATIC"
    MANUAL_ONLY = "MANUAL_ONLY"


class ManualReviewCode(StrEnum):
    """Why the policy boundary refused to remediate automatically."""

    #: Rule이 remediation 허용 범위에 등록돼 있지 않다. 판단이 없으면 아무것도 열지 않는다.
    RULE_NOT_IN_SCOPE = "RULE_NOT_IN_SCOPE"
    #: Rule이 `MANUAL_ONLY`이고 이번 판정이 Patch 합성을 요구했다.
    RULE_MANUAL_ONLY = "RULE_MANUAL_ONLY"
    #: Terraform이 관리하지 않는 리소스다. Patch가 닿을 자리가 없다.
    RESOURCE_NOT_IAC_MANAGED = "RESOURCE_NOT_IAC_MANAGED"
    #: Actual/Drift Finding인데 같은 Resource × Rule의 IaC 판정을 알 수 없다.
    IAC_OUTCOME_UNKNOWN = "IAC_OUTCOME_UNKNOWN"
    #: IaC 판정이 이번에 조치할 commit이 아닌 다른 commit에서 나왔다. 평가된 뒤 Repository가
    #: 진행했다면 그 판정은 지금 배포될 commit에 대해 아무것도 말하지 않는다.
    IAC_VERDICT_COMMIT_MISMATCH = "IAC_VERDICT_COMMIT_MISMATCH"
    #: 근거가 부족해 평가되지 못했다. 평가하지 못한 것을 고칠 수는 없다.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    #: 평가 자체가 이미 사람 판단을 요구했다.
    EVALUATION_REQUIRES_REVIEW = "EVALUATION_REQUIRES_REVIEW"


class RemediationExceptionReason(StrEnum):
    """Why a customer approved leaving one Finding unremediated.

    자유 문장이 아니다. 예외 사유는 감사 대상이고, 자유 입력은 정책 원문이나 리소스 내부 정보를
    운영 로그로 옮기는 가장 쉬운 경로다.
    """

    ACCEPTED_RISK = "ACCEPTED_RISK"
    COMPENSATING_CONTROL = "COMPENSATING_CONTROL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PLANNED_CHANGE = "PLANNED_CHANGE"


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationRuleScope:
    """The committed remediation eligibility of one exact Rule version.

    Rule version에 붙는다. 원문이 개정돼 Rule이 새 version을 얻으면 허용 범위도 다시 판단해야
    하며, 옛 version의 판단이 자동으로 따라오지 않는다.
    """

    rule_id: str
    version: str
    eligibility: RemediationEligibility

    def __post_init__(self) -> None:
        require_non_empty_string(self.rule_id, "rule_id")
        require_non_empty_string(self.version, "version")
        if not isinstance(self.eligibility, RemediationEligibility):
            raise TypeError("eligibility must be a RemediationEligibility")

    def to_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "eligibility": self.eligibility.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationException:
    """An approved, expiring customer exemption from remediating one Rule version.

    `resource_id`가 `None`이면 그 고객의 해당 Rule version 전체를 덮는다. 값이 있으면 그 리소스
    하나만 덮는다.

    예외는 반드시 만료된다. 만료 없는 예외는 통제를 조용히 영구 제거하고, 그 사실이 어느 화면에도
    남지 않는다. 유효 구간은 `approved_at`부터 `expires_at` 직전까지이며, 그 바깥에서 Finding은
    다시 조치 판정을 받는다 (ADR-0017).
    """

    exception_id: str
    customer_id: str
    rule_id: str
    rule_version: str
    reason: RemediationExceptionReason
    approved_by: str
    approved_at: str
    expires_at: str
    resource_id: str | None = None
    ticket_reference: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "exception_id",
            "customer_id",
            "rule_id",
            "rule_version",
            "approved_by",
        ):
            require_non_empty_string(getattr(self, name), name)
        for name in ("resource_id", "ticket_reference"):
            require_optional_non_empty_string(getattr(self, name), name)
        if not isinstance(self.reason, RemediationExceptionReason):
            raise TypeError("reason must be a RemediationExceptionReason")
        approved = require_offset_aware_timestamp(self.approved_at, "approved_at")
        expires = require_offset_aware_timestamp(self.expires_at, "expires_at")
        if expires <= approved:
            raise ValueError("expires_at must be later than approved_at")

    @property
    def approved_at_utc(self) -> datetime:
        return require_offset_aware_timestamp(self.approved_at, "approved_at")

    @property
    def expires_at_utc(self) -> datetime:
        return require_offset_aware_timestamp(self.expires_at, "expires_at")

    def is_active_at(self, moment: datetime) -> bool:
        """Whether the exemption had taken effect and not yet expired at one exact moment.

        유효 구간은 `approved_at <= moment < expires_at`이다. 한 시각에 대한 질문이므로 조치
        판정의 게이트로는 부족하다 — 판정 시각을 넘기면 나중에 승인된 예외가 그 이전에 평가된
        Finding까지 덮는다. Finding을 억제할 수 있는지는 `is_in_force_for()`가 답한다.
        """
        if not isinstance(moment, datetime):
            raise TypeError("moment must be a datetime")
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("moment must be offset-aware")
        return self.approved_at_utc <= moment < self.expires_at_utc

    def is_in_force_for(self, *, evaluated_at: datetime, at: datetime) -> bool:
        """Whether this exemption may suppress a Finding evaluated at `evaluated_at`, judged at `at`.

        두 시각을 따로 본다. 하나로 합칠 수 없기 때문이다.

        - 승인은 **Finding이 평가된 시점보다 앞서야** 한다. 어제의 위반을 오늘 등록한 예외가
          덮으면, 아무도 면제를 승인한 적 없는 시점의 위반이 억제된 채로 감사 기록에 남는다.
          승인 경계가 사후 승인으로 무너지는 것이 정확히 이 경로다 (ADR-0017).
        - 만료는 **판정 시점** 기준으로 확인한다. 평가 시점에 유효했다는 사실은 이미 만료된
          예외를 되살리는 근거가 아니다. 조치하지 않겠다는 결정은 조치를 판정하는 지금
          유효해야 한다.

        조치 요청은 평가보다 늦게 들어오는 것이 정상이므로(사람이 보고 고르는 흐름이다) 두 시각을
        같은 값으로 취급하면 두 규칙 중 하나는 반드시 어긋난다.
        """
        for name, moment in (("evaluated_at", evaluated_at), ("at", at)):
            if not isinstance(moment, datetime):
                raise TypeError(f"{name} must be a datetime")
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError(f"{name} must be offset-aware")
        return self.approved_at_utc <= evaluated_at and at < self.expires_at_utc

    def covers(
        self, *, customer_id: str, rule_id: str, rule_version: str, resource_id: str
    ) -> bool:
        """Whether this exemption is addressed at one exact Customer, Rule version, Resource.

        Rule version까지 대조한다. v1에 승인된 예외가 개정된 v2로 따라가면, 사람이 보지 않은
        새 요구사항이 승인 없이 면제된다.
        """
        for name, value in (
            ("customer_id", customer_id),
            ("rule_id", rule_id),
            ("rule_version", rule_version),
            ("resource_id", resource_id),
        ):
            require_non_empty_string(value, name)
        if customer_id != self.customer_id:
            return False
        if (rule_id, rule_version) != (self.rule_id, self.rule_version):
            return False
        return self.resource_id is None or self.resource_id == resource_id

    def to_dict(self) -> dict[str, object]:
        return {
            "exception_id": self.exception_id,
            "customer_id": self.customer_id,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "resource_id": self.resource_id,
            "reason": self.reason.value,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "expires_at": self.expires_at,
            "ticket_reference": self.ticket_reference,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationTarget:
    """What the remediation boundary knows about the resource a Finding points at.

    `iac_status`는 같은 `Resource × Rule`의 `IAC` 관점 판정이다. Actual/Drift Finding의 조치
    유형은 IaC가 이미 안전한지에 따라 갈리므로, 이 값 없이는 Patch와 동기화를 구분할 수 없다.

    그래서 그 판정이 **어느 Rule version과 관점의 것인지, 그리고 어느 commit에서 나왔는지**를
    값이 스스로 들고 다닌다. `resource_id`만 맞춰서는 같은 리소스의 다른 Rule이나 Actual
    관점에서 나온 `PASS`가 `ACTUAL_SYNC`를 열 수 있고, `iac_commit_sha`가 없으면 평가 이후
    진행된 새 commit에 옛 `PASS`를 붙일 수 있다. 둘 다 실제로 평가되지 않은 IaC를 배포 대상으로
    삼게 된다. `decide()`가 Resource·Rule identity를 Finding과 대조하고 `iac_commit_sha`를
    조치 대상 commit과 대조하며, Contract가 `iac_status`·`IAC` perspective·`iac_commit_sha`를
    한 묶음으로 강제한다.
    """

    resource_id: str
    resource_type: str
    rule_id: str
    rule_version: str
    terraform_managed: bool
    iac_status: EvaluationStatus | None = None
    iac_perspective: EvaluationPerspective | None = None
    iac_commit_sha: str | None = None

    def __post_init__(self) -> None:
        for name in ("resource_id", "resource_type", "rule_id", "rule_version"):
            require_non_empty_string(getattr(self, name), name)
        require_optional_non_empty_string(self.iac_commit_sha, "iac_commit_sha")
        if not isinstance(self.terraform_managed, bool):
            raise TypeError("terraform_managed must be a bool")
        if self.iac_status is not None and not isinstance(self.iac_status, EvaluationStatus):
            raise TypeError("iac_status must be an EvaluationStatus or None")
        if self.iac_perspective is not None and not isinstance(
            self.iac_perspective, EvaluationPerspective
        ):
            raise TypeError("iac_perspective must be an EvaluationPerspective or None")
        provided = {
            self.iac_status is None,
            self.iac_perspective is None,
            self.iac_commit_sha is None,
        }
        if len(provided) != 1:
            # 판정만 있고 출처가 없으면 대조할 것이 없다. 부분적으로 채워진 판정은 없는 것보다
            # 위험하다 — `decide()`가 대조를 건너뛰고 그 값을 그대로 쓰게 된다.
            raise ValueError(
                "iac_status, iac_perspective and iac_commit_sha must be provided together"
            )
        if (
            self.iac_perspective is not None
            and self.iac_perspective is not EvaluationPerspective.IAC
        ):
            raise ValueError("iac_perspective must identify an IAC evaluation")

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "terraform_managed": self.terraform_managed,
            "iac_status": None if self.iac_status is None else self.iac_status.value,
            "iac_perspective": (
                None if self.iac_perspective is None else self.iac_perspective.value
            ),
            "iac_commit_sha": self.iac_commit_sha,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationDecision:
    """The immutable judgement one Finding receives before any patch is generated.

    거부와 면제는 예외가 아니라 값이다. 고객에게 "왜 자동으로 고치지 않았는지"를 보여줘야 하는
    정상 결과이지 오류가 아니다 (Task 2의 `FAILED` 상태 반환과 같은 이유).
    """

    finding_id: str
    resource_id: str
    rule_id: str
    rule_version: str
    perspective: EvaluationPerspective
    action: RemediationAction
    manual_review_code: ManualReviewCode | None = None
    exception_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("finding_id", "resource_id", "rule_id", "rule_version"):
            require_non_empty_string(getattr(self, name), name)
        require_optional_non_empty_string(self.exception_id, "exception_id")
        if not isinstance(self.perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")
        if not isinstance(self.action, RemediationAction):
            raise TypeError("action must be a RemediationAction")
        if self.manual_review_code is not None and not isinstance(
            self.manual_review_code, ManualReviewCode
        ):
            raise TypeError("manual_review_code must be a ManualReviewCode or None")
        if (self.action is RemediationAction.MANUAL_REVIEW) != (
            self.manual_review_code is not None
        ):
            raise ValueError("only a MANUAL_REVIEW decision carries a manual_review_code")
        if (self.action is RemediationAction.SUPPRESSED) != (self.exception_id is not None):
            raise ValueError("only a SUPPRESSED decision carries an exception_id")

    @property
    def is_actionable(self) -> bool:
        """Whether D may build something from this decision."""
        return self.action in {RemediationAction.TERRAFORM_PATCH, RemediationAction.ACTUAL_SYNC}

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "resource_id": self.resource_id,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "perspective": self.perspective.value,
            "action": self.action.value,
            "manual_review_code": (
                None if self.manual_review_code is None else self.manual_review_code.value
            ),
            "exception_id": self.exception_id,
        }
