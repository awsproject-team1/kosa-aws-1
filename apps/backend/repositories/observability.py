"""감사 이력 개수를 릴리스 게이트의 4개 범주로 접는 순수 조립 (ADR-0021 §3).

ADR-0021 §3의 감사 항목은 "Remediation·Approval·Apply·Verification audit event가 모두 존재"를
요구한다. `AuditEventType`(packages/contracts/audit.py)은 그보다 세분화된 종류를 쓰므로, 이
함수가 각 종류를 게이트의 네 범주로 결정적으로 매핑해 `AuditTrailMetric`을 만든다. 매핑을 한
곳에 두어 새 `AuditEventType`이 늘 때 어느 범주에 드는지 여기서만 정한다.
"""

from collections.abc import Mapping

from packages.contracts import AuditEventType, AuditTrailMetric

# 게이트 범주 → 그 범주로 세는 AuditEventType 집합. Apply 성공 사실은 별도 event가 아니라
# 배포 승인·재조회 흐름의 durable 사실이므로, 이 함수는 DEPLOYMENT_APPROVED를 승인 범주로만
# 세고 apply 성공 건수는 별도 인자로 받는다(감사 event가 아니라 run 사실이기 때문).
_REMEDIATION_TYPES = frozenset({AuditEventType.REMEDIATION_DECIDED})
_APPROVAL_TYPES = frozenset({AuditEventType.DEPLOYMENT_APPROVED})


def assemble_audit_trail_metric(
    *,
    event_counts: Mapping[AuditEventType, int],
    apply_success_count: int,
    verification_count: int,
) -> AuditTrailMetric:
    """감사 event 개수와 apply·verification 사실을 게이트 범주로 접는다.

    `event_counts`는 데모 실행 scope 안에서 종류별로 집계된 audit event 수다. apply 성공과
    Post-Deploy Verification은 audit event 종류로 따로 있지 않으므로(각각 run 재조회 사실과
    새 Assessment 기록) 별도 인자로 받는다. 모든 값은 0 이상이어야 한다.
    """
    if not isinstance(event_counts, Mapping):
        raise TypeError("event_counts must be a mapping")
    if isinstance(apply_success_count, bool) or not isinstance(apply_success_count, int):
        raise TypeError("apply_success_count must be an integer")
    if isinstance(verification_count, bool) or not isinstance(verification_count, int):
        raise TypeError("verification_count must be an integer")

    remediation = 0
    approval = 0
    for event_type, count in event_counts.items():
        if not isinstance(event_type, AuditEventType):
            raise TypeError("event_counts keys must be AuditEventType values")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("event_counts values must be non-negative integers")
        if event_type in _REMEDIATION_TYPES:
            remediation += count
        elif event_type in _APPROVAL_TYPES:
            approval += count

    return AuditTrailMetric(
        remediation_events=remediation,
        approval_events=approval,
        apply_events=apply_success_count,
        verification_events=verification_count,
    )
