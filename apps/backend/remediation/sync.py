"""D의 `ACTUAL_SYNC` 실행 port 구현 (ADR-0018, ADR-0019 §3).

`ACTUAL_SYNC`는 새 Patch를 만들지 않는다. 조치 대상은 "이미 `IAC` 관점을 통과한 현재 default
branch commit"이고, 그 commit은 Assessment가 평가한 바로 그 snapshot commit이다 — Finding이
`AWS_ACTUAL`/`DRIFT`에서 나왔다는 것은 코드는 맞는데 실제 리소스가 어긋나 있다는 뜻이므로,
맞춰야 할 기준이 그 코드다.

그래서 이 port는 GitHub를 읽지 않는다. 읽어서 "지금의 default branch head"를 가져오면 평가 이후
merge된 다른 변경까지 apply 대상에 들어오고, 그건 아무도 이 Finding의 조치로 승인한 적 없는
코드다. `RemediationWorker._require_sync_result`가 결과의 commit이 snapshot commit과 같기를
요구하는 것도 같은 이유이며, 이 구현은 그 불변식을 만족하는 유일한 값을 돌려준다.
"""

from packages.contracts import RemediationContext, RemediationDecision, RemediationSyncTarget
from packages.contracts.remediation_policy import RemediationAction


class SyncActionError(ValueError):
    """`ACTUAL_SYNC` 대상을 만들 수 없다."""


class SnapshotSyncAction:
    """Return the assessed snapshot commit as the sync target, deterministically."""

    def prepare(
        self, *, context: RemediationContext, decision: RemediationDecision
    ) -> RemediationSyncTarget:
        if not isinstance(context, RemediationContext):
            raise TypeError("context must be a RemediationContext")
        if not isinstance(decision, RemediationDecision):
            raise TypeError("decision must be a RemediationDecision")
        if decision.action is not RemediationAction.ACTUAL_SYNC:
            # 판정 게이트는 Worker가 이미 통과시켰지만, 다른 경로에서 호출돼도 이 port가
            # 허가되지 않은 조치를 만들어내지는 않는다.
            raise SyncActionError("sync targets require an ACTUAL_SYNC decision")
        snapshot = context.snapshot
        return RemediationSyncTarget(
            finding_id=context.finding.finding_id,
            customer_id=snapshot.customer_id,
            repository_id=snapshot.repository_id,
            commit_sha=snapshot.commit_sha,
        )
