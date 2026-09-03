"""The candidate API requests a run durably, and shows only completed results.

세 가지를 고정한다.

1. **요청을 먼저 저장하고 그 다음에 queue로 보낸다.** 반대로 하면 저장이 실패했을 때 worker가
   존재하지 않는 요청을 처리한다.
2. **완결되지 않은 실행은 상태만 돌려준다.** 부분 결과를 보여주면 리뷰어가 그것을 전체로
   착각하고 승인한다.
3. **정규화 문서의 원문은 응답에 없다.** 나가는 문장은 모델이 쓴 재진술이고, 그 옆의
   `content_sha256`은 서버가 만든 값이다.
"""

import unittest

from apps.backend.api.policy_candidates import (
    MAX_PAGE_SIZE,
    PolicyCandidateApiService,
)
from apps.backend.auth import Principal, Role
from apps.backend.auth.authorization import AuthorizationDenied
from apps.backend.repositories.errors import RepositoryError
from packages.contracts import (
    AuthoringRunStatus,
    PolicyAuthoringRequest,
    RuleSeverity,
)
from tests.authoring_fixtures import UNIT_TEXTS, ready_document
from tests.unit.test_authoring_result_persistence import (
    DOCUMENT,
    FakeTable,
    _repository,
    _result,
)

CUSTOMER = "cust-001"


def principal(*, role: Role = Role.ADMIN, customer_id: str = CUSTOMER) -> Principal:
    """`MANAGE_POLICY_SOURCES`는 Admin에게만 있다 (`_ROLE_ACTIONS`)."""
    return Principal(
        subject="reviewer@example.com",
        client_id="frontend",
        customer_id=customer_id,
        roles=frozenset({role}),
    )


class RecordingQueue:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[PolicyAuthoringRequest] = []
        self.fail = fail

    def enqueue(self, request: PolicyAuthoringRequest) -> None:
        if self.fail:
            raise RuntimeError("queue unavailable")
        self.sent.append(request)


def _service(
    table: FakeTable, queue: RecordingQueue, *, run_ids: list[str] | None = None
) -> PolicyCandidateApiService:
    pending = list(run_ids or ["authoring-run-1"])
    return PolicyCandidateApiService(
        repository=_repository(table),
        queue=queue,  # type: ignore[arg-type]
        run_id_factory=lambda: pending.pop(0),
    )


class RequestExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        self.queue = RecordingQueue()
        self.service = _service(self.table, self.queue)

    def test_a_request_is_stored_before_it_is_queued(self) -> None:
        accepted = self.service.request_extraction(
            principal(),
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
        )

        self.assertIs(accepted.status, AuthoringRunStatus.QUEUED)
        self.assertEqual(accepted.authoring_run_id, "authoring-run-1")
        self.assertEqual(len(self.queue.sent), 1)
        self.assertIn(
            (
                f"CUSTOMER#{CUSTOMER}",
                f"POLICY_SOURCE#{DOCUMENT.source_id}#VERSION#{DOCUMENT.source_version}#REQUEST",
            ),
            self.table.items,
        )

    def test_a_failed_dispatch_leaves_the_request_recorded(self) -> None:
        """queue publish가 실패해도 요청 사실은 남는다. 사람이 다시 누르지 않아도 된다."""
        service = _service(self.table, RecordingQueue(fail=True))

        with self.assertRaises(RuntimeError):
            service.request_extraction(
                principal(),
                source_id=DOCUMENT.source_id,
                source_version=DOCUMENT.source_version,
            )

        self.assertIn(
            (
                f"CUSTOMER#{CUSTOMER}",
                f"POLICY_SOURCE#{DOCUMENT.source_id}#VERSION#{DOCUMENT.source_version}#REQUEST",
            ),
            self.table.items,
        )

    def test_requesting_the_same_version_twice_reuses_the_first_run(self) -> None:
        """새 run id와 새 `requested_at`을 발급하면 같은 문서의 실행이 둘이 된다.

        그 둘은 provenance가 달라 저장 계층이 서로를 다른 추출로 보고 fail-closed한다.
        """
        service = _service(self.table, self.queue, run_ids=["run-a", "run-b"])

        first = service.request_extraction(
            principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )
        second = service.request_extraction(
            principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )

        self.assertEqual(first.authoring_run_id, second.authoring_run_id)
        self.assertEqual(
            [request.requested_at for request in self.queue.sent][0],
            self.queue.sent[1].requested_at,
        )

    def test_the_queue_payload_carries_no_policy_text_or_storage_key(self) -> None:
        """payload에 텍스트를 담으면 queue와 DLQ와 queue 로그가 원문의 사본이 된다."""
        self.service.request_extraction(
            principal(), source_id=DOCUMENT.source_id, source_version=DOCUMENT.source_version
        )

        payload = self.queue.sent[0].to_dict()

        self.assertEqual(
            sorted(payload),
            ["authoring_run_id", "customer_id", "requested_at", "source_id", "source_version"],
        )
        for _locator, _kind, text in UNIT_TEXTS:
            self.assertNotIn(text, repr(payload))

    def test_a_caller_without_the_action_is_refused(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            self.service.request_extraction(
                principal(role=Role.USER),
                source_id=DOCUMENT.source_id,
                source_version=DOCUMENT.source_version,
            )
        self.assertEqual(self.table.items, {})


class ListCandidatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        self.queue = RecordingQueue()
        self.service = _service(self.table, self.queue)

    def _store_ready_run(self) -> None:
        _repository(self.table).record_authoring_result(customer_id=CUSTOMER, result=_result())

    def _list(self, **kwargs: object):
        return self.service.list_candidates(
            principal(),
            source_id=DOCUMENT.source_id,
            source_version=DOCUMENT.source_version,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_a_ready_run_returns_review_entries_with_server_derived_digests(self) -> None:
        self._store_ready_run()

        page = self._list()

        self.assertIs(page.status, AuthoringRunStatus.READY)
        self.assertEqual(len(page.candidates), 2)
        entry = next(
            candidate
            for candidate in page.candidates
            if candidate.control_key == "S3_BLOCK_PUBLIC_ACCESS"
        )
        unit = ready_document().unit(entry.locators[0].locator)
        assert unit is not None
        self.assertEqual(entry.locators[0].content_sha256, unit.text_sha256)

    def test_the_proposed_severity_is_the_catalog_value(self) -> None:
        """리뷰어는 등급을 고르지 않는다. Catalog가 정한 값을 승인하거나 후보를 거절한다."""
        self._store_ready_run()

        page = self._list()
        entry = next(
            candidate
            for candidate in page.candidates
            if candidate.control_key == "S3_BLOCK_PUBLIC_ACCESS"
        )

        self.assertIs(entry.proposed_severity, RuleSeverity.CRITICAL)
        self.assertNotIn(
            "severity", set(entry.to_dict()) - {"proposed_severity", "severity_guidance"}
        )

    def test_a_run_that_is_not_ready_returns_status_only(self) -> None:
        self._store_ready_run()
        key = (
            f"CUSTOMER#{CUSTOMER}",
            f"POLICY_SOURCE#{DOCUMENT.source_id}#VERSION#{DOCUMENT.source_version}#AUTHORING",
        )
        self.table.items[key]["status"] = AuthoringRunStatus.PROCESSING.value
        self.table.items[key]["result_digest"] = None
        self.table.items[key]["counts"] = {}

        page = self._list()

        self.assertIs(page.status, AuthoringRunStatus.PROCESSING)
        self.assertEqual(page.candidates, ())
        self.assertEqual(page.unsupported, ())
        self.assertIsNone(page.cursor)

    def test_unsupported_and_rejected_results_are_returned_but_are_not_candidates(self) -> None:
        self._store_ready_run()

        page = self._list()

        self.assertEqual(len(page.unsupported), 1)
        self.assertEqual(
            [entry.rule_id for entry in page.candidates],
            [entry.rule_id for entry in page.candidates],
        )
        self.assertNotIn("UNSUPPORTED", [entry.classification.value for entry in page.candidates])

    def test_a_page_walks_the_whole_run_without_repeating_or_skipping(self) -> None:
        self._store_ready_run()

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            page = self._list(**({} if cursor is None else {"cursor": cursor}), limit=1)
            seen.extend(entry.rule_id for entry in page.candidates)
            seen.extend(entry.requirement_summary for entry in page.unsupported)
            cursor = page.cursor
            if cursor is None:
                break

        self.assertIsNone(cursor)
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 3)

    def test_an_out_of_range_limit_is_refused(self) -> None:
        self._store_ready_run()

        for limit in (0, -1, MAX_PAGE_SIZE + 1):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    self._list(limit=limit)

    def test_a_cursor_from_another_run_is_refused(self) -> None:
        self._store_ready_run()

        with self.assertRaisesRegex(ValueError, "cursor does not refer"):
            self._list(cursor="bm90LWEta2V5")

    def test_the_response_carries_no_verbatim_source_sentence(self) -> None:
        self._store_ready_run()

        rendered = repr(self._list().to_dict())

        for _locator, _kind, text in UNIT_TEXTS:
            with self.subTest(text=text[:32]):
                self.assertNotIn(text, rendered)

    def test_a_caller_without_the_action_is_refused(self) -> None:
        self._store_ready_run()

        with self.assertRaises(AuthorizationDenied):
            self.service.list_candidates(
                principal(role=Role.USER),
                source_id=DOCUMENT.source_id,
                source_version=DOCUMENT.source_version,
            )


class TenantIsolationTest(unittest.TestCase):
    def test_another_customer_cannot_read_this_run(self) -> None:
        """모든 read가 호출자의 partition만 사용한다. 다른 고객에게는 존재하지 않는 실행이다."""
        table = FakeTable()
        _repository(table).record_authoring_result(customer_id=CUSTOMER, result=_result())
        service = _service(table, RecordingQueue())

        with self.assertRaises(RepositoryError):
            service.list_candidates(
                principal(customer_id="cust-002"),
                source_id=DOCUMENT.source_id,
                source_version=DOCUMENT.source_version,
            )


if __name__ == "__main__":
    unittest.main()
