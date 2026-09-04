"""`GET /remediations/{remediationId}` reads the stored decision, result, and PR — nothing more."""

import json
import unittest
from dataclasses import replace

from apps.backend.api.handler import JobHttpHandler
from apps.backend.api.jobs import JobApiService
from apps.backend.api.remediation_reads import (
    RemediationNotFoundError,
    RemediationReadApiService,
    RemediationView,
)
from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.jobs import JobNotFoundError, OutboxDispatcher, create_job
from apps.backend.repositories import StoredDataError
from apps.backend.repositories.remediation_read import DynamoDbRemediationReadRepository
from packages.contracts import JobCurrentStep

CUSTOMER = "cust-001"
REMEDIATION_ID = "rem-001"
JOB_ID = "job-001"


def _item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "PK": f"CUSTOMER#{CUSTOMER}",
        "SK": f"REMEDIATION#{REMEDIATION_ID}",
        "entity_type": "REMEDIATION",
        "customer_id": CUSTOMER,
        "remediation_id": REMEDIATION_ID,
        "finding_id": "finding-abc",
        "status": "QUEUED",
        "decided_at": "2026-09-04T00:00:00+00:00",
        "job_id": JOB_ID,
        "decision": {
            "finding_id": "finding-abc",
            "resource_id": "db-001",
            "rule_id": "RDS-PUBLIC-001",
            "rule_version": "2026-09-03",
            "perspective": "IAC",
            "action": "TERRAFORM_PATCH",
            "manual_review_code": None,
            "exception_id": None,
        },
        "context": {"snapshot": {"customer_id": CUSTOMER}},
        "result": {
            "kind": "TERRAFORM_PATCH",
            "patch": {
                "finding_id": "finding-abc",
                "base_commit_sha": "a" * 40,
                "artifact": {"content_sha256": "d" * 64},
                "changed_paths": ["database.tf"],
            },
        },
        "pull_request": {"number": 7, "url": "https://github.example/acme/iac/pull/7"},
    }
    item.update(overrides)
    return item


class Table:
    def __init__(self, item: dict[str, object] | None) -> None:
        self.item = item
        self.keys: list[dict[str, object]] = []

    def get_item(self, **kwargs: object) -> dict[str, object]:
        self.keys.append(kwargs["Key"])  # type: ignore[arg-type]
        return {} if self.item is None else {"Item": self.item}


class Jobs:
    def __init__(self, job) -> None:
        self.job = job

    def get_job(self, customer_id: str, job_id: str):
        return self.job if self.job is not None and job_id == self.job.job_id else None


def _principal(*, roles=frozenset({Role.USER}), subject="user-1", customer=CUSTOMER) -> Principal:
    return Principal(subject=subject, client_id="client", customer_id=customer, roles=roles)


def _job(requested_by: str = "user-1"):
    return replace(
        create_job(
            job_id=JOB_ID,
            customer_id=CUSTOMER,
            job_type="REMEDIATION",
            initial_step=JobCurrentStep.GENERATE_REMEDIATION,
            requested_by=requested_by,
        ),
        remediation_id=REMEDIATION_ID,
    )


class RepositoryTest(unittest.TestCase):
    def test_reads_the_item_inside_the_customer_partition(self) -> None:
        table = Table(_item())
        view = DynamoDbRemediationReadRepository(table).get_remediation(
            customer_id=CUSTOMER, remediation_id=REMEDIATION_ID
        )
        self.assertEqual(table.keys[0]["PK"], f"CUSTOMER#{CUSTOMER}")
        self.assertEqual(view.finding_id, "finding-abc")
        self.assertEqual(view.result["patch"]["changed_paths"], ["database.tf"])  # type: ignore[index]
        self.assertEqual(view.pull_request["number"], 7)  # type: ignore[index]
        self.assertEqual(view.to_dict()["decision"]["action"], "TERRAFORM_PATCH")

    def test_a_missing_item_is_not_found(self) -> None:
        with self.assertRaises(RemediationNotFoundError):
            DynamoDbRemediationReadRepository(Table(None)).get_remediation(
                customer_id=CUSTOMER, remediation_id=REMEDIATION_ID
            )

    def test_a_result_and_pull_request_may_still_be_absent(self) -> None:
        item = _item()
        del item["result"]
        del item["pull_request"]
        view = DynamoDbRemediationReadRepository(Table(item)).get_remediation(
            customer_id=CUSTOMER, remediation_id=REMEDIATION_ID
        )
        self.assertIsNone(view.result)
        self.assertIsNone(view.pull_request)

    def test_another_customers_item_is_refused(self) -> None:
        with self.assertRaises(StoredDataError):
            DynamoDbRemediationReadRepository(Table(_item(customer_id="other"))).get_remediation(
                customer_id=CUSTOMER, remediation_id=REMEDIATION_ID
            )


class ServiceTest(unittest.TestCase):
    _PRESENT = object()

    def _service(self, item=_PRESENT, job=None) -> RemediationReadApiService:
        stored = _item() if item is self._PRESENT else item
        return RemediationReadApiService(
            jobs=Jobs(job),
            remediations=DynamoDbRemediationReadRepository(Table(stored)),
        )

    def test_the_job_owner_reads_their_remediation(self) -> None:
        view = self._service(job=_job()).get_remediation(_principal(), REMEDIATION_ID)
        self.assertIsInstance(view, RemediationView)
        self.assertEqual(view.job_id, JOB_ID)

    def test_another_user_cannot_read_a_job_backed_remediation(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            self._service(job=_job(requested_by="someone-else")).get_remediation(
                _principal(), REMEDIATION_ID
            )

    def test_an_admin_reads_any_remediation_in_the_partition(self) -> None:
        view = self._service(job=_job(requested_by="someone-else")).get_remediation(
            _principal(roles=frozenset({Role.ADMIN}), subject="admin"), REMEDIATION_ID
        )
        self.assertEqual(view.remediation_id, REMEDIATION_ID)

    def test_a_job_that_names_another_remediation_is_not_found(self) -> None:
        job = replace(_job(), remediation_id="rem-other")
        with self.assertRaises(JobNotFoundError):
            self._service(job=job).get_remediation(_principal(), REMEDIATION_ID)

    def test_a_decision_without_a_job_is_readable_by_the_customer(self) -> None:
        item = _item(status="DECIDED_NO_ACTION")
        del item["job_id"]
        del item["result"]
        del item["pull_request"]
        view = self._service(item=item).get_remediation(_principal(), REMEDIATION_ID)
        self.assertIsNone(view.job_id)

    def test_missing_remediation_maps_to_not_found(self) -> None:
        with self.assertRaises(JobNotFoundError):
            self._service(item=None).get_remediation(_principal(), REMEDIATION_ID)


class HandlerTest(unittest.TestCase):
    def _handler(self, reads) -> JobHttpHandler:
        class Repository:
            def get_job(self, customer_id, job_id):
                return None

            def create_assessment_workflow(self, *args):
                return None

            def mark_outbox_dispatched(self, entry):
                return None

            def record_outbox_dispatch_failure(self, entry):
                return None

        class Scope:
            def authorize(self, principal, *, repository_id):
                return None

        class Catalog:
            def __call__(self, *, customer_id):
                return self

            def get_profile(self, policy_profile_id, version=None):
                return None

        class Dispatcher:
            def dispatch(self, task):
                return None

        repository = Repository()
        service = JobApiService(
            repository=repository,
            assessment_scope=Scope(),
            policy_catalog_factory=Catalog(),
            outbox_dispatcher=OutboxDispatcher(repository=repository, dispatcher=Dispatcher()),
            job_id_factory=lambda: "job-x",
            assessment_id_factory=lambda: "asm-x",
        )
        return JobHttpHandler(service, remediation_reads=reads)

    @staticmethod
    def _event(path: str) -> dict[str, object]:
        return {
            "rawPath": path,
            "body": None,
            "requestContext": {
                "http": {"method": "GET"},
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "token_use": "access",
                            "sub": "user-1",
                            "client_id": "client-001",
                            "custom:customer_id": CUSTOMER,
                            "cognito:groups": ["Admin"],
                        }
                    }
                },
            },
        }

    def test_the_route_returns_the_stored_view(self) -> None:
        reads = RemediationReadApiService(
            jobs=Jobs(_job()), remediations=DynamoDbRemediationReadRepository(Table(_item()))
        )
        response = self._handler(reads).handle(self._event(f"/remediations/{REMEDIATION_ID}"))
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["pull_request"]["url"], "https://github.example/acme/iac/pull/7")
        self.assertEqual(body["result"]["patch"]["changed_paths"], ["database.tf"])

    def test_an_unknown_remediation_is_404(self) -> None:
        reads = RemediationReadApiService(
            jobs=Jobs(None), remediations=DynamoDbRemediationReadRepository(Table(None))
        )
        response = self._handler(reads).handle(self._event("/remediations/rem-missing"))
        self.assertEqual(response["statusCode"], 404)

    def test_the_route_is_absent_without_the_service(self) -> None:
        response = self._handler(None).handle(self._event(f"/remediations/{REMEDIATION_ID}"))
        self.assertEqual(response["statusCode"], 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
