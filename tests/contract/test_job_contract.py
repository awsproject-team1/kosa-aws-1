"""Contract tests for public workflow Job polling."""

import unittest

from packages.contracts import (
    ApiError,
    ApiErrorResponse,
    JobCurrentStep,
    JobResponse,
    JobStatus,
)


class JobContractTest(unittest.TestCase):
    def test_fixed_enums_match_the_documented_contract(self) -> None:
        self.assertEqual(
            {status.value for status in JobStatus},
            {
                "QUEUED",
                "RUNNING",
                "WAITING_REVIEW",
                "WAITING_APPROVAL",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
            },
        )
        self.assertEqual(
            [step.value for step in JobCurrentStep],
            [
                "LOAD_IAC",
                "LOAD_POLICY_PROFILE",
                "BUILD_EFFECTIVE_RULES",
                "LOAD_POLICY_EVIDENCE",
                "ASSESS",
                "POLICY_REVIEW",
                "GENERATE_FINDINGS",
                "GENERATE_REPORT",
                "GENERATE_REMEDIATION",
                "CREATE_PR",
                "CI_VALIDATION",
                "AWS_DISCOVERY",
                "PRE_DEPLOY_VALIDATION",
                "TERRAFORM_PLAN",
                "APPLY",
                "POST_DEPLOY_VERIFICATION",
            ],
        )

    def test_job_response_serializes_the_complete_polling_projection(self) -> None:
        response = JobResponse(
            job_id="job-001",
            job_type="ASSESSMENT",
            status=JobStatus.FAILED,
            current_step=JobCurrentStep.ASSESS,
            assessment_id="asm-001",
            error=ApiError(code="EXTERNAL_SERVICE_ERROR", message="Assessment failed"),
        )

        self.assertEqual(
            response.to_dict(),
            {
                "job_id": "job-001",
                "job_type": "ASSESSMENT",
                "status": "FAILED",
                "current_step": "ASSESS",
                "assessment_id": "asm-001",
                "remediation_id": None,
                "deployment_id": None,
                "error": {
                    "code": "EXTERNAL_SERVICE_ERROR",
                    "message": "Assessment failed",
                },
            },
        )

    def test_opaque_identifiers_and_job_type_must_be_non_empty(self) -> None:
        with self.assertRaisesRegex(ValueError, "job_type must be a non-empty string"):
            JobResponse(
                job_id="job-001",
                job_type=" ",
                status=JobStatus.QUEUED,
                current_step=JobCurrentStep.LOAD_IAC,
            )

        with self.assertRaisesRegex(ValueError, "assessment_id must be a non-empty string"):
            JobResponse(
                job_id="job-001",
                job_type="ASSESSMENT",
                status=JobStatus.RUNNING,
                current_step=JobCurrentStep.ASSESS,
                assessment_id="",
            )

    def test_fixed_values_require_the_contract_enum_types(self) -> None:
        with self.assertRaisesRegex(TypeError, "status must be a JobStatus"):
            JobResponse(
                job_id="job-001",
                job_type="ASSESSMENT",
                status="QUEUED",
                current_step=JobCurrentStep.LOAD_IAC,
            )

        with self.assertRaisesRegex(TypeError, "current_step must be a JobCurrentStep"):
            JobResponse(
                job_id="job-001",
                job_type="ASSESSMENT",
                status=JobStatus.QUEUED,
                current_step="LOAD_IAC",
            )

    def test_job_error_rejects_the_top_level_api_envelope(self) -> None:
        with self.assertRaisesRegex(TypeError, "error must be an ApiError or None"):
            JobResponse(
                job_id="job-001",
                job_type="ASSESSMENT",
                status=JobStatus.FAILED,
                current_step=JobCurrentStep.ASSESS,
                error=ApiErrorResponse(
                    error=ApiError(code="INTERNAL_ERROR", message="Assessment failed")
                ),
            )


if __name__ == "__main__":
    unittest.main()
