"""Unit tests for the deterministic Job lifecycle and ownership boundary."""

import unittest

from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.jobs import (
    InvalidJobTransition,
    StaleJobRevision,
    authorize_job_read,
    create_job,
    transition_job,
)
from packages.contracts import ApiError, JobCurrentStep, JobStatus


def queued_job():
    return create_job(
        job_id="job-001",
        customer_id="cust-001",
        job_type="ASSESSMENT",
        initial_step=JobCurrentStep.LOAD_IAC,
        requested_by="subject-001",
    )


def principal(subject: str, role: Role) -> Principal:
    return Principal(
        subject=subject,
        client_id="client-001",
        customer_id="cust-001",
        roles=frozenset({role}),
    )


class JobLifecycleTest(unittest.TestCase):
    def test_creation_requires_an_explicit_step_and_starts_at_revision_zero(self) -> None:
        job = queued_job()

        self.assertEqual(job.status, JobStatus.QUEUED)
        self.assertEqual(job.current_step, JobCurrentStep.LOAD_IAC)
        self.assertEqual(job.revision, 0)
        self.assertEqual(job.requested_by, "subject-001")

    def test_approved_review_and_approval_path_increments_every_revision(self) -> None:
        job = queued_job()
        transitions = [
            (JobStatus.RUNNING, JobCurrentStep.ASSESS),
            (JobStatus.WAITING_REVIEW, JobCurrentStep.POLICY_REVIEW),
            (JobStatus.RUNNING, JobCurrentStep.GENERATE_FINDINGS),
            (JobStatus.WAITING_APPROVAL, JobCurrentStep.TERRAFORM_PLAN),
            (JobStatus.RUNNING, JobCurrentStep.APPLY),
            (JobStatus.COMPLETED, JobCurrentStep.POST_DEPLOY_VERIFICATION),
        ]

        for revision, (status, step) in enumerate(transitions, start=1):
            job = transition_job(
                job,
                expected_revision=job.revision,
                status=status,
                current_step=step,
            )
            self.assertEqual(job.revision, revision)
            self.assertEqual(job.status, status)
            self.assertEqual(job.current_step, step)

    def test_running_progress_update_is_allowed(self) -> None:
        running = transition_job(
            queued_job(),
            expected_revision=0,
            status=JobStatus.RUNNING,
            current_step=JobCurrentStep.ASSESS,
        )

        progressed = transition_job(
            running,
            expected_revision=1,
            status=JobStatus.RUNNING,
            current_step=JobCurrentStep.GENERATE_FINDINGS,
        )

        self.assertEqual(progressed.revision, 2)
        self.assertEqual(progressed.current_step, JobCurrentStep.GENERATE_FINDINGS)

    def test_invalid_and_terminal_transitions_are_rejected(self) -> None:
        with self.assertRaises(InvalidJobTransition):
            transition_job(
                queued_job(),
                expected_revision=0,
                status=JobStatus.COMPLETED,
            )

        for terminal in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            running = transition_job(
                queued_job(),
                expected_revision=0,
                status=JobStatus.RUNNING,
            )
            error = ApiError(code="INTERNAL_ERROR", message="Job failed")
            terminal_job = transition_job(
                running,
                expected_revision=1,
                status=terminal,
                error=error if terminal is JobStatus.FAILED else None,
            )
            with self.subTest(terminal=terminal):
                with self.assertRaises(InvalidJobTransition):
                    transition_job(
                        terminal_job,
                        expected_revision=2,
                        status=JobStatus.RUNNING,
                    )

    def test_failed_state_requires_a_public_error_and_other_states_reject_it(self) -> None:
        with self.assertRaisesRegex(ValueError, "FAILED jobs require an ApiError"):
            transition_job(
                queued_job(),
                expected_revision=0,
                status=JobStatus.FAILED,
            )

        with self.assertRaisesRegex(InvalidJobTransition, "only FAILED"):
            transition_job(
                queued_job(),
                expected_revision=0,
                status=JobStatus.RUNNING,
                error=ApiError(code="INTERNAL_ERROR", message="Job failed"),
            )

    def test_stale_revision_is_rejected_before_state_change(self) -> None:
        with self.assertRaises(StaleJobRevision):
            transition_job(
                queued_job(),
                expected_revision=1,
                status=JobStatus.RUNNING,
            )

    def test_domain_identifiers_are_write_once(self) -> None:
        running = transition_job(
            queued_job(),
            expected_revision=0,
            status=JobStatus.RUNNING,
            assessment_id="asm-001",
        )
        same_link = transition_job(
            running,
            expected_revision=1,
            status=JobStatus.RUNNING,
            assessment_id="asm-001",
        )

        self.assertEqual(same_link.assessment_id, "asm-001")
        with self.assertRaisesRegex(InvalidJobTransition, "assessment_id is write-once"):
            transition_job(
                same_link,
                expected_revision=2,
                status=JobStatus.RUNNING,
                assessment_id="asm-002",
            )

    def test_public_projection_omits_owner_and_revision(self) -> None:
        response = queued_job().to_response().to_dict()

        self.assertNotIn("requested_by", response)
        self.assertNotIn("revision", response)
        self.assertEqual(response["job_id"], "job-001")

    def test_user_reads_only_owned_jobs_and_admin_reads_every_job(self) -> None:
        job = queued_job()

        self.assertIsNone(authorize_job_read(principal("subject-001", Role.USER), job))
        self.assertIsNone(authorize_job_read(principal("admin-001", Role.ADMIN), job))
        with self.assertRaises(AuthorizationDenied):
            authorize_job_read(principal("subject-002", Role.USER), job)


if __name__ == "__main__":
    unittest.main()
