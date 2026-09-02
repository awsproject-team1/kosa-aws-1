"""Authenticated public read boundary for paginated Assessment reports."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from apps.backend.assessment import (
    AssessmentReport,
    AssessmentReportNotFoundError,
    AssessmentReportStoreError,
)
from apps.backend.auth import Action, Principal, authorize
from apps.backend.jobs import JobNotFoundError, authorize_job_read
from apps.backend.policy import annotate_suppressed_findings
from apps.backend.repositories import JobRepository, RepositoryError
from packages.contracts import Finding, RemediationException


class AssessmentReportReader(Protocol):
    def get_assessment_job_id(self, *, customer_id: str, assessment_id: str) -> str: ...

    def get_report_page(
        self,
        *,
        customer_id: str,
        assessment_id: str,
        limit: int,
        cursor: str | None = None,
        findings_cursor: str | None = None,
    ) -> AssessmentReport: ...


class RemediationExceptionReader(Protocol):
    """Read-only view of a customer's remediation exceptions for one Finding."""

    def list_exceptions(
        self, *, customer_id: str, finding: Finding
    ) -> tuple[RemediationException, ...]: ...


class AssessmentReportApiService:
    """Read a report only after customer and Job-owner authorization.

    When an exception reader and clock are wired, the service also joins the
    customer's in-force exceptions onto the page's findings at read time and
    returns them as display-only suppression notes (ADR-0020 §6). The join never
    mutates or persists a Finding; absence of a note means "not suppressed".
    """

    def __init__(
        self,
        *,
        jobs: JobRepository,
        reports: AssessmentReportReader,
        exceptions: RemediationExceptionReader | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if jobs is None or reports is None:
            raise TypeError("jobs and reports are required")
        # The suppression join needs both the exception reader and a read clock.
        # Requiring them together avoids a half-wired state that silently skips
        # suppression while looking configured.
        if (exceptions is None) != (now is None):
            raise TypeError("exceptions and now must be provided together")
        self._jobs = jobs
        self._reports = reports
        self._exceptions = exceptions
        self._now = now

    def get_assessment(
        self,
        principal: Principal,
        assessment_id: str,
        *,
        limit: int,
        cursor: str | None,
        findings_cursor: str | None = None,
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
            report = self._reports.get_report_page(
                customer_id=principal.customer_id,
                assessment_id=assessment_id,
                limit=limit,
                cursor=cursor,
                findings_cursor=findings_cursor,
            )
        except AssessmentReportNotFoundError:
            raise JobNotFoundError("assessment report not found") from None
        except AssessmentReportStoreError:
            raise RepositoryError("assessment report is unavailable") from None
        return self._annotate_suppressions(principal.customer_id, report)

    def _annotate_suppressions(
        self, customer_id: str, report: AssessmentReport
    ) -> AssessmentReport:
        """Join in-force exceptions onto the page's findings at read time.

        Skipped when no exception reader is wired (the report already defaults to
        no suppressions). The exception store is queried per (rule_id, rule_version)
        via each finding, so the same exception can come back for several findings;
        they are de-duplicated by exception_id before the shared predicate decides
        which findings each one actually suppresses (ADR-0020 §6).
        """
        if self._exceptions is None or self._now is None or not report.findings:
            return report
        try:
            collected: dict[str, RemediationException] = {}
            for finding in report.findings:
                for exception in self._exceptions.list_exceptions(
                    customer_id=customer_id, finding=finding
                ):
                    collected[exception.exception_id] = exception
        except (RepositoryError, ValueError, TypeError):
            # Suppression is a display aid, not an authorization gate. If exceptions
            # cannot be read we fail toward showing the violation rather than hiding
            # it, so a reader fault never quietly suppresses a finding.
            return report
        notes = annotate_suppressed_findings(
            report.findings,
            customer_id=customer_id,
            exceptions=tuple(collected.values()),
            at=self._now(),
        )
        return report.with_suppressions(notes)
