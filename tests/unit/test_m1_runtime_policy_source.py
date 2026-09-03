"""The live M1 Runtime evaluates the customer's approved Rules, not committed fixtures.

이것이 이 변경의 핵심이다. Runtime이 `fixtures/rules`를 읽으면, 고객이 업로드하고 사람이 승인한
정책이 아니라 저장소에 커밋된 Rule로 평가하게 된다 — 업로드부터 승인까지의 경계 전체가 결과에
아무 영향을 주지 않는다는 뜻이다.

M0 synthetic 경로는 바뀌지 않는다. 그 경로는 고객 정책이 아니라 합성 fixture를 평가하는 데모다.
"""

import json
import unittest
from pathlib import Path

from apps.backend.assessment.runtime import m1_context_resolver
from apps.backend.assessment.runtime_config import M1RuntimeConfiguration
from apps.backend.policy import PolicyNotFoundError
from apps.backend.policy.registry import load_rule_registry
from packages.contracts import AssessmentPhase, RuleEvaluationType, RuleLifecycle
from tests.unit.test_approved_rule_registry import _approve
from tests.unit.test_authoring_result_persistence import (
    DOCUMENT,
    FakeTable,
    _repository,
    _result,
)

RULES_PATH = Path(__file__).parents[2] / "fixtures" / "rules"
CUSTOMER = "cust-001"

TARGET = {
    "customer_id": CUSTOMER,
    "repository_id": "repo-001",
    "commit_sha": "a" * 40,
    "github_repository": "customer/iac",
    "github_token_secret_id": "github-token",
    "aws_account_id": "123456789012",
    "aws_read_role_arn": "arn:aws:iam::123456789012:role/Read",
    "aws_external_id_secret_id": "external-id",
    "s3_bucket_id": "customer-test-bucket",
}


def _target(customer_id: str = CUSTOMER):
    configuration = M1RuntimeConfiguration.from_json(
        json.dumps([{**TARGET, "customer_id": customer_id}])
    )
    return configuration.resolve(customer_id=customer_id, repository_id="repo-001")


def _publish_customer_profile(table: FakeTable) -> str:
    """Approve the authored Rules, then publish a Profile that references them."""
    repository, rule_ids = _approve(table)
    from packages.contracts import PolicyProfile, PolicyRuleReference

    profile = PolicyProfile(
        policy_profile_id="profile-customer-baseline",
        version="v1",
        rule_references=tuple(
            PolicyRuleReference(rule_id=rule_id, version=DOCUMENT.source_version)
            for rule_id in rule_ids
        ),
    )
    repository.record_profile(
        customer_id=CUSTOMER,
        profile=profile,
        published_by="admin@example.com",
        published_at="2026-09-03T00:00:00Z",
    )
    return profile.policy_profile_id


class RuntimePolicySourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = FakeTable()
        self.profile_id = _publish_customer_profile(self.table)

    def test_the_runtime_resolves_the_customers_own_approved_rules(self) -> None:
        resolver = m1_context_resolver(self.table, target=_target())

        context = resolver.resolve(
            policy_profile_id=self.profile_id,
            phase=AssessmentPhase.INITIAL,
            resource_type="AWS::S3::Bucket",
            expected_profile_version="v1",
        )

        self.assertEqual(len(context.rules), 1)
        rule = context.rules[0]
        self.assertEqual(rule.control_key, "S3_BLOCK_PUBLIC_ACCESS")
        self.assertEqual(rule.evaluation_type, RuleEvaluationType.AWS)
        self.assertFalse(rule.is_legacy)

    def test_the_runtime_does_not_fall_back_to_the_committed_fixture_registry(self) -> None:
        """커밋된 Registry의 Rule ID는 고객 partition에 존재하지 않는다.

        Runtime이 fixture를 읽고 있었다면 이 Profile이 해석돼 버린다.
        """
        fixture_profile = load_rule_registry(RULES_PATH).profiles[0]
        resolver = m1_context_resolver(self.table, target=_target())

        with self.assertRaises(PolicyNotFoundError):
            resolver.resolve(
                policy_profile_id=fixture_profile.policy_profile_id,
                phase=AssessmentPhase.INITIAL,
                resource_type="AWS::S3::Bucket",
            )

    def test_another_customer_sees_none_of_these_rules(self) -> None:
        resolver = m1_context_resolver(self.table, target=_target("cust-002"))

        with self.assertRaises(PolicyNotFoundError):
            resolver.resolve(
                policy_profile_id=self.profile_id,
                phase=AssessmentPhase.INITIAL,
                resource_type="AWS::S3::Bucket",
            )

    def test_the_pinned_version_is_read_directly_rather_than_followed_from_the_pointer(
        self,
    ) -> None:
        """실행 중 새 Profile이 게시돼도 고정한 판본으로 끝난다."""
        repository = _repository(self.table)
        from packages.contracts import PolicyProfile, PolicyRuleReference

        replacement = PolicyProfile(
            policy_profile_id=self.profile_id,
            version="v2",
            rule_references=(
                PolicyRuleReference(
                    rule_id=_result().candidates[0].rule.rule_id,
                    version=DOCUMENT.source_version,
                ),
            ),
        )
        repository.record_profile(
            customer_id=CUSTOMER,
            profile=replacement,
            published_by="admin@example.com",
            published_at="2026-09-03T01:00:00Z",
            expected_current_version="v1",
        )
        resolver = m1_context_resolver(self.table, target=_target())

        pinned = resolver.resolve(
            policy_profile_id=self.profile_id,
            phase=AssessmentPhase.INITIAL,
            resource_type="AWS::S3::Bucket",
            expected_profile_version="v1",
        )

        self.assertEqual(pinned.policy_profile_version, "v1")

    def test_an_unapproved_rule_in_the_partition_is_never_evaluated(self) -> None:
        """승인 경계를 거치지 않은 item이 평가에 들어오면 검토 게이트가 형식이 된다."""
        rule_key = next(key for key in self.table.items if key[1].startswith("RULE#CUST-S3_BLOCK"))
        self.table.items[rule_key]["lifecycle"] = RuleLifecycle.CANDIDATE.value
        resolver = m1_context_resolver(self.table, target=_target())

        with self.assertRaises(Exception) as caught:
            resolver.resolve(
                policy_profile_id=self.profile_id,
                phase=AssessmentPhase.INITIAL,
                resource_type="AWS::S3::Bucket",
                expected_profile_version="v1",
            )

        self.assertIn("not approved", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
