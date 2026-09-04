"""Read one remediation's decision, worker result, and pull request (customer scoped).

`POST /findings/{findingId}/remediations`는 판정과 Job만 돌려준다. 그 뒤 Worker가 만든 patch와
D가 연 PR은 `REMEDIATION#{id}` item에만 쌓이고, 화면이 그것을 읽을 경로가 없었다 — 사용자는
"조치를 요청했다"까지만 보고 "무엇이 제안됐고 어디서 검토하는가"를 볼 수 없었다. 이 서비스는
그 item을 **읽기만** 한다. patch 바이트나 PR 본문을 다시 만들지 않고, 저장된 identity(changed
paths, digest)와 PR 좌표(number, url, branch)만 돌려준다.

권한은 Job과 같은 규칙이다: 같은 customer partition 안에서, Admin이거나 그 remediation의 Job을
요청한 사용자. Job이 없는 record(MANUAL_REVIEW/SUPPRESSED 판정)는 요청자가 저장돼 있지 않으므로
같은 customer의 인증 사용자에게 열린다 — 판정 사실 외에 노출되는 것이 없다.
"""

from __future__ import annotations

from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from apps.backend.jobs import JobNotFoundError, authorize_job_read
from apps.backend.repositories import JobRepository
from apps.backend.repositories.remediation_read import (
    RemediationNotFoundError,
    RemediationView,
)

__all__ = ["RemediationNotFoundError", "RemediationReadApiService", "RemediationView"]


class RemediationRecordReader(Protocol):
    def get_remediation(self, *, customer_id: str, remediation_id: str) -> RemediationView: ...


class RemediationReadApiService:
    """Serve `GET /remediations/{remediationId}` inside the caller's partition."""

    def __init__(self, *, jobs: JobRepository, remediations: RemediationRecordReader) -> None:
        if jobs is None or remediations is None:
            raise TypeError("jobs and remediations are required")
        self._jobs = jobs
        self._remediations = remediations

    def get_remediation(self, principal: Principal, remediation_id: str) -> RemediationView:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(remediation_id, str) or not remediation_id.strip():
            raise ValueError("remediation_id must be a non-empty string")
        authorize(principal, Action.READ_JOB)
        try:
            view = self._remediations.get_remediation(
                customer_id=principal.customer_id, remediation_id=remediation_id
            )
        except RemediationNotFoundError:
            raise JobNotFoundError("remediation not found") from None
        if view.job_id is not None:
            job = self._jobs.get_job(principal.customer_id, view.job_id)
            if job is None or job.remediation_id != remediation_id:
                raise JobNotFoundError("remediation not found")
            authorize_job_read(principal, job)
        return view
