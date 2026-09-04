"""The same declared predicate judges a plan's `after` values before apply (ADR-0024 §E).

이 파일이 고정하는 것은 셋이다.

1. **plan 근거는 Catalog가 선언한 위치만 담는다.** allow-list다 — 새 provider attribute가 조용히
   근거가 되지 않는다.
2. **판정은 AWS 문서와 같은 술어다.** 네 플래그 중 셋만 true인 plan은 FAIL이고, 관측 상세는 3/4다.
3. **답할 수 없으면 판정하지 않는다.** 값이 plan에 없으면(계산 중, block 미선언, 술어가 plan 모양과
   맞지 않음) `None`이다 — "해소됨"이 아니다.
"""

import unittest

from apps.backend.assessment.plan_facts import (
    decide_from_plan_evidence,
    evidence_location,
    project_plan_evidence,
)
from apps.backend.policy.control_catalog import MVP_CONTROL_CATALOG as CATALOG
from packages.contracts import EvaluationPerspective, EvaluationStatus
from packages.contracts.terraform_plan import _RESOURCE_IDENTITY_ATTRIBUTES, resource_identity

BUCKET = "acme-media-assets"
ALB_ARN = "arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/acme-web/1a2b3c"


def _change(address: str, kind: str, after: dict[str, object] | None, actions=("update",)):
    return {
        "address": address,
        "mode": "managed",
        "type": kind,
        "name": address.split(".")[-1],
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "change": {
            "actions": list(actions),
            "before": None,
            "after": after,
            "after_unknown": {},
            "replace_paths": [],
        },
    }


def _pab_plan(**flags: bool) -> dict[str, object]:
    after = {"bucket": BUCKET, **flags}
    return {
        "resource_changes": [
            _change(
                "aws_s3_bucket_public_access_block.media",
                "aws_s3_bucket_public_access_block",
                after,
            )
        ]
    }


def _s3_control():
    control = CATALOG.control("S3_BLOCK_PUBLIC_ACCESS")
    assert control is not None
    return control, control.baseline_required_evidence


class ProjectionTest(unittest.TestCase):
    def test_collects_only_catalog_locations_keyed_by_the_finding_resource_id(self) -> None:
        evidence = project_plan_evidence(
            _pab_plan(
                block_public_acls=True,
                ignore_public_acls=True,
                block_public_policy=True,
                restrict_public_buckets=False,
                tags={"Owner": "someone"},  # not a catalog location
            ),
            CATALOG,
        )

        self.assertEqual(list(evidence), [BUCKET])
        facts = evidence[BUCKET]
        self.assertEqual(
            facts[
                evidence_location("aws_s3_bucket_public_access_block", "restrict_public_buckets")
            ],
            (False,),
        )
        self.assertFalse(any("tags" in location for location in facts))

    def test_a_resource_the_catalog_cannot_identify_contributes_nothing(self) -> None:
        plan = {
            "resource_changes": [
                _change("aws_security_group.web", "aws_security_group", {"name": "web"})
            ]
        }
        self.assertEqual(project_plan_evidence(plan, CATALOG), {})

    def test_a_computed_value_is_not_evidence(self) -> None:
        """`after` without the attribute (after_unknown) leaves the location absent."""
        evidence = project_plan_evidence(_pab_plan(block_public_acls=True), CATALOG)
        facts = evidence[BUCKET]
        self.assertNotIn(
            evidence_location("aws_s3_bucket_public_access_block", "restrict_public_buckets"), facts
        )

    def test_every_catalog_plan_type_has_a_finding_identity(self) -> None:
        """A plan path on a type without an identity attribute could never be matched to a Finding."""
        for control in CATALOG.controls:
            for binding in control.available_evidence_capabilities:
                for entry in binding.plan_paths:
                    with self.subTest(capability=binding.capability_key, path=entry.path):
                        self.assertIn(entry.terraform_resource_type, _RESOURCE_IDENTITY_ATTRIBUTES)
                        self.assertIs(binding.perspective, EvaluationPerspective.AWS_ACTUAL)
                        self.assertTrue(binding.is_decidable)


class VerdictTest(unittest.TestCase):
    def test_three_of_four_flags_in_the_plan_is_still_a_violation(self) -> None:
        control, required = _s3_control()
        evidence = project_plan_evidence(
            _pab_plan(
                block_public_acls=True,
                ignore_public_acls=True,
                block_public_policy=True,
                restrict_public_buckets=False,
            ),
            CATALOG,
        )

        verdict = decide_from_plan_evidence(
            control,
            required,
            resource_type="AWS::S3::Bucket",
            resource_id=BUCKET,
            evidence=evidence,
        )

        assert verdict is not None
        self.assertIs(verdict.status, EvaluationStatus.FAIL)
        self.assertEqual((verdict.observed_satisfied, verdict.observed_total), (3, 4))

    def test_all_four_flags_resolve_the_finding(self) -> None:
        control, required = _s3_control()
        evidence = project_plan_evidence(
            _pab_plan(
                block_public_acls=True,
                ignore_public_acls=True,
                block_public_policy=True,
                restrict_public_buckets=True,
            ),
            CATALOG,
        )
        verdict = decide_from_plan_evidence(
            control,
            required,
            resource_type="AWS::S3::Bucket",
            resource_id=BUCKET,
            evidence=evidence,
        )
        assert verdict is not None
        self.assertIs(verdict.status, EvaluationStatus.PASS)

    def test_a_plan_boolean_satisfies_a_string_expectation(self) -> None:
        """AWS reports `access_logs.s3.enabled` as "true"; the plan carries `true`. Same fact."""
        control = CATALOG.control("ALB_ACCESS_LOGGING")
        assert control is not None
        plan = {
            "resource_changes": [
                _change(
                    "aws_lb.web",
                    "aws_lb",
                    {"arn": ALB_ARN, "access_logs": [{"enabled": True, "bucket": "logs"}]},
                )
            ]
        }
        verdict = decide_from_plan_evidence(
            control,
            control.baseline_required_evidence,
            resource_type="AWS::ElasticLoadBalancingV2::LoadBalancer",
            resource_id=ALB_ARN,
            evidence=project_plan_evidence(plan, CATALOG),
        )
        assert verdict is not None
        self.assertIs(verdict.status, EvaluationStatus.PASS)

    def test_no_plan_values_means_no_verdict(self) -> None:
        control, required = _s3_control()
        self.assertIsNone(
            decide_from_plan_evidence(
                control, required, resource_type="AWS::S3::Bucket", resource_id=BUCKET, evidence={}
            )
        )
        self.assertIsNone(
            decide_from_plan_evidence(
                control,
                required,
                resource_type="AWS::S3::Bucket",
                resource_id=BUCKET,
                evidence={BUCKET: {"aws_s3_bucket.media:tags": ("x",)}},
            )
        )

    def test_a_capability_without_plan_paths_is_not_judged_from_the_plan(self) -> None:
        """SG ingress blocks look nothing like IpPermissions; the shared predicate would be a lie."""
        control = CATALOG.control("EC2_SG_INGRESS_RESTRICTED")
        assert control is not None
        plan = {
            "resource_changes": [
                _change("aws_instance.app", "aws_instance", {"id": "i-1", "ingress": []})
            ]
        }
        self.assertIsNone(
            decide_from_plan_evidence(
                control,
                control.baseline_required_evidence,
                resource_type="AWS::EC2::Instance",
                resource_id="i-1",
                evidence=project_plan_evidence(plan, CATALOG),
            )
        )

    def test_resource_identity_reads_after_then_before(self) -> None:
        entry = _change("aws_s3_bucket.media", "aws_s3_bucket", None, actions=("delete",))
        entry["change"]["before"] = {"bucket": BUCKET}
        self.assertEqual(resource_identity(entry), BUCKET)


if __name__ == "__main__":
    unittest.main()
