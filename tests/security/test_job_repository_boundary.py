"""Security tests for Job ownership and provider-error sanitation."""

import unittest

from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.jobs import authorize_job_read, create_job, sanitize_public_error
from apps.backend.repositories import DynamoDbJobRepository, RepositoryError
from packages.contracts import JobCurrentStep


class ProviderError(Exception):
    def __init__(self) -> None:
        super().__init__("AKIAEXAMPLE provider-secret table-name request-123")
        self.response = {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "AKIAEXAMPLE provider-secret table-name request-123",
            },
            "ResponseMetadata": {"RequestId": "request-123"},
        }


class FailingTable:
    def put_item(self, **kwargs: object) -> object:
        raise ProviderError

    def get_item(self, **kwargs: object) -> dict[str, object]:
        raise ProviderError


def job():
    return create_job(
        job_id="job-001",
        customer_id="cust-001",
        job_type="ASSESSMENT",
        initial_step=JobCurrentStep.LOAD_IAC,
        requested_by="owner-subject",
    )


class JobRepositoryBoundarySecurityTest(unittest.TestCase):
    def test_user_cannot_read_another_subjects_job(self) -> None:
        other_user = Principal(
            subject="other-subject",
            client_id="client-001",
            customer_id="cust-001",
            roles=frozenset({Role.USER}),
        )

        with self.assertRaises(AuthorizationDenied):
            authorize_job_read(other_user, job())

    def test_provider_details_never_enter_public_errors(self) -> None:
        repository = DynamoDbJobRepository(FailingTable())

        for operation in (
            lambda: repository.create_job(job()),
            lambda: repository.get_job("cust-001", "job-001"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(RepositoryError) as caught:
                    operation()
                public = sanitize_public_error(caught.exception).to_dict()
                serialized = str(public)
                self.assertEqual(public["code"], "EXECUTION_ERROR")
                for secret in (
                    "AKIAEXAMPLE",
                    "provider-secret",
                    "table-name",
                    "request-123",
                ):
                    self.assertNotIn(secret, serialized)

    def test_unknown_exception_uses_a_fixed_internal_error(self) -> None:
        public = sanitize_public_error(
            RuntimeError("password=hunter2 bucket-name stack trace")
        ).to_dict()

        self.assertEqual(
            public,
            {"code": "EXECUTION_ERROR", "message": "An internal error occurred"},
        )


if __name__ == "__main__":
    unittest.main()
