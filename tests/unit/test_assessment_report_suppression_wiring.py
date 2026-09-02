"""Read-time suppression is joined onto the assessment report page (ADR-0020 §6).

These lock the wiring the store cannot verify on its own: the API service must
call annotate_suppressed_findings() with the customer's in-force exceptions and a
read clock, expose the notes on the report, de-duplicate exceptions gathered per
finding, and fail toward showing the violation when the exception reader faults.
"""

import unittest
from datetime import UTC, datetime

from apps.backend.api.assessments import AssessmentReportApiService
from apps.backend.assessment import AssessmentReport, AssessmentReportNotFoundError
from apps.backend.auth import Principal, Role
from apps.backend.jobs import JobNotFoundError
from apps.backend.jobs.models import Job
from apps.backend.repositories import RepositoryError
from packages.contracts import (
    AssessmentCoverage,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    JobCurrentStep,
    JobStatus,
    RemediationException,
    RemediationExceptionReason,
)

CUSTOMER = "cust-001"
ASSESSMENT = "asm-001"
JOB = "job-001"
READ_AT = datetime(2026, 9, 1, tzinfo=UTC)


def _principal() -> Principal:
    return Principal(
        subject="subject-001",
        client_id="client-001",
        customer_id=CUSTOMER,
        roles=frozenset({Role.USER}),
    )


def _finding(*, finding_id: str = "finding-001", resource_id: str = "bucket-001") -> Finding:
    return Finding(
        finding_id=finding_id,
        resource_id=resource_id,
        rule_id="S3-001",
        rule_version="v1",
        perspective=EvaluationPerspective.IAC,
        status=EvaluationStatus.FAIL,
        severity="HIGH",
        score=10,
        rationale="Fixture failing finding.",
        evidence_references=("fixture:evidence",),
        assessed_commit_sha="commit-abc",
        evaluated_at="2026-01-01T00:00:00+00:00",
    )


def _exception(*, exception_id: str = "exception-001") -> RemediationException:
    return RemediationException(
        exception_id=exception_id,
        customer_id=CUSTOMER,
        rule_id="S3-001",
        rule_version="v1",
        reason=RemediationExceptionReason.ACCEPTED_RISK,
        approved_by="security-owner",
        approved_at="2025-01-01T00:00:00+00:00",
        expires_at="2026-12-31T00:00:00+00:00",
    )


def _job() -> Job:
    return Job(
        job_id=JOB,
        customer_id=CUSTOMER,
        job_type="ASSESSMENT",
        status=JobStatus.COMPLETED,
        current_step=JobCurrentStep.GENERATE_REPORT,
        requested_by="subject-001",
        revision=1,
        assessment_id=ASSESSMENT,
    )


class Jobs:
    def get_job(self, customer_id: str, job_id: str):
        if customer_id == CUSTOMER and job_id == JOB:
            return _job()
        return None


class Reports:
    def __init__(self, findings: tuple[Finding, ...]) -> None:
        self._findings = findings

    def get_assessment_job_id(self, *, customer_id: str, assessment_id: str) -> str:
        if customer_id != CUSTOMER or assessment_id != ASSESSMENT:
            raise AssessmentReportNotFoundError("assessment not found")
        return JOB

    def get_report_page(
        self,
        *,
        customer_id: str,
        assessment_id: str,
        limit: int,
        cursor: str | None = None,
        findings_cursor: str | None = None,
    ) -> AssessmentReport:
        return AssessmentReport(
            assessment_id=assessment_id,
            results=(),
            findings=self._findings,
            coverage=AssessmentCoverage(planned_evaluations=1, completed_evaluations=1),
            readiness_score=None,
        )


class Exceptions:
    def __init__(self, exceptions: tuple[RemediationException, ...]) -> None:
        self._exceptions = exceptions
        self.calls: list[str] = []

    def list_exceptions(self, *, customer_id: str, finding: Finding):
        self.calls.append(finding.finding_id)
        return self._exceptions


class FaultyExceptions:
    def list_exceptions(self, *, customer_id: str, finding: Finding):
        raise RepositoryError("exception store is unavailable")


def _service(
    findings: tuple[Finding, ...],
    exceptions_reader: object | None,
) -> AssessmentReportApiService:
    kwargs: dict[str, object] = {"jobs": Jobs(), "reports": Reports(findings)}
    if exceptions_reader is not None:
        kwargs["exceptions"] = exceptions_reader
        kwargs["now"] = lambda: READ_AT
    return AssessmentReportApiService(**kwargs)  # type: ignore[arg-type]


class SuppressionWiringTests(unittest.TestCase):
    def test_in_force_exception_is_reported_as_a_suppression_note(self) -> None:
        service = _service((_finding(),), Exceptions((_exception(),)))

        report = service.get_assessment(_principal(), ASSESSMENT, limit=10, cursor=None)

        self.assertEqual(len(report.suppressions), 1)
        self.assertEqual(report.suppressions[0].finding_id, "finding-001")
        self.assertEqual(report.suppressions[0].exception_id, "exception-001")
        self.assertEqual(report.to_dict()["suppressions"][0]["exception_id"], "exception-001")

    def test_no_exception_leaves_the_report_unsuppressed(self) -> None:
        service = _service((_finding(),), Exceptions(()))

        report = service.get_assessment(_principal(), ASSESSMENT, limit=10, cursor=None)

        self.assertEqual(report.suppressions, ())

    def test_exceptions_gathered_per_finding_are_de_duplicated(self) -> None:
        # The same exception (rule/version scoped) comes back for two findings; it
        # must be collapsed to one before the predicate runs, and each covered
        # finding still gets its own note.
        reader = Exceptions((_exception(),))
        service = _service(
            (_finding(finding_id="finding-001"), _finding(finding_id="finding-002")),
            reader,
        )

        report = service.get_assessment(_principal(), ASSESSMENT, limit=10, cursor=None)

        self.assertEqual(reader.calls, ["finding-001", "finding-002"])
        self.assertEqual(
            {note.finding_id for note in report.suppressions}, {"finding-001", "finding-002"}
        )
        self.assertEqual({note.exception_id for note in report.suppressions}, {"exception-001"})

    def test_reader_fault_fails_toward_showing_the_violation(self) -> None:
        service = _service((_finding(),), FaultyExceptions())

        report = service.get_assessment(_principal(), ASSESSMENT, limit=10, cursor=None)

        self.assertEqual(report.suppressions, ())

    def test_no_exception_reader_wired_returns_the_report_unchanged(self) -> None:
        service = _service((_finding(),), None)

        report = service.get_assessment(_principal(), ASSESSMENT, limit=10, cursor=None)

        self.assertEqual(report.suppressions, ())

    def test_exceptions_without_a_clock_is_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "exceptions and now must be provided together"):
            AssessmentReportApiService(
                jobs=Jobs(), reports=Reports((_finding(),)), exceptions=Exceptions(())
            )

    def test_unknown_assessment_is_not_found(self) -> None:
        service = _service((_finding(),), Exceptions((_exception(),)))

        with self.assertRaises(JobNotFoundError):
            service.get_assessment(_principal(), "asm-unknown", limit=10, cursor=None)


if __name__ == "__main__":
    unittest.main()
