"""Contract tests for the shared `terraform show -json` projection (ADR-0019 §1)."""

import unittest

from packages.contracts import (
    ArtifactType,
    PlanProjectionError,
    canonical_plan_bytes,
    compute_plan_hash,
    has_destructive_changes,
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


if __name__ == "__main__":
    unittest.main()
