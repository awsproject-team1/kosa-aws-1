"""Approval/Profile DynamoDB writes keep bindings and audit events atomic."""

import unittest
from datetime import UTC, datetime

from apps.backend.repositories.errors import RepositoryError
from apps.backend.repositories.policy_approval import DynamoDbPolicyApprovalRepository
from packages.contracts import (
    DocumentUnitKind,
    IngestionStatus,
    NormalizedDocumentUnit,
    NormalizedPolicyDocument,
    PolicyCandidateExtraction,
    PolicyProfile,
    PolicyRule,
    PolicyRuleReference,
    PolicySourceApproval,
    PolicySourceFormat,
    RuleCandidate,
    RuleSeverity,
    SourceReference,
)
from packages.contracts.assessments import AssessmentPhase


class Client:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def transact_write_items(self, **kwargs: object) -> None:
        self.requests.append(kwargs)


def approval() -> PolicySourceApproval:
    return PolicySourceApproval(
        source_id="source-1",
        source_version="v1",
        artifact_id="original-1",
        s3_version_id="s3-v1",
        content_sha256="original-sha",
        normalized_artifact_id="normalized-1",
        normalized_sha256="normalized-sha",
        approved_rules=(PolicyRuleReference(rule_id="RULE-1", version="v1"),),
        approved_by="admin",
        approved_at="2026-09-01T00:00:00Z",
    )


def extraction() -> PolicyCandidateExtraction:
    """READY 정규화 문서 하나와 그 문서를 인용하는 후보 하나로 추출 결과를 만든다."""
    unit = NormalizedDocumentUnit(
        locator="section-1",
        kind=DocumentUnitKind.SECTION,
        text_sha256="unit-sha",
        text_length=42,
        origin="markdown",
    )
    document = NormalizedPolicyDocument(
        source_id="source-1",
        source_version="v1",
        artifact_id="original-1",
        s3_version_id="s3-v1",
        content_sha256="original-sha",
        filename="policy.md",
        declared_media_type="text/markdown",
        byte_size=42,
        status=IngestionStatus.READY,
        detected_media_type="text/markdown",
        source_format=PolicySourceFormat.MARKDOWN,
        parser_id="markdown",
        parser_version="1",
        normalized_artifact_id="original-1#normalized",
        normalized_sha256="normalized-sha",
        units=(unit,),
    )
    rule = PolicyRule(
        rule_id="RULE-1",
        version="v1",
        title="Rule",
        severity=RuleSeverity.HIGH,
        applicable_phases=(AssessmentPhase.INITIAL,),
        resource_types=("AWS::S3::Bucket",),
        source_references=(
            SourceReference(
                source_id="source-1",
                source_version="v1",
                locator="section-1",
                content_sha256="unit-sha",
            ),
        ),
    )
    return PolicyCandidateExtraction(
        document=document,
        candidates=(RuleCandidate(rule=rule),),
        extractor_id="extractor",
        extractor_version="1",
    )


class DynamoDbPolicyApprovalRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client()
        self.repository = DynamoDbPolicyApprovalRepository(
            table_name="metadata",
            transaction_client=self.client,
            now=lambda: datetime(2026, 9, 1, tzinfo=UTC),
            id_factory=lambda: "id-1",
        )

    def test_approval_condition_checks_the_exact_finalized_ingestion_binding(self) -> None:
        self.repository.record_approval(customer_id="cust-a", approval=approval(), candidates=())

        writes = self.client.requests[0]["TransactItems"]
        assert isinstance(writes, list)
        condition = writes[0]["ConditionCheck"]
        self.assertEqual(condition["Key"]["PK"], {"S": "CUSTOMER#cust-a"})
        self.assertIn("s3_version_id", condition["ConditionExpression"])
        stored = writes[1]["Put"]["Item"]
        self.assertEqual(stored["content_sha256"], {"S": "original-sha"})
        self.assertEqual(writes[2]["Put"]["Item"]["event_type"], {"S": "POLICY_SOURCE_APPROVED"})

    def test_profile_and_audit_are_written_together_without_storage_keys(self) -> None:
        self.repository.record_profile(
            customer_id="cust-a",
            profile=PolicyProfile(
                policy_profile_id="profile-1",
                version="v1",
                rule_references=(PolicyRuleReference(rule_id="RULE-1", version="v1"),),
            ),
            published_by="admin",
            published_at="2026-09-01T00:00:00Z",
        )

        writes = self.client.requests[0]["TransactItems"]
        assert isinstance(writes, list)
        self.assertEqual(len(writes), 2)
        profile = writes[0]["Put"]["Item"]
        self.assertNotIn("bucket", profile)
        self.assertNotIn("key", profile)
        self.assertEqual(profile["customer_id"], {"S": "cust-a"})

    def test_candidate_extraction_persists_candidates_and_source_under_ready_binding(self) -> None:
        self.repository.record_candidate_extraction(customer_id="cust-a", extraction=extraction())

        writes = self.client.requests[0]["TransactItems"]
        assert isinstance(writes, list)
        self.assertEqual(len(writes), 3)
        # index 0: READY 바인딩 조건, 1: 후보 item, 2: PolicySource item.
        condition = writes[0]["ConditionCheck"]
        self.assertEqual(condition["Key"]["SK"], {"S": "POLICY_INGESTION#source-1#VERSION#v1"})
        self.assertIn(":ready", condition["ExpressionAttributeValues"])
        self.assertEqual(condition["ExpressionAttributeValues"][":ready"], {"S": "READY"})
        candidates_item = writes[1]["Put"]["Item"]
        self.assertEqual(
            candidates_item["SK"], {"S": "POLICY_SOURCE#source-1#VERSION#v1#CANDIDATES"}
        )
        self.assertEqual(candidates_item["entity_type"], {"S": "POLICY_CANDIDATE_EXTRACTION"})
        source_item = writes[2]["Put"]["Item"]
        self.assertEqual(source_item["SK"], {"S": "POLICY_SOURCE#source-1#VERSION#v1"})
        # PolicySource의 artifact 바인딩은 문서에서 유도하므로 승인 record와 일치한다.
        self.assertEqual(source_item["artifact_id"], {"S": "original-1"})
        self.assertEqual(source_item["content_sha256"], {"S": "original-sha"})
        # 원문·정규화 텍스트는 DynamoDB에 담기지 않는다.
        self.assertNotIn("bucket", source_item)
        self.assertNotIn("key", source_item)


class ReadTable:
    """auto-unmarshal되는 resource Table을 흉내내는 plain-dict 저장소."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def put(self, item: dict[str, object]) -> None:
        self.items[(item["PK"], item["SK"])] = item

    def get_item(self, **kwargs: object):
        key = kwargs["Key"]
        assert isinstance(key, dict)
        item = self.items.get((key["PK"], key["SK"]))
        return {"Item": item} if item is not None else {}


def _ingestion_item() -> dict[str, object]:
    document = extraction().document
    return {
        "PK": "CUSTOMER#cust-a",
        "SK": "POLICY_INGESTION#source-1#VERSION#v1",
        "entity_type": "POLICY_INGESTION",
        "customer_id": "cust-a",
        **document.to_dict(),
    }


class DynamoDbPolicyApprovalReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = ReadTable()
        self.repository = DynamoDbPolicyApprovalRepository(
            table_name="metadata",
            transaction_client=Client(),
            table=self.table,
            now=lambda: datetime(2026, 9, 1, tzinfo=UTC),
            id_factory=lambda: "id-1",
        )
        # write 경로가 남길 item들을 plain dict로 read table에 직접 채운다.
        extraction_dict = extraction().to_dict()
        self.table.put(_ingestion_item())
        self.table.put(
            {
                "PK": "CUSTOMER#cust-a",
                "SK": "POLICY_SOURCE#source-1#VERSION#v1#CANDIDATES",
                "entity_type": "POLICY_CANDIDATE_EXTRACTION",
                "customer_id": "cust-a",
                "source_id": "source-1",
                "source_version": "v1",
                **extraction_dict,
            }
        )
        self.table.put(
            {
                "PK": "CUSTOMER#cust-a",
                "SK": "POLICY_SOURCE#source-1#VERSION#v1",
                "entity_type": "POLICY_SOURCE",
                "customer_id": "cust-a",
                "source_id": "source-1",
                "kind": "INTERNAL_POLICY",
                "title": "policy.md",
                "policy_source_version": "v1",
                "artifact_id": "original-1",
                "content_sha256": "original-sha",
            }
        )

    def test_load_review_returns_ready_document_and_undecided_candidates(self) -> None:
        document, candidates = self.repository.load_review(
            customer_id="cust-a", source_id="source-1", source_version="v1"
        )
        self.assertTrue(document.is_approvable)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].rule.rule_id, "RULE-1")
        self.assertEqual(candidates[0].lifecycle.value, "CANDIDATE")

    def test_load_publication_marks_approved_candidates_and_returns_source(self) -> None:
        self.table.put(
            {
                "PK": "CUSTOMER#cust-a",
                "SK": "POLICY_SOURCE#source-1#VERSION#v1#APPROVAL",
                "entity_type": "POLICY_SOURCE_APPROVAL",
                "customer_id": "cust-a",
                **approval().to_dict(),
            }
        )
        candidates, approvals, sources = self.repository.load_publication(
            customer_id="cust-a", source_id="source-1", source_version="v1"
        )
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].is_approved)
        self.assertEqual(approvals[0].source_id, "source-1")
        self.assertEqual(sources[0].artifact_id, "original-1")
        self.assertEqual(sources[0].content_sha256, "original-sha")

    def test_load_review_without_read_table_fails_closed(self) -> None:
        repository = DynamoDbPolicyApprovalRepository(
            table_name="metadata", transaction_client=Client()
        )
        with self.assertRaises(RepositoryError):
            repository.load_review(customer_id="cust-a", source_id="source-1", source_version="v1")

    def test_load_publication_returns_only_approved_candidates_on_partial_approval(self) -> None:
        # CANDIDATES item에 후보 두 개(RULE-1, RULE-2)를 넣되 승인 record는 RULE-1만 승인한다.
        base = extraction().to_dict()
        second = {
            "rule": {
                "rule_id": "RULE-2",
                "version": "v1",
                "title": "Rule",
                "severity": "HIGH",
                "applicable_phases": ["INITIAL"],
                "resource_types": ["AWS::S3::Bucket"],
                "source_references": [
                    {
                        "source_id": "source-1",
                        "source_version": "v1",
                        "locator": "section-1",
                        "content_sha256": "unit-sha",
                    }
                ],
            },
            "lifecycle": "CANDIDATE",
        }
        base["candidates"] = [*base["candidates"], second]
        self.table.put(
            {
                "PK": "CUSTOMER#cust-a",
                "SK": "POLICY_SOURCE#source-1#VERSION#v1#CANDIDATES",
                "entity_type": "POLICY_CANDIDATE_EXTRACTION",
                "customer_id": "cust-a",
                "source_id": "source-1",
                "source_version": "v1",
                **base,
            }
        )
        self.table.put(
            {
                "PK": "CUSTOMER#cust-a",
                "SK": "POLICY_SOURCE#source-1#VERSION#v1#APPROVAL",
                "entity_type": "POLICY_SOURCE_APPROVAL",
                "customer_id": "cust-a",
                **approval().to_dict(),  # approved_rules = (RULE-1,)
            }
        )
        candidates, _, _ = self.repository.load_publication(
            customer_id="cust-a", source_id="source-1", source_version="v1"
        )
        # 게시 입력에는 승인된 RULE-1만 오고 미승인 RULE-2는 오지 않는다.
        approved_ids = {candidate.rule.rule_id for candidate in candidates}
        self.assertEqual(approved_ids, {"RULE-1"})
        self.assertTrue(all(candidate.is_approved for candidate in candidates))
