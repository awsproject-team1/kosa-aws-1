"""M1 live targets are deployment-defined and fail closed outside scope."""

import json
import unittest
from pathlib import Path

from apps.backend.assessment import AssessmentReportNotFoundError
from apps.backend.assessment.runtime import (
    DynamoM1WorkRepository,
    _actual_resource_tool,
    _ensure_evaluation_plan,
    _with_complete_evaluation_plan,
)
from apps.backend.assessment.runtime_config import (
    M1RuntimeConfiguration,
    M1RuntimeConfigurationError,
)
from apps.backend.policy import PolicyContextResolver, load_rule_registry
from packages.contracts import (
    EvaluationPerspective,
    ModelProfile,
    ModelProfileRole,
    PlannedEvaluation,
)

TARGET = {
    "customer_id": "cust-001",
    "repository_id": "repo-001",
    "policy_profile_id": "profile-mvp-baseline",
    "commit_sha": "a" * 40,
    "github_repository": "customer/iac",
    "github_token_secret_id": "github-token",
    "aws_account_id": "123456789012",
    "aws_read_role_arn": "arn:aws:iam::123456789012:role/Read",
    "aws_external_id_secret_id": "external-id",
    "s3_bucket_id": "customer-test-bucket",
}

MODEL_PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m1-v2",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment-s3-m1-v2",
    rubric_version="m1-v2",
    golden_dataset_version="m1-s3-v2",
)


class M1RuntimeConfigurationTest(unittest.TestCase):
    def test_resolves_only_an_exact_approved_scope(self) -> None:
        configuration = M1RuntimeConfiguration.from_json(json.dumps([TARGET]))
        target = configuration.resolve(
            customer_id="cust-001",
            repository_id="repo-001",
            policy_profile_id="profile-mvp-baseline",
        )
        self.assertEqual(target.commit_sha, "a" * 40)
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "outside M1 runtime scope"):
            configuration.resolve(
                customer_id="cust-001",
                repository_id="repo-other",
                policy_profile_id="profile-mvp-baseline",
            )

    def test_rejects_missing_or_extra_configuration_fields(self) -> None:
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "required"):
            M1RuntimeConfiguration.from_json("")
        invalid = dict(TARGET)
        invalid["unexpected"] = "value"
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "invalid"):
            M1RuntimeConfiguration.from_json(json.dumps([invalid]))

    def test_worker_repository_resolves_only_persisted_assessment_selectors(self) -> None:
        class Table:
            def query(self, **kwargs: object) -> dict[str, object]:
                return {
                    "Items": [
                        {
                            "customer_id": "cust-001",
                            "assessment_id": "asm-001",
                            "revision": 0,
                        }
                    ]
                }

            def get_item(self, **kwargs: object) -> dict[str, object]:
                return {
                    "Item": {
                        "repository_id": "repo-001",
                        "policy_profile_id": "profile-mvp-baseline",
                    }
                }

        repository = DynamoM1WorkRepository(
            Table(),
            M1RuntimeConfiguration.from_json(json.dumps([TARGET])),
            model_profile=MODEL_PROFILE,
        )
        work = repository.get_resource_work(job_id="job-001", expected_revision=0)

        self.assertIsNotNone(work)
        assert work is not None
        self.assertEqual(work.resource_id, "customer-test-bucket")
        self.assertEqual(work.perspective.value, "AWS_ACTUAL")
        self.assertEqual(work.model_profile_id, "assessment-nova-lite-m1-v2")


MULTI_TARGET = {
    **{name: value for name, value in TARGET.items() if name != "s3_bucket_id"},
    "resources": [
        {"resource_type": "AWS::S3::Bucket", "resource_id": "customer-test-bucket"},
        {"resource_type": "AWS::EC2::Instance", "resource_id": "i-0123456789abcdef0"},
        {"resource_type": "AWS::RDS::DBInstance", "resource_id": "demo-db-001"},
    ],
}


def _table(assessment: dict[str, object], *, policy_profile_id: str = "profile-mvp-baseline"):
    class Table:
        def query(self, **kwargs: object) -> dict[str, object]:
            return {
                "Items": [{"customer_id": "cust-001", "assessment_id": "asm-001", "revision": 0}]
            }

        def get_item(self, **kwargs: object) -> dict[str, object]:
            return {
                "Item": {
                    "repository_id": "repo-001",
                    "policy_profile_id": policy_profile_id,
                    **assessment,
                }
            }

    return Table()


class M1MultiResourceTargetTest(unittest.TestCase):
    """확장된 target은 승인된 (resource_type, resource_id) 목록으로 평가 대상을 정한다."""

    def _configuration(self, target=None) -> M1RuntimeConfiguration:
        return M1RuntimeConfiguration.from_json(json.dumps([target or MULTI_TARGET]))

    def _target(self, target=None):
        return self._configuration(target).resolve(
            customer_id="cust-001",
            repository_id="repo-001",
            policy_profile_id="profile-mvp-baseline",
        )

    def test_the_legacy_single_bucket_target_stays_valid(self) -> None:
        target = self._target(TARGET)

        self.assertEqual(target.resource_types, ("AWS::S3::Bucket",))
        self.assertEqual(target.resolve_resource().resource_id, "customer-test-bucket")

    def test_reports_the_resource_types_read_adapters_are_needed_for(self) -> None:
        self.assertEqual(
            self._target().resource_types,
            ("AWS::S3::Bucket", "AWS::EC2::Instance", "AWS::RDS::DBInstance"),
        )

    def test_resolves_the_resource_an_assessment_names(self) -> None:
        repository = DynamoM1WorkRepository(
            _table({"resource_type": "AWS::RDS::DBInstance", "resource_id": "demo-db-001"}),
            self._configuration(),
            model_profile=MODEL_PROFILE,
        )

        work = repository.get_resource_work(job_id="job-001", expected_revision=0)

        assert work is not None
        self.assertEqual(work.resource_type, "AWS::RDS::DBInstance")
        self.assertEqual(work.resource_id, "demo-db-001")

    def test_refuses_a_resource_outside_the_approved_list(self) -> None:
        """Assessment record는 서버가 쓰지만 승인 경계는 이 설정이다."""
        repository = DynamoM1WorkRepository(
            _table({"resource_type": "AWS::RDS::DBInstance", "resource_id": "not-approved"}),
            self._configuration(),
            model_profile=MODEL_PROFILE,
        )

        with self.assertRaisesRegex(M1RuntimeConfigurationError, "outside M1 runtime scope"):
            repository.get_resource_work(job_id="job-001", expected_revision=0)

    def test_expands_an_api_created_assessment_over_every_approved_resource(self) -> None:
        repository = DynamoM1WorkRepository(
            _table({}), self._configuration(), model_profile=MODEL_PROFILE
        )

        works = repository.get_resource_works(job_id="job-001", expected_revision=0)

        self.assertEqual(
            [(work.resource_type, work.resource_id) for work in works],
            [
                ("AWS::S3::Bucket", "customer-test-bucket"),
                ("AWS::EC2::Instance", "i-0123456789abcdef0"),
                ("AWS::RDS::DBInstance", "demo-db-001"),
            ],
        )
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "must be expanded"):
            repository.get_resource_work(job_id="job-001", expected_revision=0)

    def test_expanded_resources_share_one_complete_immutable_plan(self) -> None:
        policy_profile_id = "profile-multiresource-baseline"
        target = {**MULTI_TARGET, "policy_profile_id": policy_profile_id}
        repository = DynamoM1WorkRepository(
            _table({}, policy_profile_id=policy_profile_id),
            self._configuration(target),
            model_profile=MODEL_PROFILE,
        )
        works = repository.get_resource_works(job_id="job-001", expected_revision=0)
        resolver = PolicyContextResolver(
            load_rule_registry(Path(__file__).parents[2] / "fixtures" / "rules").catalog
        )

        planned_works = _with_complete_evaluation_plan(works, resolver)

        plans = {work.planned_coordinates for work in planned_works}
        self.assertEqual(len(plans), 1)
        plan = next(iter(plans))
        assert plan is not None
        self.assertEqual(
            {coordinate.resource_id for coordinate in plan},
            {
                "customer-test-bucket",
                "i-0123456789abcdef0",
                "demo-db-001",
            },
        )
        self.assertEqual(len(plan), 39)

        class PlanStore:
            def __init__(self) -> None:
                self.plan = None
                self.writes = 0

            def get_planned_evaluations(self, **kwargs: object):
                if self.plan is None:
                    raise AssessmentReportNotFoundError("missing")
                return self.plan.planned_coordinates

            def put_plan_if_absent(self, next_plan: object) -> None:
                self.plan = next_plan
                self.writes += 1

        store = PlanStore()
        _ensure_evaluation_plan(store, planned_works[0])
        _ensure_evaluation_plan(store, planned_works[0])
        self.assertEqual(store.writes, 1)

    def test_verification_expands_only_over_resources_in_the_source_plan(self) -> None:
        policy_profile_id = "profile-multiresource-baseline"
        target = {**MULTI_TARGET, "policy_profile_id": policy_profile_id}
        planned = tuple(
            PlannedEvaluation(
                resource_id=resource_id,
                rule_id=rule_id,
                perspective=EvaluationPerspective.AWS_ACTUAL,
            )
            for resource_id, rule_id in (
                ("customer-test-bucket", "S3-PUBLIC-001"),
                ("demo-db-001", "RDS-ACCESS-001"),
            )
        )

        class PlanReader:
            def get_planned_evaluations(self, **kwargs: object):
                return planned

        repository = DynamoM1WorkRepository(
            _table(
                {
                    "phase": "POST_DEPLOY_VERIFICATION",
                    "source_assessment_id": "asm-source",
                    "deployment_id": "dep-001",
                    "model_profile_id": MODEL_PROFILE.model_profile_id,
                    "rubric_version": MODEL_PROFILE.rubric_version,
                    "policy_profile_version": "v1",
                },
                policy_profile_id=policy_profile_id,
            ),
            self._configuration(target),
            model_profile=MODEL_PROFILE,
            plan_reader=PlanReader(),
        )

        works = repository.get_resource_works(job_id="job-001", expected_revision=0)

        self.assertEqual(
            [work.resource_id for work in works],
            ["customer-test-bucket", "demo-db-001"],
        )
        self.assertTrue(all(work.planned_coordinates == planned for work in works))

    def test_refuses_half_a_resource_selector(self) -> None:
        repository = DynamoM1WorkRepository(
            _table({"resource_type": "AWS::RDS::DBInstance"}),
            self._configuration(),
            model_profile=MODEL_PROFILE,
        )

        with self.assertRaisesRegex(ValueError, "resource_id is invalid"):
            repository.get_resource_work(job_id="job-001", expected_revision=0)

    def test_rejects_declaring_resources_two_ways_at_once(self) -> None:
        both = {**MULTI_TARGET, "s3_bucket_id": "customer-test-bucket"}
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "invalid"):
            self._configuration(both)

    def test_rejects_a_resource_type_without_a_read_adapter(self) -> None:
        unsupported = {
            **MULTI_TARGET,
            "resources": [{"resource_type": "AWS::DynamoDB::Table", "resource_id": "table-1"}],
        }
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "invalid"):
            self._configuration(unsupported)

    def test_rejects_a_duplicate_approved_resource(self) -> None:
        duplicated = {
            **MULTI_TARGET,
            "resources": [
                {"resource_type": "AWS::S3::Bucket", "resource_id": "customer-test-bucket"},
                {"resource_type": "AWS::S3::Bucket", "resource_id": "customer-test-bucket"},
            ],
        }
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "invalid"):
            self._configuration(duplicated)

    def test_rejects_an_empty_resource_list(self) -> None:
        with self.assertRaisesRegex(M1RuntimeConfigurationError, "invalid"):
            self._configuration({**MULTI_TARGET, "resources": []})


class FakeBoto3:
    """Record which AWS clients the composition root builds, without any SDK."""

    def __init__(self) -> None:
        self.services: list[str] = []

    def client(self, service: str, **kwargs: object) -> object:
        self.services.append(service)
        return object()


class ActualResourceToolWiringTest(unittest.TestCase):
    """설정에 선언된 유형만큼의 read adapter가 만들어지고 분배가 그 목록으로 닫힌다."""

    def _target(self, resources):
        configuration = M1RuntimeConfiguration.from_json(
            json.dumps(
                [
                    {
                        **{name: value for name, value in TARGET.items() if name != "s3_bucket_id"},
                        "resources": resources,
                    }
                ]
            )
        )
        return configuration.resolve(
            customer_id="cust-001",
            repository_id="repo-001",
            policy_profile_id="profile-mvp-baseline",
        )

    def test_builds_one_adapter_per_declared_resource_type(self) -> None:
        boto3 = FakeBoto3()
        target = self._target(
            [
                {"resource_type": "AWS::EC2::Instance", "resource_id": "i-0123456789abcdef0"},
                {"resource_type": "AWS::EC2::Instance", "resource_id": "i-0fedcba9876543210"},
                {
                    "resource_type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
                    "resource_id": (
                        "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                        "loadbalancer/app/demo/50dc6c495c0c9188"
                    ),
                },
            ]
        )

        tool = _actual_resource_tool(boto3, target=target, external_id="external")

        self.assertEqual(
            tool.resource_types,
            ("AWS::EC2::Instance", "AWS::ElasticLoadBalancingV2::LoadBalancer"),
        )
        # Two declared types collapse to two adapters, and the service clients stay
        # unbuilt until a read actually needs credentials.
        self.assertEqual(boto3.services, ["sts"])

    def test_refuses_to_wire_a_type_without_a_read_adapter(self) -> None:
        """두 allow-list(evidence scope / adapter builder)가 어긋나면 시작 전에 멈춘다."""
        boto3 = FakeBoto3()

        class Unbuildable:
            customer_id = "cust-001"
            aws_account_id = "123456789012"
            aws_read_role_arn = "arn:aws:iam::123456789012:role/Read"
            resource_types = ("AWS::DynamoDB::Table",)

        with self.assertRaisesRegex(RuntimeError, "no Actual read adapter exists"):
            _actual_resource_tool(boto3, target=Unbuildable(), external_id="external")

    def test_a_type_the_deployment_did_not_declare_is_not_readable(self) -> None:
        boto3 = FakeBoto3()
        target = self._target(
            [{"resource_type": "AWS::S3::Bucket", "resource_id": "customer-test-bucket"}]
        )

        tool = _actual_resource_tool(boto3, target=target, external_id="external")

        self.assertTrue(tool.supports("AWS::S3::Bucket"))
        self.assertFalse(tool.supports("AWS::RDS::DBInstance"))
