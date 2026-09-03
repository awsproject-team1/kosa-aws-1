"""Contract tests for the shared `terraform show -json` projection (ADR-0019 §1)."""

import unittest

from packages.contracts import (
    ArtifactType,
    PlanProjectionError,
    canonical_plan_bytes,
    compute_plan_hash,
    has_destructive_changes,
    mapped_resource_ids,
    project_plan_changes,
)


def show_json(resource_changes: list[dict[str, object]], **extra: object) -> dict[str, object]:
    """Build a `show -json`-shaped document with noise fields for exclusion tests."""
    return {
        "format_version": "1.2",
        "terraform_version": "1.9.5",
        "timestamp": "2026-09-02T00:00:00Z",
        "resource_changes": resource_changes,
        **extra,
    }


def change(address: str, actions: list[str], **change_fields: object) -> dict[str, object]:
    return {
        "address": address,
        "mode": "managed",
        "type": "aws_s3_bucket",
        "name": address.split(".")[-1],
        "index": None,
        "provider_name": "registry.terraform.io/hashicorp/aws",
        # A field outside the allow-list that must never enter the hash.
        "deposed": "abc123",
        "change": {
            "actions": actions,
            "before": None,
            "after": {"acl": "private"},
            **change_fields,
        },
    }


class TerraformPlanProjectionTest(unittest.TestCase):
    def test_plan_hash_is_stable_across_two_computations(self) -> None:
        # ADR-0019 불변식 #2: 같은 plan에서 두 번 계산해도 같다.
        document = show_json([change("aws_s3_bucket.logs", ["update"])])
        self.assertEqual(compute_plan_hash(document), compute_plan_hash(document))

    def test_projection_is_invariant_to_resource_change_ordering(self) -> None:
        first = show_json(
            [change("aws_s3_bucket.b", ["update"]), change("aws_s3_bucket.a", ["create"])]
        )
        second = show_json(
            [change("aws_s3_bucket.a", ["create"]), change("aws_s3_bucket.b", ["update"])]
        )
        self.assertEqual(compute_plan_hash(first), compute_plan_hash(second))
        addresses = [entry["address"] for entry in project_plan_changes(first)]
        self.assertEqual(addresses, ["aws_s3_bucket.a", "aws_s3_bucket.b"])

    def test_projection_drops_fields_outside_the_allow_list(self) -> None:
        document = show_json([change("aws_s3_bucket.logs", ["update"])])
        [projected] = project_plan_changes(document)
        self.assertNotIn("deposed", projected)
        self.assertEqual(
            set(projected), {"address", "mode", "type", "name", "index", "provider_name", "change"}
        )
        self.assertEqual(
            set(projected["change"]),
            {"actions", "before", "after", "after_unknown", "replace_paths"},
        )

    def test_top_level_noise_does_not_change_hash(self) -> None:
        base = show_json([change("aws_s3_bucket.logs", ["update"])])
        noisy = show_json(
            [change("aws_s3_bucket.logs", ["update"])],
            prior_state={"values": {"root_module": {}}},
        )
        self.assertEqual(compute_plan_hash(base), compute_plan_hash(noisy))

    def test_canonical_bytes_have_no_trailing_newline_and_compact_separators(self) -> None:
        document = show_json([change("aws_s3_bucket.logs", ["update"])])
        payload = canonical_plan_bytes(document)
        self.assertFalse(payload.endswith(b"\n"))
        self.assertNotIn(b", ", payload)
        self.assertNotIn(b": ", payload)

    def test_destructive_when_change_deletes(self) -> None:
        # ADR-0019 불변식 #8: delete는 파괴적이다.
        document = show_json([change("aws_s3_bucket.logs", ["delete", "create"])])
        self.assertTrue(has_destructive_changes(document))

    def test_destructive_when_replace_paths_present(self) -> None:
        document = show_json([change("aws_s3_bucket.logs", ["update"], replace_paths=[["acl"]])])
        self.assertTrue(has_destructive_changes(document))

    def test_not_destructive_for_pure_create_or_update(self) -> None:
        document = show_json(
            [
                change("aws_s3_bucket.a", ["create"]),
                change("aws_s3_bucket.b", ["update"], replace_paths=[]),
            ]
        )
        self.assertFalse(has_destructive_changes(document))

    def test_rejects_nan_and_infinity(self) -> None:
        document = show_json(
            [change("aws_s3_bucket.logs", ["update"], after={"size": float("inf")})]
        )
        with self.assertRaises(PlanProjectionError):
            compute_plan_hash(document)

    def test_rejects_change_without_address(self) -> None:
        broken = {"mode": "managed", "change": {"actions": ["create"]}}
        with self.assertRaisesRegex(PlanProjectionError, "address"):
            project_plan_changes(show_json([broken]))

    def test_rejects_document_missing_resource_changes(self) -> None:
        # A missing `resource_changes` is a corrupt plan; it must not default to
        # an empty (and therefore non-destructive) projection.
        document = {"format_version": "1.2", "terraform_version": "1.9.5"}
        with self.assertRaisesRegex(PlanProjectionError, "resource_changes"):
            project_plan_changes(document)
        with self.assertRaisesRegex(PlanProjectionError, "resource_changes"):
            has_destructive_changes(document)

    def test_rejects_entry_missing_change(self) -> None:
        broken = {
            "address": "aws_s3_bucket.logs",
            "mode": "managed",
            "type": "aws_s3_bucket",
            "name": "logs",
        }
        with self.assertRaisesRegex(PlanProjectionError, "change"):
            project_plan_changes(show_json([broken]))

    def test_rejects_change_missing_actions(self) -> None:
        # A missing `change.actions` would read as non-destructive; reject it so a
        # corrupt plan cannot bypass the destructive-change manual-review gate.
        broken = {
            "address": "aws_s3_bucket.logs",
            "mode": "managed",
            "type": "aws_s3_bucket",
            "name": "logs",
            "change": {"before": None, "after": {"acl": "private"}},
        }
        with self.assertRaisesRegex(PlanProjectionError, "actions"):
            project_plan_changes(show_json([broken]))
        with self.assertRaisesRegex(PlanProjectionError, "actions"):
            has_destructive_changes(show_json([broken]))

    def test_binary_plan_artifact_type_is_distinct_from_hashed_projection(self) -> None:
        self.assertNotEqual(ArtifactType.TERRAFORM_PLAN, ArtifactType.TERRAFORM_PLAN_BINARY)


class MappedResourceIdsContractTest(unittest.TestCase):
    """`mapped_resource_ids` 투영 (ADR-0019 §1-a)."""

    @staticmethod
    def _change(
        *,
        resource_type: str = "aws_s3_bucket_public_access_block",
        after: object = None,
        before: object = None,
        address: str = "aws_s3_bucket_public_access_block.demo",
    ) -> dict[str, object]:
        return {
            "address": address,
            "mode": "managed",
            "type": resource_type,
            "name": "demo",
            "index": None,
            "provider_name": "registry.terraform.io/hashicorp/aws",
            "change": {
                "actions": ["update"],
                "before": before,
                "after": after,
                "after_unknown": {},
                "replace_paths": [],
            },
        }

    def test_reads_the_bucket_name_from_the_allow_listed_attribute(self) -> None:
        document = {"resource_changes": [self._change(after={"bucket": "bucket-public-001"})]}
        self.assertEqual(mapped_resource_ids(document), ("bucket-public-001",))

    def test_falls_back_to_before_for_a_destroyed_resource(self) -> None:
        document = {
            "resource_changes": [self._change(after=None, before={"bucket": "bucket-gone-001"})]
        }
        self.assertEqual(mapped_resource_ids(document), ("bucket-gone-001",))

    def test_a_type_outside_the_allow_list_contributes_nothing(self) -> None:
        """허용 목록 밖 type은 매핑되지 않아 readiness가 BLOCKED가 된다(fail-closed)."""
        document = {
            "resource_changes": [
                self._change(
                    resource_type="aws_dynamodb_table",
                    address="aws_dynamodb_table.demo",
                    after={"bucket": "not-a-bucket", "name": "table-1"},
                )
            ]
        }
        self.assertEqual(mapped_resource_ids(document), ())

    def test_a_computed_identifier_is_skipped_not_guessed(self) -> None:
        document = {"resource_changes": [self._change(after={"bucket": None})]}
        self.assertEqual(mapped_resource_ids(document), ())

    def test_projects_the_ec2_instance_id(self) -> None:
        document = {
            "resource_changes": [
                self._change(
                    resource_type="aws_instance",
                    address="aws_instance.demo",
                    after={"id": "i-0123456789abcdef0", "associate_public_ip_address": False},
                )
            ]
        }
        self.assertEqual(mapped_resource_ids(document), ("i-0123456789abcdef0",))

    def test_a_created_ec2_instance_has_no_id_to_project(self) -> None:
        """생성 중인 인스턴스의 `id`는 computed다. 추측하지 않는다 — Finding도 아직 없다."""
        document = {
            "resource_changes": [
                self._change(
                    resource_type="aws_instance",
                    address="aws_instance.demo",
                    after={"associate_public_ip_address": False},
                )
            ]
        }
        self.assertEqual(mapped_resource_ids(document), ())

    def test_projects_the_rds_declared_identifier(self) -> None:
        document = {
            "resource_changes": [
                self._change(
                    resource_type="aws_db_instance",
                    address="aws_db_instance.demo",
                    after={"identifier": "demo-db-001", "publicly_accessible": False},
                )
            ]
        }
        self.assertEqual(mapped_resource_ids(document), ("demo-db-001",))

    def test_projects_one_alb_arn_from_the_load_balancer_and_its_listener(self) -> None:
        """리스너는 부모를 ARN으로만 지목한다. 두 리소스가 한 어휘로 모여야 한다."""
        arn = (
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
            "loadbalancer/app/demo-alb/50dc6c495c0c9188"
        )
        document = {
            "resource_changes": [
                self._change(
                    resource_type="aws_lb",
                    address="aws_lb.demo",
                    after={"arn": arn, "name": "demo-alb"},
                ),
                self._change(
                    resource_type="aws_lb_listener",
                    address="aws_lb_listener.https",
                    after={"load_balancer_arn": arn, "protocol": "HTTPS"},
                ),
            ]
        }
        self.assertEqual(mapped_resource_ids(document), (arn,))

    def test_ebs_and_security_group_resources_stay_outside_the_allow_list(self) -> None:
        """볼륨/SG id는 Finding 어휘가 아니다. 투영하면 다른 질문에 답하게 된다(fail-closed)."""
        document = {
            "resource_changes": [
                self._change(
                    resource_type="aws_ebs_volume",
                    address="aws_ebs_volume.demo",
                    after={"id": "vol-0123456789abcdef0", "encrypted": True},
                ),
                self._change(
                    resource_type="aws_security_group",
                    address="aws_security_group.demo",
                    after={"id": "sg-0123456789abcdef0"},
                ),
            ]
        }
        self.assertEqual(mapped_resource_ids(document), ())

    def test_deduplicates_and_sorts_for_determinism(self) -> None:
        document = {
            "resource_changes": [
                self._change(address="aws_s3_bucket_acl.b", after={"bucket": "bucket-b"}),
                self._change(address="aws_s3_bucket_acl.a", after={"bucket": "bucket-a"}),
                self._change(address="aws_s3_bucket_policy.a", after={"bucket": "bucket-a"}),
            ]
        }
        self.assertEqual(mapped_resource_ids(document), ("bucket-a", "bucket-b"))

    def test_covers_the_s3_sub_resources_the_mvp_rules_touch(self) -> None:
        """여섯 S3 Rule의 조치가 만드는 리소스는 모두 자기 bucket을 밝혀야 한다."""
        types = (
            "aws_s3_bucket",
            "aws_s3_bucket_acl",
            "aws_s3_bucket_logging",
            "aws_s3_bucket_policy",
            "aws_s3_bucket_public_access_block",
            "aws_s3_bucket_server_side_encryption_configuration",
        )
        for resource_type in types:
            document = {
                "resource_changes": [
                    self._change(
                        resource_type=resource_type,
                        address=f"{resource_type}.demo",
                        after={"bucket": "bucket-public-001"},
                    )
                ]
            }
            self.assertEqual(mapped_resource_ids(document), ("bucket-public-001",), resource_type)

    def test_rejects_a_change_without_a_type(self) -> None:
        document = {
            "resource_changes": [
                {
                    "address": "aws_s3_bucket.demo",
                    "change": {"actions": ["update"], "after": {"bucket": "b"}},
                }
            ]
        }
        with self.assertRaises(PlanProjectionError):
            mapped_resource_ids(document)


if __name__ == "__main__":
    unittest.main()
