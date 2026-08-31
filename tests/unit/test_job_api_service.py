"""M0 Job API application-boundary tests without AWS clients."""

import unittest

from apps.backend.api.jobs import AssessmentRequest, JobApiService
from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.jobs import AssessmentScopeDenied, JobNotFoundError, create_job
from packages.contracts import JobCurrentStep


class InMemoryJobRepository:
    def __init__(self) -> None:
        self.jobs = {}
        self.assessments = {}
        self.outbox = {}

    def create_assessment_workflow(self, assessment, job, outbox) -> None:
        self.assessments[(assessment.customer_id, assessment.assessment_id)] = assessment
        self.jobs[(job.customer_id, job.job_id)] = job
        self.outbox[(outbox.customer_id, outbox.job_id)] = outbox

    def create_job(self, job) -> None:
        self.jobs[(job.customer_id, job.job_id)] = job

    def get_job(self, customer_id: str, job_id: str):
        return self.jobs.get((customer_id, job_id))

    def update_job(self, job, *, expected_revision: int) -> None:
        self.jobs[(job.customer_id, job.job_id)] = job


class ApprovedScope:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.calls = []

    def authorize(self, principal, *, repository_id: str, policy_profile_id: str) -> None:
        self.calls.append((principal, repository_id, policy_profile_id))
        if not self.approved:
            raise AssessmentScopeDenied("outside approved scope")


def principal(subject: str = "subject-001", customer_id: str = "cust-001") -> Principal:
    return Principal(
        subject=subject,
        client_id="client-001",
        customer_id=customer_id,
        roles=frozenset({Role.USER}),
    )


class JobApiServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryJobRepository()
        self.scope = ApprovedScope()
        self.service = JobApiService(
            repository=self.repository,
            assessment_scope=self.scope,
            job_id_factory=lambda: "job-001",
            assessment_id_factory=lambda: "asm-001",
        )

    def test_create_uses_jwt_customer_and_dispatches_only_internal_task_fields(self) -> None:
        response = self.service.create_assessment(
            principal(),
            AssessmentRequest(repository_id="repo-001", policy_profile_id="profile-001"),
        )

        self.assertEqual(response.job_id, "job-001")
        stored = self.repository.get_job("cust-001", "job-001")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.customer_id, "cust-001")
        self.assertEqual(stored.requested_by, "subject-001")
        self.assertEqual(stored.assessment_id, "asm-001")
        assessment = self.repository.assessments[("cust-001", "asm-001")]
        self.assertEqual(assessment.job_id, "job-001")
        self.assertEqual(assessment.repository_id, "repo-001")
        self.assertEqual(assessment.policy_profile_id, "profile-001")
        self.assertEqual(
            self.repository.outbox[("cust-001", "job-001")].task.to_dict(),
            {
                "job_id": "job-001",
                "expected_revision": 0,
                "command": "ASSESS_RESOURCE",
            },
        )

    def test_create_rejects_unapproved_selectors_before_persistence(self) -> None:
        self.scope.approved = False

        with self.assertRaises(AssessmentScopeDenied):
            self.service.create_assessment(
                principal(),
                AssessmentRequest(repository_id="repo-001", policy_profile_id="profile-001"),
            )

        self.assertEqual(self.repository.jobs, {})
        self.assertEqual(self.repository.assessments, {})
        self.assertEqual(self.repository.outbox, {})

    def test_get_reads_only_jwt_customer_partition_then_applies_owner_check(self) -> None:
        job = create_job(
            job_id="job-002",
            customer_id="cust-001",
            job_type="ASSESSMENT",
            initial_step=JobCurrentStep.LOAD_IAC,
            requested_by="subject-001",
        )
        self.repository.create_job(job)

        self.assertEqual(self.service.get_job(principal(), "job-002").job_id, "job-002")
        with self.assertRaises(AuthorizationDenied):
            self.service.get_job(principal(subject="subject-002"), "job-002")
        with self.assertRaises(JobNotFoundError):
            self.service.get_job(principal(customer_id="cust-002"), "job-002")
