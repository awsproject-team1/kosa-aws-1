"""What the Authoring Worker does with a request whose document is no longer there.

삭제는 큐를 되돌리지 않는다. 사용자가 문서를 지워도 이미 발행된 추출 요청은 큐에 남아 있고,
worker는 그것을 언젠가 집어 든다. 그때 예외를 올리면 SQS가 재시도하고, 재시도해도 문서는 돌아
오지 않으므로 결국 DLQ에 쌓인다. 라이브 sandbox에서 실제로 그렇게 됐다 — 지워진 판본을 가리키는
메시지가 DLQ를 채워 큐 상태를 읽을 수 없었다.

없어진 문서에 대한 추출은 실패가 아니라 할 일이 없는 것이다. 같은 배치의 나머지 요청은 그대로
처리되어야 한다.
"""

import json
import unittest
from dataclasses import replace

from apps.backend.policy.authoring import FakePolicyCandidateExtractor
from apps.backend.policy.authoring.runtime import run_requests
from apps.backend.policy.control_catalog import MVP_CONTROL_CATALOG
from packages.common.errors import PolicySourceNotFound
from packages.contracts import (
    AuthoringManifest,
    AuthoringRunStatus,
    ExtractionWarningCode,
    IngestionStatus,
    PolicyAuthoringResult,
)
from tests.authoring_fixtures import ready_document
from tests.unit.test_policy_authoring_pipeline import artifact_reader

DOCUMENT = ready_document()
CUSTOMER = "cust-001"


def _record(source_id: str, source_version: str, run_id: str) -> dict[str, object]:
    return {
        "body": json.dumps(
            {
                "customer_id": CUSTOMER,
                "source_id": source_id,
                "source_version": source_version,
                "authoring_run_id": run_id,
                "requested_at": "2026-09-04T00:00:00+00:00",
            }
        )
    }


class Documents:
    """`get_document`는 지워진 판본에 대해 `LookupError`를 올린다 (repository 계약)."""

    def __init__(self, *, present: tuple[str, ...]) -> None:
        self._present = present
        self.asked: list[str] = []

    def get_document(self, *, customer_id: str, source_id: str, source_version: str):
        self.asked.append(source_version)
        if source_version not in self._present:
            raise PolicySourceNotFound("policy source version not found")
        return DOCUMENT


class Repository:
    def __init__(self) -> None:
        self.recorded: list[PolicyAuthoringResult] = []

    def record_authoring_result(
        self, *, customer_id: str, result: PolicyAuthoringResult
    ) -> AuthoringManifest:
        self.recorded.append(result)
        return AuthoringManifest(
            source_id=result.document.source_id,
            source_version=result.document.source_version,
            normalized_sha256=result.document.normalized_sha256,
            status=AuthoringRunStatus.READY,
            provenance=result.provenance,
            result_digest="digest-" + result.provenance.authoring_run_id,
            # 결과가 세는 것을 그대로 쓴다. 여기서 key를 손으로 나열하면 Contract가 세는
            # 항목이 늘 때마다 가짜만 조용히 뒤처진다.
            counts=result.counts,
        )


def _run(documents: Documents, repository: Repository, records: list[dict[str, object]]):
    return run_requests(
        {"Records": records},
        documents=documents,
        repository=repository,
        extractor=FakePolicyCandidateExtractor(),
        artifact_reader=artifact_reader(),
        catalog=MVP_CONTROL_CATALOG,
    )


class DeletedSourceTest(unittest.TestCase):
    def test_a_request_for_a_deleted_version_is_skipped_rather_than_retried(self) -> None:
        documents = Documents(present=())
        repository = Repository()

        manifests = _run(documents, repository, [_record(DOCUMENT.source_id, "gone", "run-1")])

        self.assertEqual(manifests, ())
        self.assertEqual(repository.recorded, [])

    def test_the_rest_of_the_batch_still_runs(self) -> None:
        """한 메시지의 문서가 없다고 같은 배치의 다른 요청까지 재시도로 넘기지 않는다."""
        documents = Documents(present=(DOCUMENT.source_version,))
        repository = Repository()

        manifests = _run(
            documents,
            repository,
            [
                _record(DOCUMENT.source_id, "gone", "run-1"),
                _record(DOCUMENT.source_id, DOCUMENT.source_version, "run-2"),
            ],
        )

        self.assertEqual(len(manifests), 1)
        self.assertEqual(len(repository.recorded), 1)
        self.assertEqual(repository.recorded[0].provenance.authoring_run_id, "run-2")
        self.assertEqual(documents.asked, ["gone", DOCUMENT.source_version])


class AwaitingReviewTest(unittest.TestCase):
    """`REVIEW_REQUIRED`도 재시도로는 바뀌지 않는다 — 사람의 확인은 API로만 온다.

    라이브에서 ISMS-P 엑셀(334 unit, 병합 셀 경고)이 그 상태로 멈췄고, worker가 16분마다
    `SOURCE_NOT_READY`로 실패하며 5시간 넘게 DLQ를 채웠다.
    """

    class PendingDocuments(Documents):
        def get_document(self, *, customer_id: str, source_id: str, source_version: str):
            self.asked.append(source_version)
            return replace(
                DOCUMENT,
                status=IngestionStatus.REVIEW_REQUIRED,
                warnings=(ExtractionWarningCode.MERGED_CELLS_EXPANDED,),
            )

    def test_a_document_awaiting_review_is_skipped_rather_than_retried(self) -> None:
        documents = self.PendingDocuments(present=(DOCUMENT.source_version,))
        repository = Repository()

        manifests = _run(
            documents, repository, [_record(DOCUMENT.source_id, DOCUMENT.source_version, "run-1")]
        )

        self.assertEqual(manifests, ())
        self.assertEqual(repository.recorded, [])

    def test_a_confirmed_document_is_extracted_normally(self) -> None:
        """확인이 끝나면 같은 요청을 다시 보내 추출이 진행된다."""
        documents = Documents(present=(DOCUMENT.source_version,))
        repository = Repository()

        manifests = _run(
            documents, repository, [_record(DOCUMENT.source_id, DOCUMENT.source_version, "run-1")]
        )

        self.assertEqual(len(manifests), 1)


if __name__ == "__main__":
    unittest.main()
