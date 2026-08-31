"""Authenticated public read boundary for paginated Assessment reports."""

from typing import Protocol

from apps.backend.assessment import (
    AssessmentReport,
    AssessmentReportNotFoundError,
    AssessmentReportStoreError,
)
from apps.backend.auth import Action, Principal, authorize
from apps.backend.jobs import JobNotFoundError, authorize_job_read
from apps.backend.repositories import JobRepository, RepositoryError


class AssessmentReportReader(Protocol):
    def get_assessment_job_id(self, *, customer_id: str, assessment_id: str) -> str: ...

    def get_report_page(
        self,
        *,
        customer_id: str,
        assessment_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> AssessmentReport: ...


class AssessmentReportApiService:
    """Read a report only after customer and Job-owner authorization."""

    def __init__(self, *, jobs: JobRepository, reports: AssessmentReportReader) -> None:
        if jobs is None or reports is None:
            raise TypeError("jobs and reports are required")
        self._jobs = jobs
        self._reports = reports

    def get_assessment(
        self,
        principal: Principal,
        assessment_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> AssessmentReport:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(assessment_id, str) or not assessment_id.strip():
            raise ValueError("assessment_id must be a non-empty string")
        authorize(principal, Action.READ_JOB)
        try:
            job_id = self._reports.get_assessment_job_id(
                customer_id=principal.customer_id, assessment_id=assessment_id
            )
            job = self._jobs.get_job(principal.customer_id, job_id)
        except AssessmentReportNotFoundError:
            raise JobNotFoundError("assessment not found") from None
        except AssessmentReportStoreError:
            raise RepositoryError("assessment report is unavailable") from None
        if job is None or job.assessment_id != assessment_id:
            raise JobNotFoundError("assessment not found")
        authorize_job_read(principal, job)
        try:
            return self._reports.get_report_page(
                customer_id=principal.customer_id,
                assessment_id=assessment_id,
                limit=limit,
                cursor=cursor,
            )
        except AssessmentReportNotFoundError:
            raise JobNotFoundError("assessment report not found") from None
        except AssessmentReportStoreError:
            raise RepositoryError("assessment report is unavailable") from None
