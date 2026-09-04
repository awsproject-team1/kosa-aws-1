"""A-owned DynamoDB remediation-context reader tests.

Rebuild `RemediationContext`/`RemediationTarget` from immutable M1 evidence and fail
closed on missing evidence, scope mismatch, ambiguous findings, and missing provenance.
"""

import hashlib
import json
import unittest

from apps.backend.repositories import RepositoryError, StoredDataError
from apps.backend.repositories.remediation_context import DynamoDbRemediationContextReader
from packages.contracts import (
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    RemediationTarget,
)

CUSTOMER = "kosa-sandbox"
ASSESSMENT = "asm-001"
FINDING_ID = "finding-abc"
COMMIT = "d6b2c119872e20a890e14cb6bc41017527e600e6"
EVALUATED_AT = "2026-09-03T06:27:46.374691+00:00"


def _finding_item(**overrides):
    item = {
        "PK": f"CUSTOMER#{CUSTOMER}",
        "SK": f"ASSESSMENT#{ASSESSMENT}#FINDING#{FINDING_ID}",
        "entity_type": "FINDING",
        "customer_id": CUSTOMER,
        "assessment_id": ASSESSMENT,
        "finding_id": FINDING_ID,
        "resource_id": "tfsbx-bucket",
        "rule_id": "S3-PUBLIC-001",
        "rule_version": "2026-08-31",
        "perspective": EvaluationPerspective.AWS_ACTUAL.value,
        "status": EvaluationStatus.FAIL.value,
        "severity": "HIGH",
        "score": 0,
        "rationale": "public access is not blocked",
        "evidence_references": ["aws:tfsbx-bucket:public_access_block"],
        "assessed_commit_sha": COMMIT,
        "evaluated_at": EVALUATED_AT,
    }
    item.update(overrides)
    return item


def _result_item(perspective: EvaluationPerspective, status: EvaluationStatus, **overrides):
    item = {
        "PK": f"CUSTOMER#{CUSTOMER}",
        "SK": (
            f"ASSESSMENT#{ASSESSMENT}#RESULT#tfsbx-bucket"
            f"#RULE#S3-PUBLIC-001#PERSPECTIVE#{perspective.value}"
        ),
        "entity_type": "ASSESSMENT_RESULT",
        "customer_id": CUSTOMER,
        "assessment_id": ASSESSMENT,
        "resource_id": "tfsbx-bucket",
        "rule_id": "S3-PUBLIC-001",
        "perspective": perspective.value,
        "status": status.value,
        "severity": "HIGH",
        "score": 0 if status is EvaluationStatus.FAIL else 100,
        "rationale": "derived",
        "evidence_references": [f"{perspective.value}:tfsbx-bucket"],
        "rule_version": "2026-08-31",
        "rubric_version": "2026-08-31",
        "model_profile_id": "mp-nova-lite",
        "scoring_mode": "CONTINUOUS",
        "assessed_commit_sha": COMMIT,
        "evaluated_at": EVALUATED_AT,
    }
    item.update(overrides)
    return item


def _assessment_item(**overrides):
    item = {
        "PK": f"CUSTOMER#{CUSTOMER}",
        "SK": f"ASSESSMENT#{ASSESSMENT}",
        "entity_type": "ASSESSMENT",
        "customer_id": CUSTOMER,
        "assessment_id": ASSESSMENT,
        "repository_id": "test-s3-sandbox",
        "policy_profile_id": "profile-mvp-baseline",
        "job_id": "job-001",
        "phase": "INITIAL",
        "status": "QUEUED",
    }
    item.update(overrides)
    return item


class Table:
    """Fake resource-level table: FINDING via query, RESULT/ASSESSMENT via get_item."""

    def __init__(self, *, findings=None, items=None):
        self.findings = [_finding_item()] if findings is None else findings
        # keyed by SK for get_item
        default = {
            f"ASSESSMENT#{ASSESSMENT}#RESULT#tfsbx-bucket#RULE#S3-PUBLIC-001#PERSPECTIVE#IAC": (
                _result_item(EvaluationPerspective.IAC, EvaluationStatus.FAIL)
            ),
            f"ASSESSMENT#{ASSESSMENT}#RESULT#tfsbx-bucket#RULE#S3-PUBLIC-001#PERSPECTIVE#AWS_ACTUAL": (
                _result_item(EvaluationPerspective.AWS_ACTUAL, EvaluationStatus.FAIL)
            ),
            f"ASSESSMENT#{ASSESSMENT}": _assessment_item(),
        }
        self.items = default if items is None else items
        self.query_calls = []
        self.get_calls = []

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return {"Items": list(self.findings)}

    def get_item(self, **kwargs):
        self.get_calls.append(kwargs)
        sk = kwargs["Key"]["SK"]
        item = self.items.get(sk)
        return {"Item": item} if item is not None else {}


class RemediationContextReaderTest(unittest.TestCase):
    def test_get_context_rebuilds_finding_snapshot_and_evidence(self):
        table = Table()

        context = DynamoDbRemediationContextReader(table).get_context(
            customer_id=CUSTOMER, finding_id=FINDING_ID
        )

        self.assertEqual(context.finding.finding_id, FINDING_ID)
        self.assertEqual(context.finding.assessed_commit_sha, COMMIT)
        self.assertEqual(context.snapshot.customer_id, CUSTOMER)
        self.assertEqual(context.snapshot.repository_id, "test-s3-sandbox")
        self.assertEqual(context.snapshot.commit_sha, COMMIT)
        self.assertEqual(context.snapshot.artifact.artifact_type, ArtifactType.TERRAFORM_SNAPSHOT)
        self.assertTrue(context.evidence_references)

    def test_snapshot_digest_is_deterministic_over_coordinates(self):
        table = Table()

        context = DynamoDbRemediationContextReader(table).get_context(
            customer_id=CUSTOMER, finding_id=FINDING_ID
        )

        expected = hashlib.sha256(
            json.dumps(
                {
                    "customer_id": CUSTOMER,
                    "repository_id": "test-s3-sandbox",
                    "commit_sha": COMMIT,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(context.snapshot.artifact.content_sha256, expected)

    def test_get_target_binds_iac_status_and_commit(self):
        table = Table()

        target = DynamoDbRemediationContextReader(table).get_target(
            customer_id=CUSTOMER, finding_id=FINDING_ID
        )

        self.assertIsInstance(target, RemediationTarget)
        self.assertEqual(target.resource_id, "tfsbx-bucket")
        self.assertEqual(target.resource_type, "AWS::S3::Bucket")
        self.assertEqual(target.rule_id, "S3-PUBLIC-001")
        self.assertTrue(target.terraform_managed)
        self.assertEqual(target.iac_status, EvaluationStatus.FAIL)
        self.assertEqual(target.iac_perspective, EvaluationPerspective.IAC)
        self.assertEqual(target.iac_commit_sha, COMMIT)

    def test_get_target_restores_the_resource_type_from_the_actual_read_locator(self):
        """An RDS Finding must not produce an S3 target — the locator says what was read."""
        table = Table()
        actual_sk = (
            f"ASSESSMENT#{ASSESSMENT}#RESULT#tfsbx-bucket#RULE#S3-PUBLIC-001#PERSPECTIVE#AWS_ACTUAL"
        )
        table.items[actual_sk]["evidence_references"] = [
            "aws:rds:db-instance/tfsbx-bucket#read-resource",
            "isms-p-2023@2023-10-31#control/2.10.2",
        ]

        target = DynamoDbRemediationContextReader(table).get_target(
            customer_id=CUSTOMER, finding_id=FINDING_ID
        )

        self.assertEqual(target.resource_type, "AWS::RDS::DBInstance")

    def test_get_target_keeps_the_legacy_s3_type_without_a_recognized_locator(self):
        target = DynamoDbRemediationContextReader(Table()).get_target(
            customer_id=CUSTOMER, finding_id=FINDING_ID
        )
        self.assertEqual(target.resource_type, "AWS::S3::Bucket")

    def test_finding_query_is_scoped_to_customer_partition(self):
        table = Table()

        DynamoDbRemediationContextReader(table).get_context(
            customer_id=CUSTOMER, finding_id=FINDING_ID
        )

        values = table.query_calls[0]["ExpressionAttributeValues"]
        self.assertEqual(values[":pk"], f"CUSTOMER#{CUSTOMER}")
        self.assertEqual(values[":fid"], FINDING_ID)
        self.assertEqual(values[":finding"], "FINDING")

    def test_missing_finding_fails_closed(self):
        table = Table(findings=[])

        with self.assertRaises(StoredDataError):
            DynamoDbRemediationContextReader(table).get_context(
                customer_id=CUSTOMER, finding_id=FINDING_ID
            )

    def test_ambiguous_finding_fails_closed(self):
        table = Table(findings=[_finding_item(), _finding_item(assessment_id="asm-002")])

        with self.assertRaises(StoredDataError):
            DynamoDbRemediationContextReader(table).get_context(
                customer_id=CUSTOMER, finding_id=FINDING_ID
            )

    def test_a_rule_evaluated_in_one_perspective_still_reaches_the_policy(self):
        """`AWS` Rule에는 IaC 판정이 없다. 저장소가 그것을 오류로 막으면 정책이 판단할 기회가 없다.

        Finding은 `AWS_ACTUAL` 관점이므로 IaC 결과가 없어도 증거는 완결돼 있다. Patch와 동기화를
        가를 수 없다는 판단은 `RemediationPolicy.decide()`가 `MANUAL_REVIEW`로 내린다.
        """
        table = Table()
        del table.items[
            f"ASSESSMENT#{ASSESSMENT}#RESULT#tfsbx-bucket#RULE#S3-PUBLIC-001#PERSPECTIVE#IAC"
        ]
        reader = DynamoDbRemediationContextReader(table)

        context = reader.get_context(customer_id=CUSTOMER, finding_id=FINDING_ID)
        target = reader.get_target(customer_id=CUSTOMER, finding_id=FINDING_ID)

        self.assertEqual(context.finding.finding_id, FINDING_ID)
        # 세 필드는 한 묶음이다. IaC 판정이 없으면 출처도 없다.
        self.assertIsNone(target.iac_status)
        self.assertIsNone(target.iac_perspective)
        self.assertIsNone(target.iac_commit_sha)

    def test_the_findings_own_perspective_is_still_required(self):
        """Finding이 나온 그 결과가 없으면 저장된 증거가 서로 어긋난 것이다."""
        table = Table()
        del table.items[
            f"ASSESSMENT#{ASSESSMENT}#RESULT#tfsbx-bucket#RULE#S3-PUBLIC-001#PERSPECTIVE#AWS_ACTUAL"
        ]

        with self.assertRaises(StoredDataError):
            DynamoDbRemediationContextReader(table).get_context(
                customer_id=CUSTOMER, finding_id=FINDING_ID
            )

    def test_missing_assessment_record_fails_closed(self):
        table = Table()
        del table.items[f"ASSESSMENT#{ASSESSMENT}"]

        with self.assertRaises(StoredDataError):
            DynamoDbRemediationContextReader(table).get_context(
                customer_id=CUSTOMER, finding_id=FINDING_ID
            )

    def test_result_scope_mismatch_fails_closed(self):
        table = Table()
        table.items[
            f"ASSESSMENT#{ASSESSMENT}#RESULT#tfsbx-bucket#RULE#S3-PUBLIC-001#PERSPECTIVE#IAC"
        ]["customer_id"] = "other"

        with self.assertRaises(StoredDataError):
            DynamoDbRemediationContextReader(table).get_target(
                customer_id=CUSTOMER, finding_id=FINDING_ID
            )

    def test_query_failure_is_repository_error(self):
        class Failing(Table):
            def query(self, **kwargs):
                raise RuntimeError("throttled")

        with self.assertRaises(RepositoryError):
            DynamoDbRemediationContextReader(Failing()).get_context(
                customer_id=CUSTOMER, finding_id=FINDING_ID
            )

    def test_blank_finding_id_rejected(self):
        with self.assertRaises(ValueError):
            DynamoDbRemediationContextReader(Table()).get_context(
                customer_id=CUSTOMER, finding_id=" "
            )


if __name__ == "__main__":
    unittest.main()
