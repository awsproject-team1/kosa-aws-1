"""Judge whether one Finding may be remediated automatically, is exempt, or needs a human.

`PROGRESS.md` M2 B 항목이다. D의 Patch 생성과 A의 Remediation API가 이 판정을 **앞에서**
호출한다. 이 모듈은 아무것도 영속화하지 않고 GitHub·AWS·Terraform 어느 쪽도 건드리지 않는다.

허용 범위는 커밋된 Registry(`fixtures/rules/remediation.json`)가 정본이고, 예외는 고객 데이터라
A가 저장한 것을 판정 시점에 인자로 받는다. 둘을 한 객체에 담지 않는 이유는 수명이 다르기
때문이다 — 허용 범위는 Rule version과 함께 커밋되고, 예외는 고객이 언제든 등록하고 만료된다.

`annotate_suppressed_findings()`는 M3 B 항목이다 (ADR-0020 §6). 예외는 평가 게이트가 아니므로
재평가를 막지 않고 Finding도 그대로 생성되며, 억제는 조회 시점 표시로만 존재한다. 조치 판정과
같은 술어(`select_in_force_exception()`)를 공유해 화면과 판정이 갈리지 않게 한다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from packages.contracts import (
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    require_non_empty_string,
    require_offset_aware_timestamp,
    require_optional_non_empty_string,
)
from packages.contracts.remediation_policy import (
    ManualReviewCode,
    RemediationAction,
    RemediationDecision,
    RemediationEligibility,
    RemediationException,
    RemediationExceptionReason,
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
        commit_sha: str,
        finding_evaluated_at: datetime,
        at: datetime,
        exceptions: Iterable[RemediationException] = (),
    ) -> RemediationDecision:
        """Decide what may happen to one Finding, without performing any of it.

        `commit_sha`는 이 판정이 조치 대상으로 삼는 IaC commit이고, `finding_evaluated_at`은
        Finding이 평가된 시각, `at`은 판정 시각이다. 조치 요청은 평가보다 늦게 들어오는 것이
        정상이므로 셋을 하나로 합칠 수 없다.

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
           `TERRAFORM_PATCH`, 알 수 없으면 `MANUAL_REVIEW`다. 그 판정이 `commit_sha`가 아닌
           다른 commit에서 나왔으면 알 수 없는 것으로 취급한다.
        6. 5번이 `TERRAFORM_PATCH`를 고른 경우에만 `MANUAL_ONLY` 판단이 막는다. 허용 범위는
           **Patch 합성**에 대한 판단이기 때문이다 (ADR-0017).
        """
        if not isinstance(finding, Finding):
            raise TypeError("finding must be a Finding")
        if not isinstance(target, RemediationTarget):
            raise TypeError("target must be a RemediationTarget")
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(commit_sha, str) or not commit_sha.strip():
            raise ValueError("commit_sha must be a non-empty string")
        for name, moment in (("finding_evaluated_at", finding_evaluated_at), ("at", at)):
            if not isinstance(moment, datetime):
                raise TypeError(f"{name} must be a datetime")
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError(f"{name} must be offset-aware")
        if finding_evaluated_at > at:
            raise ValueError("finding_evaluated_at must not be after the decision time")
        if finding.resource_id != target.resource_id:
            # 다른 리소스의 상태로 판정하면 관리 여부와 IaC 판정이 통째로 어긋난다.
            raise ValueError("target describes a different resource than the finding")
        if (target.rule_id, target.rule_version) != (finding.rule_id, finding.rule_version):
            # `iac_status`는 같은 Rule version의 판정일 때만 의미가 있다. 대조하지 않으면 같은
            # 리소스의 다른 Rule에서 나온 `PASS`가 `ACTUAL_SYNC`를 열어, 안전하지 않은 IaC를
            # 배포 대상으로 삼게 된다.
            raise ValueError("target describes a different rule than the finding")

        exception = select_in_force_exception(
            finding,
            customer_id=customer_id,
            finding_evaluated_at=finding_evaluated_at,
            at=at,
            exceptions=exceptions,
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

        if self._is_stale_verdict(finding, target, commit_sha=commit_sha):
            return self._manual(finding, ManualReviewCode.IAC_VERDICT_COMMIT_MISMATCH)

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
    def _is_stale_verdict(finding: Finding, target: RemediationTarget, *, commit_sha: str) -> bool:
        """Whether the IaC verdict came from a commit other than the one being remediated.

        Assessment 이후 Repository가 진행하는 것은 예외 상황이 아니라 정상이다. 사람이 결과를
        보고 조치를 고르는 사이에 다른 commit이 올라오면, `IAC` 관점이 통과시킨 것은 그 옛
        commit이고 지금 배포될 commit은 평가된 적이 없다. `ACTUAL_SYNC`는 **IAC를 통과한 그
        commit**을 배포해야 하므로(ADR-0017), 출처가 어긋난 판정은 `PASS`든 `FAIL`이든 모르는
        것으로 되돌린다. `FAIL`도 마찬가지다 — 옛 commit의 실패는 이미 고쳐졌을 수 있고, 그 위에
        Patch를 합성하면 사람이 쓴 수정을 되돌릴 수 있다.

        `IAC` Finding은 자기 관점의 판정을 다시 읽지 않으므로(`_proposed_action`이 곧바로
        `TERRAFORM_PATCH`를 고른다) 이 대조의 대상이 아니다. 판정이 아예 없는 경우도 여기서
        걸러내지 않는다 — 그것은 출처 불일치가 아니라 `IAC_OUTCOME_UNKNOWN`이다.
        """
        if finding.perspective is EvaluationPerspective.IAC:
            return False
        return target.iac_commit_sha is not None and target.iac_commit_sha != commit_sha

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


def select_in_force_exception(
    finding: Finding,
    *,
    customer_id: str,
    finding_evaluated_at: datetime,
    at: datetime,
    exceptions: Iterable[RemediationException],
) -> RemediationException | None:
    """Return the narrowest in-force exemption covering this Finding, or `None`.

    조치 판정(`RemediationPolicy.decide()`)과 조회 시점 억제 표시
    (`annotate_suppressed_findings()`)가 **이 하나의 술어**를 공유한다. 둘이 각자 판정하면 화면이
    "억제됨"이라 말하는데 판정은 `SUPPRESSED`가 아닌 상태가 만들어지고, 사람은 어느 쪽이 거짓인지
    알 수 없다.

    여러 예외가 겹치면 리소스 단위가 Rule 전체보다 우선한다. 같은 좁기가 여러 건이면
    `exception_id` 순으로 첫 번째다 — 입력 순서를 기준으로 삼으면 저장소 조회 순서가 달라질
    때 감사 기록에 남는 `exception_id`가 같은 사실에 대해 달라진다.
    """
    if finding_evaluated_at > at:
        raise ValueError("finding_evaluated_at must not be after the selection time")
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
        if not exception.is_in_force_for(evaluated_at=finding_evaluated_at, at=at):
            # Finding이 평가된 뒤에 승인된 예외이거나 이미 만료된 예외다. 둘 다 없는 것과
            # 같고, Finding은 조치 판정을 받는다.
            continue
        matches.append(exception)
    if not matches:
        return None
    return min(
        matches,
        key=lambda exception: (exception.resource_id is None, exception.exception_id),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FindingSuppression:
    """A display-only note that one Finding is covered by an in-force customer exemption.

    `Finding`의 사본도 교체본도 아니다. `Finding`에 필드를 더하거나 억제된 Finding을 바꿔서
    돌려주면 호출자가 그것을 그대로 저장할 수 있고, 예외는 만료되므로 저장된 억제는 만료 이후
    과거 사실을 왜곡한다 (ADR-0020 §6). 별도 값으로 두면 저장 경로로 흘러도 Finding item에는
    들어가지 않는다.

    `expires_at`을 함께 싣는 이유는 화면이 "언제까지"를 보여줘야 하기 때문이다. 억제는 영구
    면제가 아니며, 만료일이 보이지 않으면 통제가 조용히 사라진 것과 구분되지 않는다.
    """

    finding_id: str
    exception_id: str
    reason: RemediationExceptionReason
    expires_at: str
    ticket_reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("finding_id", "exception_id"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.reason, RemediationExceptionReason):
            raise TypeError("reason must be a RemediationExceptionReason")
        # 화면에 "언제까지"로 노출되는 값이므로 표시 경계에서도 Contract와 같은 형식을 요구한다.
        # 지금은 이미 검증된 `RemediationException.expires_at`에서만 채워지지만, 다른 호출자가
        # 생겼을 때 offset 없는 문자열이 만료일로 표시되면 읽는 사람마다 만료 시점이 달라진다.
        require_offset_aware_timestamp(self.expires_at, "expires_at")
        require_optional_non_empty_string(self.ticket_reference, "ticket_reference")


def annotate_suppressed_findings(
    findings: Iterable[Finding],
    *,
    customer_id: str,
    exceptions: Iterable[RemediationException],
    at: datetime,
) -> tuple[FindingSuppression, ...]:
    """Return the read-time suppression notes for `findings`, in input order.

    ADR-0020 §6: 예외는 평가 게이트가 아니다. 위반이면 Finding은 그대로 생성되고 저장되며, 억제는
    **조회 시점에 join해 표시만** 한다. 그래서 이 함수는 아무것도 저장하지 않고 `Finding`을 바꾸지
    않으며, 억제된 것만 담는다 — 목록의 부재가 곧 "억제 아님"이다.

    평가 시각은 `Finding.evaluated_at`에서 읽는다. 그 값이 없는 옛 record는 **억제하지 않는다**.
    두 시각 규칙(ADR-0017)은 "승인이 평가보다 앞설 것"을 요구하는데, 없는 평가 시각을 조회 시각으로
    대체하면 나중에 승인된 예외가 옛 위반을 덮는 바로 그 경로가 열린다. 근거가 없을 때는 위반이
    보이는 쪽으로 닫는다.

    `at`은 조회 시각이다. 만료는 이 시각 기준으로 판정하므로, 같은 Assessment를 다른 시각에 조회하면
    억제 표시가 사라질 수 있다. 그것이 의도다 — 만료된 면제는 더 이상 유효하지 않다.
    """
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("customer_id must be a non-empty string")
    if not isinstance(at, datetime):
        raise TypeError("at must be a datetime")
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be offset-aware")
    resolved_exceptions = tuple(exceptions)

    notes: list[FindingSuppression] = []
    for finding in findings:
        if not isinstance(finding, Finding):
            raise TypeError("findings must contain Finding values")
        if finding.evaluated_at is None:
            continue
        evaluated_at = finding.evaluated_at_utc
        exception = select_in_force_exception(
            finding,
            customer_id=customer_id,
            finding_evaluated_at=evaluated_at,
            at=at,
            exceptions=resolved_exceptions,
        )
        if exception is None:
            continue
        notes.append(
            FindingSuppression(
                finding_id=finding.finding_id,
                exception_id=exception.exception_id,
                reason=exception.reason,
                expires_at=exception.expires_at,
                ticket_reference=exception.ticket_reference,
            )
        )
    return tuple(notes)
