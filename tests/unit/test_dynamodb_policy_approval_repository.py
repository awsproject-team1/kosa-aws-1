"""Approval/Profile DynamoDB writes keep bindings and audit events atomic."""

import unittest
from datetime import UTC, datetime

from apps.backend.repositories.policy_approval import DynamoDbPolicyApprovalRepository
from packages.contracts import PolicyProfile, PolicyRuleReference, PolicySourceApproval


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
