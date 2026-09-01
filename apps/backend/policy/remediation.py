"""Judge whether one Finding may be remediated automatically, is exempt, or needs a human.

`PROGRESS.md` M2 B 항목이다. D의 Patch 생성과 A의 Remediation API가 이 판정을 **앞에서**
호출한다. 이 모듈은 아무것도 영속화하지 않고 GitHub·AWS·Terraform 어느 쪽도 건드리지 않는다.

허용 범위는 커밋된 Registry(`fixtures/rules/remediation.json`)가 정본이고, 예외는 고객 데이터라
A가 저장한 것을 판정 시점에 인자로 받는다. 둘을 한 객체에 담지 않는 이유는 수명이 다르기
때문이다 — 허용 범위는 Rule version과 함께 커밋되고, 예외는 고객이 언제든 등록하고 만료된다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from packages.contracts import EvaluationPerspective, EvaluationStatus, Finding
from packages.contracts.remediation_policy import (
    ManualReviewCode,
    RemediationAction,
    RemediationDecision,
    RemediationEligibility,
    RemediationException,
    RemediationRuleScope,
    RemediationTarget,
)

#: Actual/Drift Finding에서 "IaC는 이미 안전하다"고 말할 수 있는 유일한 판정.
#: 나머지는 전부 모르는 것으로 취급한다 — `OUT_OF_SCOPE`나 `EXECUTION_ERROR`를 안전으로 읽으면
#: 평가되지 않은 IaC를 배포 대상으로 삼게 된다.
_IAC_SAFE_STATUSES = frozenset({EvaluationStatus.PASS})

#: 자동 조치를 계산할 수 있는 유일한 Finding status. `Finding` Contract가 현재 허용하는 나머지
#: 둘은 아래 표로 사유가 붙고, 표에 없는 status가 생기면 안전으로 새는 대신 사람에게 간다.
_REMEDIABLE_STATUS = EvaluationStatus.FAIL

_STATUS_MANUAL_REVIEW_CODES = {
    EvaluationStatus.INSUFFICIENT_EVIDENCE: ManualReviewCode.INSUFFICIENT_EVIDENCE,
    EvaluationStatus.MANUAL_REVIEW: ManualReviewCode.EVALUATION_REQUIRES_REVIEW,
}


class RemediationPolicyError(ValueError):
    """Raised when the committed remediation scope is malformed or ambiguous."""


class RemediationPolicy:
    """The committed remediation scope: which Rule versions may be fixed automatically.

    등록되지 않은 Rule은 `AUTOMATIC`이 아니라 `MANUAL_REVIEW`다. 새 Rule이 들어왔을 때 허용
    범위를 정하는 것을 잊으면 자동 조치가 조용히 열리는 것이 아니라 닫힌 채로 남는다.
    """

    def __init__(self, scopes: Iterable[RemediationRuleScope]) -> None:
        self._scopes: dict[tuple[str, str], RemediationRuleScope] = {}
        for scope in scopes:
            if not isinstance(scope, RemediationRuleScope):
                raise TypeError("scopes must contain RemediationRuleScope values")
            key = (scope.rule_id, scope.version)
            if key in self._scopes:
                raise RemediationPolicyError(
                    f"duplicate remediation scope for rule {scope.rule_id}@{scope.version}"
                )
            self._scopes[key] = scope

    @property
    def scopes(self) -> tuple[RemediationRuleScope, ...]:
        return tuple(self._scopes.values())

    def eligibility(self, *, rule_id: str, version: str) -> RemediationEligibility | None:
        """Return the committed eligibility of one exact Rule version, or `None` if unregistered."""
        scope = self._scopes.get((rule_id, version))
        return None if scope is None else scope.eligibility

    def decide(
        self,
        finding: Finding,
        *,
        customer_id: str,
        target: RemediationTarget,
        at: datetime,
        exceptions: Iterable[RemediationException] = (),
    ) -> RemediationDecision:
        """Decide what may happen to one Finding, without performing any of it.

        판정 순서는 그 자체가 정책이다.

        1. 고객이 승인한 유효한 예외가 있으면 `SUPPRESSED`. 예외는 "이 리소스에서 이 Rule은
           조치하지 않는다"는 결정이므로 조치 유형을 계산할 이유가 없다.
        2. 평가가 근거 부족이거나 이미 사람 판단을 요구했으면 `MANUAL_REVIEW`. 평가하지 못한
           것을 자동으로 고칠 수는 없다.
        3. 허용 범위에 **없는** Rule은 `MANUAL_REVIEW`. 판단이 아직 없는 것과 `MANUAL_ONLY`라는
           판단이 있는 것은 다르므로, 전자만 여기서 전부 막는다.
        4. Terraform이 관리하지 않는 리소스는 `MANUAL_REVIEW`. Patch가 닿을 자리가 없다.
        5. `IAC` Finding은 `TERRAFORM_PATCH`. `AWS_ACTUAL`/`DRIFT` Finding은 같은
           `Resource × Rule`의 IaC 판정이 `PASS`면 `ACTUAL_SYNC`, `FAIL`이면
           `TERRAFORM_PATCH`, 알 수 없으면 `MANUAL_REVIEW`다.
        6. 5번이 `TERRAFORM_PATCH`를 고른 경우에만 `MANUAL_ONLY` 판단이 막는다. 허용 범위는
           **Patch 합성**에 대한 판단이기 때문이다 (ADR-0017).
        """
        if not isinstance(finding, Finding):
            raise TypeError("finding must be a Finding")
        if not isinstance(target, RemediationTarget):
            raise TypeError("target must be a RemediationTarget")
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(at, datetime):
            raise TypeError("at must be a datetime")
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("at must be offset-aware")
        if finding.resource_id != target.resource_id:
            # 다른 리소스의 상태로 판정하면 관리 여부와 IaC 판정이 통째로 어긋난다.
            raise ValueError("target describes a different resource than the finding")
        if (target.rule_id, target.rule_version) != (finding.rule_id, finding.rule_version):
            # `iac_status`는 같은 Rule version의 판정일 때만 의미가 있다. 대조하지 않으면 같은
            # 리소스의 다른 Rule에서 나온 `PASS`가 `ACTUAL_SYNC`를 열어, 안전하지 않은 IaC를
            # 배포 대상으로 삼게 된다.
            raise ValueError("target describes a different rule than the finding")

        exception = self._active_exception(
            finding, customer_id=customer_id, at=at, exceptions=exceptions
        )
        if exception is not None:
            return self._decision(
                finding, action=RemediationAction.SUPPRESSED, exception_id=exception.exception_id
            )

        if finding.status is not _REMEDIABLE_STATUS:
            return self._manual(
                finding,
                _STATUS_MANUAL_REVIEW_CODES.get(
                    finding.status, ManualReviewCode.EVALUATION_REQUIRES_REVIEW
                ),
            )

        eligibility = self.eligibility(rule_id=finding.rule_id, version=finding.rule_version)
        if eligibility is None:
            # 판단 자체가 없다. 판단의 부재는 `MANUAL_ONLY`라는 판단과 다르므로 전부 막는다.
            return self._manual(finding, ManualReviewCode.RULE_NOT_IN_SCOPE)

        if not target.terraform_managed:
            return self._manual(finding, ManualReviewCode.RESOURCE_NOT_IAC_MANAGED)

        action = self._proposed_action(finding, target)
        if action is None:
            return self._manual(finding, ManualReviewCode.IAC_OUTCOME_UNKNOWN)
        if (
            action is RemediationAction.TERRAFORM_PATCH
            and eligibility is RemediationEligibility.MANUAL_ONLY
        ):
            return self._manual(finding, ManualReviewCode.RULE_MANUAL_ONLY)
        return self._decision(finding, action=action)

    @staticmethod
    def _proposed_action(finding: Finding, target: RemediationTarget) -> RemediationAction | None:
        """Which action the perspectives point at, or `None` when the IaC verdict is unknown.

        `ACTUAL_SYNC`는 Patch를 합성하지 않는다. 사람이 쓰고 `IAC` 관점 평가를 통과한 commit을
        그대로 배포 대상으로 삼는 것이므로, Rule의 patch 허용 범위가 막을 대상이 아니다. 적용의
        파괴성은 refresh된 Plan과 Human Approval이 판단한다 (ADR-0007).
        """
        if finding.perspective is EvaluationPerspective.IAC:
            return RemediationAction.TERRAFORM_PATCH
        if target.iac_status in _IAC_SAFE_STATUSES:
            # IaC는 이미 안전하다. Patch를 만들면 안전한 코드를 건드리게 되므로, 현재 commit을
            # 그대로 배포 대상으로 삼아 Actual만 맞춘다 (`docs/PRD.md` Assessment stages).
            return RemediationAction.ACTUAL_SYNC
        if target.iac_status is EvaluationStatus.FAIL:
            return RemediationAction.TERRAFORM_PATCH
        return None

    def _active_exception(
        self,
        finding: Finding,
        *,
        customer_id: str,
        at: datetime,
        exceptions: Iterable[RemediationException],
    ) -> RemediationException | None:
        """Return the narrowest active exemption covering this Finding, or `None`.

        여러 예외가 겹치면 리소스 단위가 Rule 전체보다 우선한다. 같은 좁기가 여러 건이면
        `exception_id` 순으로 첫 번째다 — 입력 순서를 기준으로 삼으면 저장소 조회 순서가 달라질
        때 감사 기록에 남는 `exception_id`가 같은 사실에 대해 달라진다.
        """
        matches: list[RemediationException] = []
        for exception in exceptions:
            if not isinstance(exception, RemediationException):
                raise TypeError("exceptions must contain RemediationException values")
            if not exception.covers(
                customer_id=customer_id,
                rule_id=finding.rule_id,
                rule_version=finding.rule_version,
                resource_id=finding.resource_id,
            ):
                continue
            if not exception.is_active_at(at):
                # 승인 전이거나 만료된 예외는 없는 것과 같다. Finding은 조치 판정을 받는다.
                continue
            matches.append(exception)
        if not matches:
            return None
        return min(
            matches,
            key=lambda exception: (exception.resource_id is None, exception.exception_id),
        )

    def _manual(self, finding: Finding, code: ManualReviewCode) -> RemediationDecision:
        return self._decision(
            finding, action=RemediationAction.MANUAL_REVIEW, manual_review_code=code
        )

    def _decision(
        self,
        finding: Finding,
        *,
        action: RemediationAction,
        manual_review_code: ManualReviewCode | None = None,
        exception_id: str | None = None,
    ) -> RemediationDecision:
        return RemediationDecision(
            finding_id=finding.finding_id,
            resource_id=finding.resource_id,
            rule_id=finding.rule_id,
            rule_version=finding.rule_version,
            perspective=finding.perspective,
            action=action,
            manual_review_code=manual_review_code,
            exception_id=exception_id,
        )
